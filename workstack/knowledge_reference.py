"""Bounded read-only Markdown references; no crawl, network, models or writes.

The caller chooses the root and its local identity explicitly. Text is untrusted
reference material, never an agent instruction or a sanitized Capture Packet.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat

MAX_DOCUMENT_BYTES = 512 * 1024
MAX_EXCERPT_CHARS = 6000
MAX_EXCERPT_LINES = 80
# Windows aliases these device names, including the superscript COM/LPT forms,
# before any directory lookup.
_DEVICE_SUFFIXES = '123456789\xb9\xb2\xb3'
RESERVED_DEVICE_NAMES = frozenset(
    {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} |
    {f'COM{suffix}' for suffix in _DEVICE_SUFFIXES} |
    {f'LPT{suffix}' for suffix in _DEVICE_SUFFIXES})
# Only CRLF, CR and LF end a physical Markdown line. U+2028, U+2029, U+0085
# and the vertical tab do not, so str.splitlines must not decide the span.
_PHYSICAL_LINE_END = re.compile('\r\n|\r|\n')


class KnowledgeReferenceError(ValueError):
    """Closed error code without source paths or document text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _identity(metadata: os.stat_result) -> tuple:
    # On Windows Python 3.12 lstat and fstat expose different ctime semantics
    # (creation versus metadata-change time). Compare their shared identity.
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode,
            metadata.st_size, metadata.st_mtime_ns)


def _safe_metadata(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
        raise KnowledgeReferenceError('linked_path_refused')
    return metadata


def _path_snapshot(path: Path) -> tuple:
    return tuple((str(part), _identity(_safe_metadata(part)))
                 for part in reversed((path, *path.parents)))


def _snapshot_after_read(path: Path) -> tuple:
    # A path that only stops being readable after its bytes were taken changed
    # under the read; it is not the document having been missing all along.
    try:
        return _path_snapshot(path)
    except OSError as error:
        raise KnowledgeReferenceError('source_changed_during_read') from error


def _reserved_device(part: str) -> bool:
    # Mirror the alias resolution PureWindowsPath reports, portably and on every
    # host: extension, stream suffix and trailing spaces do not defeat it.
    return part.partition('.')[0].partition(':')[0].rstrip(' ').upper() in RESERVED_DEVICE_NAMES


def _invalid_component(part: str) -> bool:
    return (not part or part.startswith('.') or part.endswith((' ', '.')) or
            _reserved_device(part))


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise KnowledgeReferenceError('invalid_document_path')
    parts = value.split('/')
    if any(character in value for character in '\\:*?"<>|') or any(ord(c) < 32 for c in value):
        raise KnowledgeReferenceError('invalid_document_path')
    if any(_invalid_component(part) for part in parts):
        raise KnowledgeReferenceError('invalid_document_path')
    if PurePosixPath(value).suffix.casefold() != '.md':
        raise KnowledgeReferenceError('markdown_required')
    return value


def _read_bytes(root: Path, relative: str) -> bytes:
    candidate = root.joinpath(*relative.split('/'))
    before = _path_snapshot(candidate)
    if not candidate.resolve(strict=True).is_relative_to(root):
        raise KnowledgeReferenceError('outside_vault')
    metadata = _safe_metadata(candidate)
    if not stat.S_ISREG(metadata.st_mode):
        raise KnowledgeReferenceError('regular_file_required')
    if metadata.st_size > MAX_DOCUMENT_BYTES:
        raise KnowledgeReferenceError('document_too_large')
    with candidate.open('rb') as stream:
        opened = _identity(os.fstat(stream.fileno()))
        if opened != _identity(metadata):
            raise KnowledgeReferenceError('source_changed_during_read')
        raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        finished = _identity(os.fstat(stream.fileno()))
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise KnowledgeReferenceError('document_too_large')
    if finished != opened or len(raw) != metadata.st_size or _snapshot_after_read(candidate) != before:
        raise KnowledgeReferenceError('source_changed_during_read')
    return raw


def _physical_lines(text: str) -> list[str]:
    if not text:
        return []
    lines = _PHYSICAL_LINE_END.split(text)
    if lines[-1] == '':
        lines.pop()
    return lines


def _validate_request(start_line: int, end_line: int, expected_sha256: str | None) -> None:
    if (type(start_line) is not int or type(end_line) is not int or
            start_line < 1 or end_line < start_line or end_line - start_line >= MAX_EXCERPT_LINES):
        raise KnowledgeReferenceError('invalid_line_range')
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or
            not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)):
        raise KnowledgeReferenceError('invalid_source_revision')


def _read_text(root: Path, relative: str) -> tuple[bytes, str]:
    try:
        raw = _read_bytes(root, relative)
        return raw, raw.decode('utf-8-sig')
    except FileNotFoundError as error:
        raise KnowledgeReferenceError('document_missing') from error
    except PermissionError as error:
        raise KnowledgeReferenceError('document_unreadable') from error
    except UnicodeDecodeError as error:
        raise KnowledgeReferenceError('invalid_utf8') from error
    except OSError as error:
        raise KnowledgeReferenceError('document_unavailable') from error


@dataclass(frozen=True)
class MarkdownVault:
    root: Path
    vault_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.vault_id, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', self.vault_id):
            raise KnowledgeReferenceError('invalid_vault_id')
        try:
            root = Path(os.path.abspath(self.root))
            _path_snapshot(root)
            if not stat.S_ISDIR(_safe_metadata(root).st_mode):
                raise KnowledgeReferenceError('vault_unavailable')
            object.__setattr__(self, 'root', root)
        except OSError as error:
            raise KnowledgeReferenceError('vault_unavailable') from error

    def read(self, document: str, *, start_line: int = 1, end_line: int = 40,
             expected_sha256: str | None = None) -> dict:
        relative = _relative_path(document)
        _validate_request(start_line, end_line, expected_sha256)
        raw, text = _read_text(self.root, relative)
        if '\x00' in text:
            raise KnowledgeReferenceError('invalid_text')
        lines = _physical_lines(text)
        if not lines:
            raise KnowledgeReferenceError('document_empty')
        if start_line > len(lines):
            raise KnowledgeReferenceError('line_range_unavailable')
        selected = lines[start_line - 1:end_line]
        excerpt = '\n'.join(selected)
        digest = hashlib.sha256(raw).hexdigest()
        title = next((line[2:].strip() for line in lines if line.startswith('# ') and line[2:].strip()), PurePosixPath(relative).stem)
        return {
            'schema_version': 1, 'provider': 'markdown-vault', 'vault_id': self.vault_id,
            'document_path': relative, 'title': title[:240], 'source_sha256': digest,
            'expected_sha256': expected_sha256,
            'freshness': 'uncompared' if expected_sha256 is None else ('unchanged' if digest == expected_sha256 else 'changed'),
            'start_line': start_line, 'end_line': min(end_line, len(lines)),
            'excerpt': excerpt[:MAX_EXCERPT_CHARS], 'excerpt_truncated': len(excerpt) > MAX_EXCERPT_CHARS,
            'trust': 'external_reference', 'read_only': True,
        }
