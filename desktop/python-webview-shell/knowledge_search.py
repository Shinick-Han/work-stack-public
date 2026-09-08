"""Bounded related-document search over one already registered Markdown vault.

Work Stack does not build an index.  A local provider the coordinator installs
answers a query with candidate spans, and every candidate is re-read through
the existing vault reader before it can be shown: the provider proposes where
to look, it never supplies excerpt text, Task changes or commands.

The provider is trusted local executable configuration, not user input.  The
UI can send only a query; it cannot name a command, an executable, a root or a
URL.  The configuration document lives beside the registry under the desktop
StateRoot and is read through the same bound path chain, so a link planted over
it is refused rather than followed.

Nothing here is a sandbox against a hostile provider: the coordinator chooses
what runs.  What is bounded is what a misbehaving provider can do to the host --
it is fed one small JSON request, its stdout is capped, its stderr is read and
discarded rather than surfaced, and it is terminated and reaped once it exceeds
its runtime or its output budget.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time

from knowledge_registry_paths import KnowledgeRegistryError, link_metadata, refuse
from workstack.knowledge_reference import (KnowledgeReferenceError, MarkdownVault,
                                           MAX_EXCERPT_LINES)

SEARCH_PROVIDER_FILE = 'search-provider.json'
PROVIDER_SCHEMA_VERSION = 1
MAX_QUERY_CHARS = 1000
MATCH_LIMIT = 5
PROVIDER_CANDIDATE_LIMIT = 20
PROVIDER_TIMEOUT_SECONDS = 75.0
PROVIDER_POLL_SECONDS = 0.02
REAP_TIMEOUT_SECONDS = 5.0
MAX_PROVIDER_STDOUT_BYTES = 64 * 1024
MAX_PROVIDER_STDERR_BYTES = 8 * 1024
MAX_COMMAND_PARTS = 16
MAX_COMMAND_PART_CHARS = 512
MAX_CORPUS_LABEL_CHARS = 120
MAX_DOCUMENT_COUNT = 100_000_000
MAX_CANDIDATE_PATH_CHARS = 1024
# A search hit is a preview of where to look, not the linked excerpt: the read
# stays bounded well below the reader's own excerpt budget so five previews fit
# in one bounded host response even when every character escapes to six bytes.
SEARCH_EXCERPT_CHARS = 1200

UNCONFIGURED = 'search_unconfigured'
UNAVAILABLE = 'search_unavailable'
TIMEOUT = 'search_timeout'
INVALID_RESPONSE = 'search_invalid_response'

CORPUS_KEYS = frozenset({'label', 'document_count', 'indexed_at'})
CANDIDATE_KEYS = frozenset({'document_path', 'start_line', 'end_line', 'source_sha256'})
PROVIDER_KEYS = frozenset({'schema_version', 'corpus', 'candidates'})
CONFIG_KEYS = frozenset({'schema_version', 'command'})
SHA256_PATTERN = re.compile(r'[0-9a-f]{64}')
RFC3339_PATTERN = re.compile(
    r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d{1,6})?([Zz]|[+-]\d{2}:\d{2})')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _control_character(character: str) -> bool:
    """Report C0, DEL and C1 characters; normal whitespace is not a control."""

    code = ord(character)
    return (code < 32 and character not in '\t\n\r') or code == 127 or 0x80 <= code <= 0x9f


def _display_control(character: str) -> bool:
    """Report every control character; a one-line label has no whitespace layout."""

    code = ord(character)
    return code < 32 or code == 127 or 0x80 <= code <= 0x9f


def validated_query(value: object) -> str:
    """Bound the one field the UI supplies, independently of the caller."""

    if not isinstance(value, str):
        refuse('invalid_query')
    trimmed = value.strip()
    if not trimmed or len(trimmed) > MAX_QUERY_CHARS:
        refuse('invalid_query')
    if any(_control_character(character) for character in trimmed):
        refuse('invalid_query')
    return trimmed


def _command_part(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_COMMAND_PART_CHARS:
        refuse(UNAVAILABLE)
    if any(_control_character(character) for character in value):
        refuse(UNAVAILABLE)
    return value


def _executable(value: str) -> str:
    """Accept one absolute, unlinked, regular executable file, as configured.

    The path is checked as given -- a reparse point or symlink at the leaf is
    refused rather than followed -- in the same style the registry uses for its
    own documents.  Relative and traversal spellings never reach the launcher.
    """

    if not os.path.isabs(value) or '..' in Path(value).parts:
        refuse(UNAVAILABLE)
    try:
        path = Path(os.path.abspath(value))
    except (OSError, ValueError) as error:
        raise KnowledgeRegistryError(UNAVAILABLE) from error
    try:
        metadata = link_metadata(path)
    except KnowledgeRegistryError as error:
        raise KnowledgeRegistryError(UNAVAILABLE) from error
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        refuse(UNAVAILABLE)
    return str(path)


def provider_command(document: object) -> list[str]:
    """Return the configured command, or refuse with a closed search code.

    ``None`` is the ordinary unconfigured device.  Every other shape failure is
    a configuration the coordinator has to fix, which the UI reports as an
    unavailable provider rather than as a missing one.
    """

    if document is None:
        refuse(UNCONFIGURED)
    if not isinstance(document, dict) or set(document) != CONFIG_KEYS:
        refuse(UNAVAILABLE)
    if type(document['schema_version']) is not int or (
            document['schema_version'] != PROVIDER_SCHEMA_VERSION):
        refuse(UNAVAILABLE)
    command = document['command']
    if not isinstance(command, list) or not 1 <= len(command) <= MAX_COMMAND_PARTS:
        refuse(UNAVAILABLE)
    parts = [_command_part(part) for part in command]
    return [_executable(parts[0]), *parts[1:]]


def _feed(stream: object, payload: bytes) -> None:
    try:
        stream.write(payload)
        stream.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _drain(stream: object, limit: int, sink: list, oversize: threading.Event | None) -> None:
    """Take at most ``limit`` bytes and stop; the rest of a flood is refused."""

    try:
        taken = stream.read(limit + 1)
    except (OSError, ValueError):
        taken = b''
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    taken = taken if isinstance(taken, bytes) else b''
    sink.append(taken)
    if oversize is not None and len(taken) > limit:
        oversize.set()


def _reap(process: object) -> None:
    """Stop and collect the process this host started, without widening."""

    try:
        if process.poll() is not None:
            return
        process.terminate()
        process.wait(timeout=REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=REAP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    except (OSError, ValueError):
        pass


def _await_exit(process: object, oversize: threading.Event, deadline: float) -> None:
    while True:
        if oversize.is_set():
            _reap(process)
            refuse(INVALID_RESPONSE)
        if process.poll() is not None:
            return
        if time.monotonic() >= deadline:
            _reap(process)
            refuse(TIMEOUT)
        time.sleep(PROVIDER_POLL_SECONDS)


def request_payload(query: str, vault_root: str) -> bytes:
    return (json.dumps({'schema_version': PROVIDER_SCHEMA_VERSION, 'query': query,
                        'vault_root': vault_root, 'limit': MATCH_LIMIT},
                       ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')


def run_provider(command: list[str], query: str, vault_root: str, *,
                 timeout: float = PROVIDER_TIMEOUT_SECONDS,
                 launcher=subprocess.Popen) -> bytes:
    """Run the configured provider once and return its bounded stdout bytes.

    The three pipes are serviced concurrently, so a provider that never reads
    its request or floods its output cannot wedge the host; the runtime budget
    and the output budget are both enforced by terminating the owned process.
    """

    payload = request_payload(query, vault_root)
    try:
        process = launcher(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, shell=False, creationflags=NO_WINDOW)
    except (OSError, ValueError) as error:
        raise KnowledgeRegistryError(UNAVAILABLE) from error
    stdout: list = []
    stderr: list = []
    oversize = threading.Event()
    workers = [
        threading.Thread(target=_feed, args=(process.stdin, payload), daemon=True),
        threading.Thread(target=_drain,
                         args=(process.stdout, MAX_PROVIDER_STDOUT_BYTES, stdout, oversize),
                         daemon=True),
        # stderr is read so the provider cannot block on a full pipe, and then
        # discarded: provider diagnostics never reach the UI.
        threading.Thread(target=_drain,
                         args=(process.stderr, MAX_PROVIDER_STDERR_BYTES, stderr, None),
                         daemon=True),
    ]
    try:
        for worker in workers:
            worker.start()
        _await_exit(process, oversize, time.monotonic() + max(float(timeout), 0.0))
    except BaseException:
        _reap(process)
        raise
    for worker in workers:
        worker.join(timeout=REAP_TIMEOUT_SECONDS)
    raw = stdout[0] if stdout else b''
    if len(raw) > MAX_PROVIDER_STDOUT_BYTES:
        refuse(INVALID_RESPONSE)
    if process.returncode != 0:
        refuse(UNAVAILABLE)
    return raw


def _reject_duplicate_members(pairs: list) -> dict:
    members: dict = {}
    for key, value in pairs:
        if key in members:
            raise ValueError('duplicate provider response member')
        members[key] = value
    return members


def _reject_constant(value: str) -> None:
    raise ValueError('provider response constants are not allowed')


def _corpus(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != CORPUS_KEYS:
        refuse(INVALID_RESPONSE)
    label = value['label']
    if not isinstance(label, str) or not label or len(label) > MAX_CORPUS_LABEL_CHARS:
        refuse(INVALID_RESPONSE)
    # A corpus label is shown next to the results, so it carries no control
    # characters and no filesystem separators that would leak a local root.
    if any(_display_control(character) for character in label) or any(
            separator in label for separator in '/\\'):
        refuse(INVALID_RESPONSE)
    count = value['document_count']
    if type(count) is not int or not 0 <= count <= MAX_DOCUMENT_COUNT:
        refuse(INVALID_RESPONSE)
    return {'label': label, 'document_count': count,
            'indexed_at': _indexed_at(value['indexed_at'])}


def _indexed_at(value: object) -> str:
    if not isinstance(value, str) or not RFC3339_PATTERN.fullmatch(value):
        refuse(INVALID_RESPONSE)
    try:
        datetime.fromisoformat(value.replace('z', '+00:00').replace('Z', '+00:00'))
    except ValueError as error:
        raise KnowledgeRegistryError(INVALID_RESPONSE) from error
    return value


def _candidate(value: object) -> dict:
    """Bound one proposed span; the vault reader still decides what it reads."""

    if not isinstance(value, dict) or set(value) != CANDIDATE_KEYS:
        refuse(INVALID_RESPONSE)
    document_path = value['document_path']
    if (not isinstance(document_path, str) or not document_path or
            len(document_path) > MAX_CANDIDATE_PATH_CHARS):
        refuse(INVALID_RESPONSE)
    start_line, end_line = value['start_line'], value['end_line']
    if type(start_line) is not int or type(end_line) is not int:
        refuse(INVALID_RESPONSE)
    if start_line < 1 or end_line < start_line or end_line - start_line >= MAX_EXCERPT_LINES:
        refuse(INVALID_RESPONSE)
    source_sha256 = value['source_sha256']
    if not isinstance(source_sha256, str) or not SHA256_PATTERN.fullmatch(source_sha256):
        refuse(INVALID_RESPONSE)
    return {'document_path': document_path, 'start_line': start_line,
            'end_line': end_line, 'source_sha256': source_sha256}


def parsed_response(raw: bytes) -> tuple[dict, list]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_PROVIDER_STDOUT_BYTES:
        refuse(INVALID_RESPONSE)
    try:
        document = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant,
                              object_pairs_hook=_reject_duplicate_members)
    except (UnicodeDecodeError, RecursionError, ValueError) as error:
        raise KnowledgeRegistryError(INVALID_RESPONSE) from error
    if not isinstance(document, dict) or set(document) != PROVIDER_KEYS:
        refuse(INVALID_RESPONSE)
    # ``v1`` is an integer literal, not a numeric value: ``1.0`` and ``True``
    # both compare equal to ``1`` in Python and neither is the declared shape.
    if type(document['schema_version']) is not int or (
            document['schema_version'] != PROVIDER_SCHEMA_VERSION):
        refuse(INVALID_RESPONSE)
    candidates = document['candidates']
    if not isinstance(candidates, list) or len(candidates) > PROVIDER_CANDIDATE_LIMIT:
        refuse(INVALID_RESPONSE)
    return _corpus(document['corpus']), [_candidate(entry) for entry in candidates]


def _preview(read: dict) -> dict:
    excerpt = read['excerpt']
    if len(excerpt) <= SEARCH_EXCERPT_CHARS:
        return read
    return {**read, 'excerpt': excerpt[:SEARCH_EXCERPT_CHARS], 'excerpt_truncated': True}


def verified_matches(vault: MarkdownVault, candidates: list,
                     limit: int = MATCH_LIMIT) -> tuple[list, int]:
    """Re-read each proposed span; only unchanged in-vault hits are presented.

    A candidate whose document moved, changed, disappeared or never resolved
    inside the vault is omitted and counted.  It is not a current source, and
    the provider's snapshot is never allowed to stand in for one.
    """

    matches: list = []
    omitted = 0
    for candidate in candidates:
        if len(matches) >= limit:
            break
        try:
            read = vault.read(candidate['document_path'],
                              start_line=candidate['start_line'],
                              end_line=candidate['end_line'],
                              expected_sha256=candidate['source_sha256'])
        except KnowledgeReferenceError:
            omitted += 1
            continue
        if read['freshness'] != 'unchanged':
            omitted += 1
            continue
        matches.append(_preview(read))
    return matches, omitted


def search_references(document: object, query: object, vault: MarkdownVault, *,
                      timeout: float = PROVIDER_TIMEOUT_SECONDS,
                      launcher=subprocess.Popen) -> dict:
    """Answer one explicit query with verified, in-vault reference candidates."""

    command = provider_command(document)
    raw = run_provider(command, validated_query(query), str(vault.root),
                       timeout=timeout, launcher=launcher)
    corpus, candidates = parsed_response(raw)
    matches, omitted = verified_matches(vault, candidates)
    return {'corpus': corpus, 'matches': matches, 'omitted_count': omitted}
