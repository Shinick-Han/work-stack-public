"""Device-local storage for explicitly chosen Markdown knowledge references.

The registry is deliberately separate from the canonical Work Stack Store: it
holds only the vault roots the user picked on this device and the reference
metadata they explicitly linked to a Task.  Nothing here is synchronised,
backed up, or promoted into Task authority, and no document text is ever
persisted -- a saved reference is a pointer plus the source revision that was
read when the user linked it.

Every document is bounded, closed-schema JSON.  Writes take one interprocess
lease and land through an atomic replace, so a corrupt or oversized registry is
refused rather than reset, and a serialized in-process host worker never
substitutes for the cross-process lease.

Every registry path is proved component by component below the StateRoot and
then bound: each directory from the StateRoot down to the one a write lands in
is held open for the length of the operation, so it can be neither renamed away
nor replaced by a junction between the create, the lease open and the replace.
Rechecking a path only reports what was true at the moment of the check, which
is why the held directory -- not the recheck -- is what closes that window.
The StateRoot itself is the boundary the host designates: what lies above it is
the host's choice, not an escape from it.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

import knowledge_search
from knowledge_registry_paths import (bound_chain, guarded_chain, KnowledgeRegistryError,
                                      link_metadata, refuse, same_root)
from workstack.knowledge_context import task_binding
from workstack.knowledge_reference import KnowledgeReferenceError, MarkdownVault

KNOWLEDGE_DIRECTORY = 'knowledge'
VAULTS_FILE = 'vaults.json'
LEASE_FILE = 'registry.lock'
TASKS_DIRECTORY = 'tasks'
SCHEMA_VERSION = 1
MAX_REGISTRY_BYTES = 256 * 1024
MAX_VAULTS = 32
MAX_REFERENCES = 64
MAX_REASON_CHARS = 500
MAX_LABEL_CHARS = 120
MAX_ROOT_CHARS = 1024
MAX_DOCUMENT_PATH_CHARS = 1024
LEASE_TIMEOUT_SECONDS = 5.0
LEASE_POLL_SECONDS = 0.02

BINDING_KEYS = frozenset({'workspace_uid', 'task_uid', 'task_id', 'task_revision'})
VAULT_KEYS = ('vault_id', 'label', 'root')
REFERENCE_KEYS = ('reference_id', 'vault_id', 'document_path', 'start_line', 'end_line',
                  'source_sha256', 'reason')
VAULT_ID_PATTERN = re.compile(r'[a-z0-9]{32}')
SHA256_PATTERN = re.compile(r'[0-9a-f]{64}')


@contextmanager
def _reader_errors():
    """Surface the shared reader's closed codes as registry refusals."""

    try:
        yield
    except KnowledgeReferenceError as error:
        raise KnowledgeRegistryError(error.code) from error


def _binding(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != BINDING_KEYS:
        refuse('invalid_binding')
    with _reader_errors():
        return task_binding({'id': value['task_id'], 'uid': value['task_uid'],
                             'revision': value['task_revision']}, value['workspace_uid'])


def _text(value: object, limit: int, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        refuse(code)
    return value


def _reason(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_REASON_CHARS:
        refuse('invalid_reason')
    if any(ord(character) < 32 and character not in '\n\t' for character in value):
        refuse('invalid_reason')
    return value


def _vault_id(value: object) -> str:
    if not isinstance(value, str) or not VAULT_ID_PATTERN.fullmatch(value):
        refuse('invalid_vault_id')
    return value


def _reference_id(value: object) -> str:
    if not isinstance(value, str):
        refuse('invalid_reference_id')
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise KnowledgeRegistryError('invalid_reference_id') from error
    if str(parsed) != value or parsed.int == 0:
        refuse('invalid_reference_id')
    return value


def _required_sha256(value: object) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        refuse('invalid_source_revision')
    return value


def _canonical_root(root: object) -> tuple[Path, str]:
    """Validate a user-chosen directory and return its canonical form and label.

    The chosen path is checked as given so a symlink or reparse point anywhere
    in the chain is refused instead of being followed.
    """

    if not isinstance(root, (str, Path)) or not str(root):
        refuse('invalid_vault_root')
    try:
        absolute = Path(os.path.abspath(root))
    except (OSError, ValueError) as error:
        raise KnowledgeRegistryError('invalid_vault_root') from error
    with _reader_errors():
        MarkdownVault(absolute, uuid.uuid4().hex)
    try:
        canonical = Path(os.path.realpath(absolute))
    except (OSError, ValueError) as error:
        raise KnowledgeRegistryError('vault_unavailable') from error
    if len(str(canonical)) > MAX_ROOT_CHARS:
        refuse('invalid_vault_root')
    label = canonical.name or canonical.drive or canonical.anchor
    label = ''.join(character for character in label if ord(character) >= 32)[:MAX_LABEL_CHARS]
    if not label:
        refuse('invalid_vault_root')
    return canonical, label


def _identity(metadata: os.stat_result) -> tuple:
    # Only the entry identity is shared between lstat and fstat on Windows;
    # the timestamps carry different semantics and are deliberately excluded.
    return (metadata.st_dev, metadata.st_ino)


def _open_checked(bound, name: str, mode: str, before, code: str):
    """Open a registry file inside a bound directory and prove the handle.

    The binding is what keeps the parent from being redirected under the open;
    this proves the leaf.  The entry checked before the open, the entry seen
    after it and the handle itself must all name one regular file, so an entry
    swapped for a link in that window is refused instead of written through.
    """

    try:
        stream = bound.open_stream(name, mode)
    except OSError as error:
        raise KnowledgeRegistryError(code) from error
    try:
        after = bound.entry(name)
        if after is None or not stat.S_ISREG(after.st_mode):
            refuse('linked_path_refused')
        if _identity(after) != _identity(os.fstat(stream.fileno())):
            refuse('linked_path_refused')
        if before is not None and _identity(before) != _identity(after):
            refuse('linked_path_refused')
    except BaseException:
        stream.close()
        raise
    return stream


def _read_bounded(bound, name: str) -> bytes | None:
    """Read one bounded registry document out of a directory that is held open."""

    metadata = bound.entry(name)
    if metadata is None:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        refuse('registry_corrupt')
    if metadata.st_size > MAX_REGISTRY_BYTES:
        refuse('registry_too_large')
    with _open_checked(bound, name, 'rb', metadata, 'registry_unavailable') as stream:
        try:
            raw = stream.read(MAX_REGISTRY_BYTES + 1)
        except OSError as error:
            raise KnowledgeRegistryError('registry_unavailable') from error
    if len(raw) > MAX_REGISTRY_BYTES:
        refuse('registry_too_large')
    return raw


def _read_document(anchor: Path, path: Path) -> dict | None:
    with bound_chain(anchor, path.parent, create=False) as bound:
        raw = None if bound is None else _read_bounded(bound, path.name)
    if raw is None:
        return None
    try:
        document = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnowledgeRegistryError('registry_corrupt') from error
    if not isinstance(document, dict):
        refuse('registry_corrupt')
    return document


def _write_document(anchor: Path, path: Path, document: dict) -> None:
    payload = (json.dumps(document, ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')
    if len(payload) > MAX_REGISTRY_BYTES:
        refuse('registry_too_large')
    with bound_chain(anchor, path.parent, create=True) as bound:
        if bound is None:
            refuse('registry_unwritable')
        existing = bound.entry(path.name)
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            refuse('registry_unwritable')
        temporary = f'.{path.name}.{uuid.uuid4().hex}.tmp'
        try:
            with _open_checked(bound, temporary, 'xb', None, 'registry_unwritable') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Both names are resolved inside the bound directory, so the
            # replace cannot land anywhere the chain was not proved to reach.
            bound.entry(path.name)
            bound.replace(temporary, path.name)
            bound.sync()
        except OSError as error:
            raise KnowledgeRegistryError('registry_unwritable') from error
        finally:
            try:
                bound.unlink(temporary)
            except OSError:
                pass


def _acquire_os_lease(stream) -> bool:
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt

        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_os_lease(stream) -> None:
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _validated_vault(entry: object, seen_ids: set, seen_roots: list) -> dict:
    if not isinstance(entry, dict) or set(entry) != set(VAULT_KEYS):
        refuse('registry_corrupt')
    try:
        vault_id = _vault_id(entry['vault_id'])
        label = _text(entry['label'], MAX_LABEL_CHARS, 'registry_corrupt')
        root = _text(entry['root'], MAX_ROOT_CHARS, 'registry_corrupt')
    except KnowledgeRegistryError as error:
        raise KnowledgeRegistryError('registry_corrupt') from error
    if vault_id in seen_ids or any(same_root(root, other) for other in seen_roots):
        refuse('registry_corrupt')
    seen_ids.add(vault_id)
    seen_roots.append(root)
    return {'vault_id': vault_id, 'label': label, 'root': root}


def _validated_reference(entry: object, vault_ids: set, seen: set) -> dict:
    if not isinstance(entry, dict) or set(entry) != set(REFERENCE_KEYS):
        refuse('registry_corrupt')
    try:
        reference_id = _reference_id(entry['reference_id'])
        vault_id = _vault_id(entry['vault_id'])
        document_path = _text(entry['document_path'], MAX_DOCUMENT_PATH_CHARS, 'registry_corrupt')
        source_sha256 = _required_sha256(entry['source_sha256'])
        reason = _reason(entry['reason'])
    except KnowledgeRegistryError as error:
        raise KnowledgeRegistryError('registry_corrupt') from error
    start_line, end_line = entry['start_line'], entry['end_line']
    if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line < start_line:
        refuse('registry_corrupt')
    key = (vault_id, document_path, start_line, end_line)
    if reference_id in seen or key in seen:
        refuse('registry_corrupt')
    if vault_ids is not None and vault_id not in vault_ids:
        refuse('registry_corrupt')
    seen.add(reference_id)
    seen.add(key)
    return {'reference_id': reference_id, 'vault_id': vault_id, 'document_path': document_path,
            'start_line': start_line, 'end_line': end_line, 'source_sha256': source_sha256,
            'reason': reason}


def _validated_vaults(document: dict | None) -> list:
    if document is None:
        return []
    if set(document) != {'schema_version', 'local_only', 'vaults'}:
        refuse('registry_corrupt')
    if document['schema_version'] != SCHEMA_VERSION or document['local_only'] is not True:
        refuse('registry_corrupt')
    entries = document['vaults']
    if not isinstance(entries, list) or len(entries) > MAX_VAULTS:
        refuse('registry_corrupt')
    seen_ids: set = set()
    seen_roots: list = []
    return [_validated_vault(entry, seen_ids, seen_roots) for entry in entries]


def _validated_references(document: dict | None, binding: dict, vault_ids: set) -> list:
    if document is None:
        return []
    if set(document) != {'schema_version', 'local_only', 'workspace_uid', 'task_uid', 'references'}:
        refuse('registry_corrupt')
    if document['schema_version'] != SCHEMA_VERSION or document['local_only'] is not True:
        refuse('registry_corrupt')
    if (document['workspace_uid'] != binding['workspace_uid'] or
            document['task_uid'] != binding['task_uid']):
        refuse('registry_corrupt')
    entries = document['references']
    if not isinstance(entries, list) or len(entries) > MAX_REFERENCES:
        refuse('registry_corrupt')
    seen: set = set()
    return [_validated_reference(entry, vault_ids, seen) for entry in entries]


class KnowledgeRegistry:
    """Local-only vault and reference storage rooted at the desktop StateRoot."""

    def __init__(self, state_root, lease_timeout: float = LEASE_TIMEOUT_SECONDS):
        if not isinstance(state_root, (str, Path)) or not str(state_root):
            refuse('invalid_state_root')
        if not isinstance(lease_timeout, (int, float)) or isinstance(lease_timeout, bool) or lease_timeout < 0:
            refuse('invalid_lease_timeout')
        self._state_root = Path(os.path.abspath(state_root))
        self._directory = self._state_root / KNOWLEDGE_DIRECTORY
        self._lease_timeout = float(lease_timeout)
        # Admission: a registry directory that is already a link is refused
        # here rather than opened later.  Every operation re-proves the chain,
        # because a link planted after this point must be refused as well.
        guarded_chain(self._state_root, self._directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def _task_path(self, binding: dict) -> Path:
        return (self._directory / TASKS_DIRECTORY / binding['workspace_uid'] /
                f"{binding['task_uid']}.json")

    def _load_vaults(self) -> list:
        return _validated_vaults(_read_document(self._state_root, self._directory / VAULTS_FILE))

    def _load_references(self, binding: dict, vaults: list) -> list:
        return _validated_references(_read_document(self._state_root, self._task_path(binding)),
                                     binding, {vault['vault_id'] for vault in vaults})

    @contextmanager
    def _lease(self):
        """Hold the one interprocess write lease for this StateRoot."""

        # The whole chain stays bound for the length of the lease, so the
        # registry directory the lease names cannot be swapped underneath the
        # writes it guards.
        with bound_chain(self._state_root, self._directory, create=True) as bound:
            if bound is None:
                refuse('registry_unwritable')
            existing = bound.entry(LEASE_FILE)
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                refuse('registry_unwritable')
            stream = _open_checked(bound, LEASE_FILE, 'a+b', existing, 'registry_unwritable')
            acquired = False
            try:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b'\0')
                    stream.flush()
                    os.fsync(stream.fileno())
                deadline = time.monotonic() + self._lease_timeout
                while True:
                    acquired = _acquire_os_lease(stream)
                    if acquired:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        refuse('registry_busy')
                    time.sleep(min(LEASE_POLL_SECONDS, remaining))
                yield
            finally:
                try:
                    if acquired:
                        _release_os_lease(stream)
                finally:
                    stream.close()

    def _vault_root(self, vaults: list, vault_id: str) -> str:
        for vault in vaults:
            if vault['vault_id'] == vault_id:
                return vault['root']
        refuse('unknown_vault')

    def _open_vault(self, vaults: list, vault_id: str) -> MarkdownVault:
        """Resolve a saved vault without ever searching for a moved directory."""

        with _reader_errors():
            return MarkdownVault(self._vault_root(vaults, _vault_id(vault_id)), vault_id)

    def status(self) -> dict:
        vaults = sorted(self._load_vaults(), key=lambda vault: (vault['label'], vault['vault_id']))
        return {'vaults': [{'vault_id': vault['vault_id'], 'label': vault['label']} for vault in vaults],
                'local_only': True}

    def add_vault(self, root) -> dict:
        canonical, label = _canonical_root(root)
        with self._lease():
            vaults = self._load_vaults()
            for vault in vaults:
                if same_root(vault['root'], str(canonical)):
                    return {'vault_id': vault['vault_id'], 'label': vault['label']}
            if len(vaults) >= MAX_VAULTS:
                refuse('vault_limit_reached')
            vault = {'vault_id': uuid.uuid4().hex, 'label': label, 'root': str(canonical)}
            _write_document(self._state_root, self._directory / VAULTS_FILE,
                            {'schema_version': SCHEMA_VERSION, 'local_only': True,
                             'vaults': [*vaults, vault]})
        return {'vault_id': vault['vault_id'], 'label': vault['label']}

    def list_references(self, binding) -> list:
        validated = _binding(binding)
        return self._load_references(validated, self._load_vaults())

    def read_reference(self, binding, vault_id, document_path, start_line, end_line,
                       expected_sha256=None) -> dict:
        _binding(binding)
        vault = self._open_vault(self._load_vaults(), vault_id)
        with _reader_errors():
            return vault.read(document_path, start_line=start_line, end_line=end_line,
                              expected_sha256=expected_sha256)

    def search_references(self, binding, vault_id, query) -> dict:
        """Answer one explicit query out of an already registered vault.

        No lease is taken: search reads, and the provider budget is far longer
        than any write should hold the registry.  The saved vault is resolved
        exactly as a read does, so a moved vault is refused, never searched
        for.
        """

        _binding(binding)
        vault = self._open_vault(self._load_vaults(), vault_id)
        return knowledge_search.search_references(self._search_provider_document(),
                                                  query, vault)

    def _search_provider_document(self):
        """Read the coordinator-provisioned provider configuration, if any.

        The document is read through the same bound chain as the registry's own
        state.  An absent file is an unconfigured device; anything the registry
        refuses is a configuration to fix, reported without its reason.
        """

        try:
            return _read_document(self._state_root,
                                  self._directory / knowledge_search.SEARCH_PROVIDER_FILE)
        except KnowledgeRegistryError as error:
            raise KnowledgeRegistryError(knowledge_search.UNAVAILABLE) from error

    def pin_reference(self, binding, vault_id, document_path, start_line, end_line,
                      expected_sha256, reason) -> dict:
        validated = _binding(binding)
        expected = _required_sha256(expected_sha256)
        stored_reason = _reason(reason)
        with self._lease():
            vaults = self._load_vaults()
            vault = self._open_vault(vaults, vault_id)
            with _reader_errors():
                read = vault.read(document_path, start_line=start_line, end_line=end_line,
                                  expected_sha256=expected)
            if read['freshness'] != 'unchanged':
                refuse('source_revision_conflict')
            references = self._load_references(validated, vaults)
            key = (read['vault_id'], read['document_path'], read['start_line'], read['end_line'])
            saved = next((reference for reference in references
                          if (reference['vault_id'], reference['document_path'],
                              reference['start_line'], reference['end_line']) == key), None)
            if saved is None:
                if len(references) >= MAX_REFERENCES:
                    refuse('reference_limit_reached')
                saved = {'reference_id': str(uuid.uuid4()), 'vault_id': read['vault_id'],
                         'document_path': read['document_path'], 'start_line': read['start_line'],
                         'end_line': read['end_line'], 'source_sha256': read['source_sha256'],
                         'reason': stored_reason}
                references = [*references, saved]
            else:
                saved = {**saved, 'source_sha256': read['source_sha256'], 'reason': stored_reason}
                references = [saved if reference['reference_id'] == saved['reference_id'] else reference
                              for reference in references]
            self._write_references(validated, references)
        return dict(saved)

    def unpin_reference(self, binding, reference_id) -> bool:
        validated = _binding(binding)
        wanted = _reference_id(reference_id)
        with self._lease():
            references = self._load_references(validated, self._load_vaults())
            remaining = [reference for reference in references if reference['reference_id'] != wanted]
            if len(remaining) == len(references):
                return False
            self._write_references(validated, remaining)
        return True

    def _write_references(self, binding: dict, references: list) -> None:
        # The Task revision that authorised this edit is never persisted: a
        # saved reference is reference material, not standing authority.
        _write_document(self._state_root, self._task_path(binding),
                        {'schema_version': SCHEMA_VERSION, 'local_only': True,
                         'workspace_uid': binding['workspace_uid'], 'task_uid': binding['task_uid'],
                         'references': references})
