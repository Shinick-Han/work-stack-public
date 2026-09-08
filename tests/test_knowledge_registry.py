"""Device-local knowledge registry: identity, closed schema, lease and writes.

The registry stores only chosen vault roots and reference metadata under a
temporary StateRoot; no test touches a live Store, a real vault, or the
network.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / 'desktop' / 'python-webview-shell'
MODULE_PATH = SHELL / 'knowledge_registry.py'
for entry in (str(ROOT), str(SHELL)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
SPEC = importlib.util.spec_from_file_location('knowledge_registry_test', MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
MODULE_PATHS = importlib.import_module('knowledge_registry_paths')

KnowledgeRegistry = MODULE.KnowledgeRegistry
KnowledgeRegistryError = MODULE.KnowledgeRegistryError

WORKSPACE = '66666666-6666-4666-8666-666666666666'
OTHER_WORKSPACE = '55555555-5555-4555-8555-555555555555'
TASK_UID = '77777777-7777-4777-8777-777777777777'
OTHER_TASK_UID = '88888888-8888-4888-8888-888888888888'
ABSENT_REFERENCE = '99999999-9999-4999-8999-999999999999'
BODY = '# 설계 검토\n원문 근거\n다음 행동\n'

LEASE_HOLDER = '''
import importlib.util, os, sys, time
module_path, repo, shell, state_root, ready, release = sys.argv[1:7]
for entry in (repo, shell):
    if entry not in sys.path:
        sys.path.insert(0, entry)
spec = importlib.util.spec_from_file_location('knowledge_registry_holder', module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
registry = module.KnowledgeRegistry(state_root)
with registry._lease():
    open(ready, 'wb').close()
    deadline = time.monotonic() + 30
    while not os.path.exists(release) and time.monotonic() < deadline:
        time.sleep(0.02)
'''


def binding(workspace: str = WORKSPACE, task_uid: str = TASK_UID, revision: int = 2) -> dict:
    return {'workspace_uid': workspace, 'task_uid': task_uid, 'task_id': 'T-0033',
            'task_revision': revision}


class KnowledgeRegistryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state_root = self.base / 'StateRoot'
        self.vault_root = self.base / '개인 볼트'
        (self.vault_root / '업무').mkdir(parents=True)
        self.document = self.vault_root / '업무' / '검토.md'
        self.document.write_bytes(BODY.encode('utf-8'))
        self.registry = KnowledgeRegistry(self.state_root)
        self.knowledge = self.state_root / 'knowledge'
        self.vaults_file = self.knowledge / 'vaults.json'

    # helpers -------------------------------------------------------------

    def _vault_id(self) -> str:
        return self.registry.add_vault(self.vault_root)['vault_id']

    def _digest(self) -> str:
        return hashlib.sha256(self.document.read_bytes()).hexdigest()

    def _task_file(self, workspace: str = WORKSPACE, task_uid: str = TASK_UID) -> Path:
        return self.knowledge / 'tasks' / workspace / f'{task_uid}.json'

    def _pin(self, **overrides) -> dict:
        request = {'binding': binding(), 'vault_id': self._vault_id(),
                   'document_path': '업무/검토.md', 'start_line': 1, 'end_line': 3,
                   'expected_sha256': self._digest(), 'reason': '설계 근거'}
        request.update(overrides)
        return self.registry.pin_reference(request['binding'], request['vault_id'],
                                           request['document_path'], request['start_line'],
                                           request['end_line'], request['expected_sha256'],
                                           request['reason'])

    # vault identity ------------------------------------------------------

    def test_added_vault_is_local_only_labelled_by_basename_without_root(self):
        vault = self.registry.add_vault(self.vault_root)
        self.assertEqual(vault['label'], '개인 볼트')
        self.assertEqual(set(vault), {'vault_id', 'label'})
        status = self.registry.status()
        self.assertTrue(status['local_only'])
        self.assertEqual(status['vaults'], [vault])
        self.assertNotIn(str(self.vault_root), json.dumps(status, ensure_ascii=False))

    def test_same_canonical_root_is_deduped_across_spelling_variants(self):
        first = self.registry.add_vault(self.vault_root)
        for variant in (str(self.vault_root) + os.sep, Path(os.path.abspath(self.vault_root)),
                        str(self.vault_root).swapcase() if os.name == 'nt' else str(self.vault_root)):
            self.assertEqual(self.registry.add_vault(variant), first)
        stored = json.loads(self.vaults_file.read_text(encoding='utf-8'))
        self.assertEqual(len(stored['vaults']), 1)
        self.assertTrue(stored['local_only'])
        self.assertEqual(stored['schema_version'], 1)

    def test_missing_file_and_empty_vault_roots_are_refused(self):
        for candidate, code in ((self.base / 'absent', 'vault_unavailable'),
                                (self.document, 'vault_unavailable'),
                                ('', 'invalid_vault_root'), (None, 'invalid_vault_root'),
                                (7, 'invalid_vault_root')):
            with self.subTest(candidate=candidate):
                with self.assertRaises(KnowledgeRegistryError) as caught:
                    self.registry.add_vault(candidate)
                self.assertEqual(caught.exception.code, code)
        self.assertFalse(self.vaults_file.exists())

    def test_linked_vault_root_is_refused(self):
        link = self.base / 'linked-vault'
        try:
            link.symlink_to(self.vault_root, target_is_directory=True)
        except OSError:
            self.skipTest('Host does not permit creating directory symlinks')
        with self.assertRaises(KnowledgeRegistryError) as caught:
            self.registry.add_vault(link)
        self.assertEqual(caught.exception.code, 'linked_path_refused')

    def test_reparse_attribute_refused_without_link_privileges(self):
        metadata = self.vault_root.stat()
        fake = SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
        with patch('pathlib.Path.lstat', return_value=fake):
            with self.assertRaises(KnowledgeRegistryError) as caught:
                self.registry.add_vault(self.vault_root)
        self.assertEqual(caught.exception.code, 'linked_path_refused')

    def test_moved_vault_is_never_silently_followed(self):
        vault_id = self._vault_id()
        self.vault_root.rename(self.base / '옮긴 볼트')
        with self.assertRaises(KnowledgeRegistryError) as caught:
            self.registry.read_reference(binding(), vault_id, '업무/검토.md', 1, 3)
        self.assertEqual(caught.exception.code, 'vault_unavailable')
        self.assertNotIn(str(self.base), str(caught.exception))

    def test_unknown_vault_and_invalid_vault_id_are_refused_without_paths(self):
        self._vault_id()
        with self.assertRaises(KnowledgeRegistryError) as unknown:
            self.registry.read_reference(binding(), 'a' * 32, '업무/검토.md', 1, 3)
        self.assertEqual(unknown.exception.code, 'unknown_vault')
        self.assertNotIn(str(self.vault_root), str(unknown.exception))
        with self.assertRaises(KnowledgeRegistryError) as invalid:
            self.registry.read_reference(binding(), 'NOT-A-VAULT', '업무/검토.md', 1, 3)
        self.assertEqual(invalid.exception.code, 'invalid_vault_id')

    # binding identity ----------------------------------------------------

    def test_binding_shape_and_identity_are_validated(self):
        vault_id = self._vault_id()
        cases = [
            ({'workspace_uid': WORKSPACE, 'task_uid': TASK_UID, 'task_id': 'T-1'}, 'invalid_binding'),
            ({**binding(), 'extra': 1}, 'invalid_binding'),
            ('binding', 'invalid_binding'),
            (binding(workspace='not-a-uuid'), 'invalid_task_identity'),
            (binding(task_uid='00000000-0000-0000-0000-000000000000'), 'invalid_task_identity'),
            (binding(revision=True), 'invalid_task_revision'),
            (binding(revision=-1), 'invalid_task_revision'),
            ({**binding(), 'task_id': ''}, 'invalid_task_identity'),
        ]
        for candidate, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(KnowledgeRegistryError) as caught:
                    self.registry.read_reference(candidate, vault_id, '업무/검토.md', 1, 3)
                self.assertEqual(caught.exception.code, code)

    # reads ---------------------------------------------------------------

    def test_read_reference_reuses_core_reader_and_never_writes(self):
        vault_id = self._vault_id()
        result = self.registry.read_reference(binding(), vault_id, '업무/검토.md', 2, 2)
        self.assertEqual(result['excerpt'], '원문 근거')
        self.assertEqual(result['provider'], 'markdown-vault')
        self.assertEqual(result['source_sha256'], self._digest())
        self.assertEqual(result['freshness'], 'uncompared')
        self.assertTrue(result['read_only'])
        self.assertEqual(self.document.read_bytes(), BODY.encode('utf-8'))
        self.assertFalse(self._task_file().exists())
        self.assertEqual(self.registry.list_references(binding()), [])

    def test_read_reference_reports_changed_source_revision(self):
        vault_id = self._vault_id()
        original = self._digest()
        self.assertEqual(self.registry.read_reference(binding(), vault_id, '업무/검토.md', 1, 3,
                                                      original)['freshness'], 'unchanged')
        self.document.write_text('# 개정\n새 근거\n', encoding='utf-8')
        changed = self.registry.read_reference(binding(), vault_id, '업무/검토.md', 1, 2, original)
        self.assertEqual(changed['freshness'], 'changed')
        self.assertEqual(changed['expected_sha256'], original)

    def test_oversize_and_invalid_documents_are_refused(self):
        vault_id = self._vault_id()
        (self.vault_root / 'huge.md').write_bytes(b'# big\n' + b'x' * (512 * 1024))
        for path, code in (('huge.md', 'document_too_large'), ('업무/없음.md', 'document_missing'),
                           ('업무/검토.txt', 'markdown_required'), ('../탈출.md', 'invalid_document_path')):
            with self.subTest(path=path):
                with self.assertRaises(KnowledgeRegistryError) as caught:
                    self.registry.read_reference(binding(), vault_id, path, 1, 3)
                self.assertEqual(caught.exception.code, code)

    # pin and unpin -------------------------------------------------------

    def test_pin_saves_bounded_metadata_only_and_never_the_excerpt(self):
        saved = self._pin()
        self.assertEqual(set(saved), {'reference_id', 'vault_id', 'document_path', 'start_line',
                                      'end_line', 'source_sha256', 'reason'})
        self.assertEqual(saved['document_path'], '업무/검토.md')
        self.assertEqual((saved['start_line'], saved['end_line']), (1, 3))
        self.assertEqual(saved['source_sha256'], self._digest())
        stored = self._task_file().read_text(encoding='utf-8')
        self.assertNotIn('원문 근거', stored)
        self.assertNotIn(str(self.vault_root), stored)
        document = json.loads(stored)
        self.assertEqual(set(document), {'schema_version', 'local_only', 'workspace_uid',
                                         'task_uid', 'references'})
        self.assertTrue(document['local_only'])
        self.assertNotIn('task_revision', stored)
        self.assertNotIn('T-0033', stored)
        self.assertEqual(document['references'], [saved])
        self.assertEqual(KnowledgeRegistry(self.state_root).list_references(binding()), [saved])

    def test_pin_is_idempotent_for_the_same_vault_path_and_clamped_span(self):
        first = self._pin(end_line=40)
        self.assertEqual(first['end_line'], 3)
        second = self._pin(end_line=40, reason='근거 보강')
        self.assertEqual(second['reference_id'], first['reference_id'])
        self.assertEqual(second['reason'], '근거 보강')
        self.assertEqual(len(self.registry.list_references(binding())), 1)

    def test_pin_refuses_when_the_source_revision_changed(self):
        original = self._digest()
        saved = self._pin()
        self.document.write_text('# 개정\n새 근거\n', encoding='utf-8')
        with self.assertRaises(KnowledgeRegistryError) as caught:
            self._pin(start_line=1, end_line=2, expected_sha256=original)
        self.assertEqual(caught.exception.code, 'source_revision_conflict')
        self.assertEqual(self.registry.list_references(binding()), [saved])

    def test_pin_requires_a_bounded_reason_and_an_exact_expected_revision(self):
        vault_id = self._vault_id()
        for reason, expected, code in (('x' * 501, self._digest(), 'invalid_reason'),
                                       (None, self._digest(), 'invalid_reason'),
                                       ('근거', None, 'invalid_source_revision'),
                                       ('근거', 'zz', 'invalid_source_revision')):
            with self.subTest(code=code):
                with self.assertRaises(KnowledgeRegistryError) as caught:
                    self.registry.pin_reference(binding(), vault_id, '업무/검토.md', 1, 3, expected, reason)
                self.assertEqual(caught.exception.code, code)
        accepted = self._pin(reason='x' * 500)
        self.assertEqual(len(accepted['reason']), 500)

    def test_unpin_removes_once_and_is_idempotent_when_absent(self):
        saved = self._pin()
        self.assertTrue(self.registry.unpin_reference(binding(), saved['reference_id']))
        self.assertEqual(self.registry.list_references(binding()), [])
        self.assertFalse(self.registry.unpin_reference(binding(), saved['reference_id']))
        with self.assertRaises(KnowledgeRegistryError) as caught:
            self.registry.unpin_reference(binding(), 'not-a-uuid')
        self.assertEqual(caught.exception.code, 'invalid_reference_id')

    # isolation -----------------------------------------------------------

    def test_references_are_partitioned_by_workspace_and_task_uid(self):
        vault_id = self._vault_id()
        mine = self._pin()
        other_task = binding(task_uid=OTHER_TASK_UID)
        other_workspace = binding(workspace=OTHER_WORKSPACE)
        for scope in (other_task, other_workspace):
            self.assertEqual(self.registry.list_references(scope), [])
            self.assertFalse(self.registry.unpin_reference(scope, mine['reference_id']))
        self.assertEqual(self.registry.list_references(binding()), [mine])
        theirs = self.registry.pin_reference(other_task, vault_id, '업무/검토.md', 1, 3,
                                             self._digest(), '다른 과제')
        self.assertNotEqual(theirs['reference_id'], mine['reference_id'])
        self.assertEqual(self.registry.list_references(other_task), [theirs])
        self.assertEqual(self.registry.list_references(binding()), [mine])
        self.assertTrue(self._task_file(task_uid=OTHER_TASK_UID).is_file())

    def test_a_reference_file_copied_into_another_partition_is_refused(self):
        self._pin()
        target = self._task_file(task_uid=OTHER_TASK_UID)
        target.write_bytes(self._task_file().read_bytes())
        with self.assertRaises(KnowledgeRegistryError) as caught:
            self.registry.list_references(binding(task_uid=OTHER_TASK_UID))
        self.assertEqual(caught.exception.code, 'registry_corrupt')

    # registry path boundaries --------------------------------------------

    def _junction(self, link: Path, target: Path) -> None:
        """Plant a real directory junction, or skip where the host refuses one.

        A junction is the interesting case on Windows: creating one needs no
        privilege, so it is the redirection a user can actually be tricked into
        having on disk.
        """

        if os.name == 'nt':
            completed = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                       capture_output=True)
            if completed.returncode != 0 or not link.exists():
                self.skipTest('Host does not permit creating directory junctions')
        else:
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest('Host does not permit creating directory symlinks')
        # Drop the link before the temporary directory is cleaned up so no
        # cleanup can ever walk through it into the target.
        self.addCleanup(self._drop_link, link)

    def _drop_link(self, link: Path) -> None:
        try:
            os.rmdir(link)
        except OSError:
            pass

    def _assert_untouched(self, target: Path) -> None:
        self.assertEqual(sorted(str(path.relative_to(target)) for path in target.rglob('*')), [])

    def _assert_all_operations_refuse(self, target: Path) -> None:
        operations = {
            'status': self.registry.status,
            'add_vault': lambda: self.registry.add_vault(self.vault_root),
            'list_references': lambda: self.registry.list_references(binding()),
            'pin_reference': self._pin,
            'unpin_reference': lambda: self.registry.unpin_reference(binding(), ABSENT_REFERENCE),
            'lease': lambda: self.registry._lease().__enter__(),
        }
        for name, call in operations.items():
            with self.subTest(operation=name):
                with self.assertRaises(KnowledgeRegistryError) as caught:
                    call()
                self.assertEqual(caught.exception.code, 'linked_path_refused')
                self.assertNotIn(str(target), str(caught.exception))
        self._assert_untouched(target)

    def test_precreated_knowledge_junction_is_refused_by_admission_and_by_every_operation(self):
        outside = self.base / 'outside'
        outside.mkdir()
        self.state_root.mkdir(parents=True)
        # The registry in setUp was constructed before the link existed: this
        # is the race where the link is planted after admission passed.
        self._junction(self.knowledge, outside)
        with self.assertRaises(KnowledgeRegistryError) as caught:
            KnowledgeRegistry(self.state_root)
        self.assertEqual(caught.exception.code, 'linked_path_refused')
        self._assert_all_operations_refuse(outside)

    def test_linked_tasks_directory_is_refused_before_any_partition_write(self):
        self._vault_id()
        outside = self.base / 'outside-tasks'
        outside.mkdir()
        self._junction(self.knowledge / 'tasks', outside)
        for call in (self._pin, lambda: self.registry.list_references(binding()),
                     lambda: self.registry.unpin_reference(binding(), ABSENT_REFERENCE)):
            with self.assertRaises(KnowledgeRegistryError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'linked_path_refused')
        self._assert_untouched(outside)
        self.assertTrue(self.vaults_file.is_file())

    def test_linked_workspace_partition_is_refused_without_touching_other_tasks(self):
        vault_id = self._vault_id()
        other = self.registry.pin_reference(binding(workspace=OTHER_WORKSPACE), vault_id,
                                            '업무/검토.md', 1, 3, self._digest(), '다른 작업공간')
        outside = self.base / 'outside-workspace'
        outside.mkdir()
        self._junction(self.knowledge / 'tasks' / WORKSPACE, outside)
        for call in (self._pin, lambda: self.registry.list_references(binding())):
            with self.assertRaises(KnowledgeRegistryError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'linked_path_refused')
        self._assert_untouched(outside)
        self.assertEqual(self.registry.list_references(binding(workspace=OTHER_WORKSPACE)), [other])

    def test_directory_turned_into_a_junction_between_check_and_create_is_refused(self):
        outside = self.base / 'outside-race'
        outside.mkdir()
        self.state_root.mkdir(parents=True)
        original = Path.mkdir
        planted: list = []

        def racing_mkdir(self_path, *arguments, **keywords):
            # A concurrent process wins the create and leaves a junction behind.
            if self_path == self.knowledge and not planted:
                planted.append(True)
                self._junction(self.knowledge, outside)
                return None
            return original(self_path, *arguments, **keywords)

        with patch.object(Path, 'mkdir', racing_mkdir):
            with self.assertRaises(KnowledgeRegistryError) as caught:
                self.registry.add_vault(self.vault_root)
        self.assertEqual(caught.exception.code, 'linked_path_refused')
        self.assertTrue(planted)
        self._assert_untouched(outside)

    def test_lease_refuses_a_handle_that_is_not_the_entry_that_was_checked(self):
        self._vault_id()
        before = self.vaults_file.read_bytes()
        swapped = SimpleNamespace(st_dev=-1, st_ino=-1)
        with patch.object(MODULE.os, 'fstat', return_value=swapped):
            with self.assertRaises(KnowledgeRegistryError) as caught:
                self.registry.add_vault(self.base)
        self.assertEqual(caught.exception.code, 'linked_path_refused')
        self.assertEqual(self.vaults_file.read_bytes(), before)

    def test_reparse_attribute_on_the_registry_directory_is_refused_without_link_privileges(self):
        self.state_root.mkdir(parents=True)
        self.knowledge.mkdir()
        real = Path.lstat

        def reparse_knowledge(self_path):
            metadata = real(self_path)
            if self_path == self.knowledge:
                return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
            return metadata

        with patch.object(Path, 'lstat', reparse_knowledge):
            for call in (self.registry.status, lambda: self.registry.add_vault(self.vault_root)):
                with self.assertRaises(KnowledgeRegistryError) as caught:
                    call()
                self.assertEqual(caught.exception.code, 'linked_path_refused')
        self.assertFalse(self.vaults_file.exists())

    def _attempt_junction_swap(self, link: Path, outside: Path) -> bool:
        """Really try to replace a live registry directory with a junction.

        This is the mutation the fix has to survive rather than detect: a link
        can only take the name once the directory is gone, so a bound directory
        makes the removal itself fail.  ``True`` reports that the swap did
        happen, which is the branch where the write has to refuse instead.
        """

        try:
            os.rmdir(link)
        except OSError:
            return False
        if os.name == 'nt':
            completed = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                       capture_output=True)
            swapped = completed.returncode == 0
        else:
            try:
                link.symlink_to(outside, target_is_directory=True)
                swapped = True
            except OSError:
                swapped = False
        if swapped:
            self.addCleanup(self._drop_link, link)
        else:
            link.mkdir(exist_ok=True)
        return swapped

    def _assert_bound_or_refused(self, call, swapped: list, landed: Path) -> None:
        """Either the binding held the directory, or the write refused it.

        Both outcomes are honest: on a host that pins directories the swap is
        the thing that fails, and where the directory can still be removed the
        operation must fail closed.  What is never acceptable is a write that
        succeeds through the swapped name.
        """

        try:
            call()
        except KnowledgeRegistryError as error:
            refused = error.code
        else:
            refused = None
        self.assertTrue(swapped, 'the racing junction attempt never ran')
        if os.name == 'nt':
            # A bound directory cannot be removed, so the swap loses outright.
            self.assertFalse(swapped[0], 'the registry directory was swapped anyway')
        if swapped[0]:
            self.assertIn(refused, {'registry_unwritable', 'linked_path_refused'})
        else:
            self.assertIsNone(refused)
            self.assertTrue(landed.is_file())

    def _racing_entry(self, wanted: str, link: Path, outside: Path, swapped: list):
        """Attempt the swap in the window between the bound chain and the open."""

        original = MODULE_PATHS.BOUND_DIRECTORY.entry

        def racing(bound, name):
            if name == wanted and not swapped:
                swapped.append(self._attempt_junction_swap(link, outside))
            return original(bound, name)

        return patch.object(MODULE_PATHS.BOUND_DIRECTORY, 'entry', racing)

    def test_the_lease_cannot_be_created_through_a_swapped_registry_directory(self):
        # Window one: the chain is created and proved, and the lease file has
        # not been opened yet -- where registry.lock used to land outside.
        outside = self.base / 'outside-lease'
        outside.mkdir()
        swapped: list = []
        with self._racing_entry(MODULE.LEASE_FILE, self.knowledge, outside, swapped):
            self._assert_bound_or_refused(lambda: self.registry.add_vault(self.vault_root),
                                          swapped, self.vaults_file)
        self._assert_untouched(outside)

    def test_a_task_document_cannot_land_through_a_swapped_workspace_partition(self):
        # Window two: the partition is created and proved, and the temporary
        # file the replace renames has not been opened yet.
        self._vault_id()
        outside = self.base / 'outside-partition'
        outside.mkdir()
        swapped: list = []
        partition = self.knowledge / 'tasks' / WORKSPACE
        with self._racing_entry(f'{TASK_UID}.json', partition, outside, swapped):
            self._assert_bound_or_refused(self._pin, swapped, self._task_file())
        self._assert_untouched(outside)

    def test_a_bound_directory_can_be_neither_renamed_nor_removed(self):
        # The mechanism itself, with nothing else holding the directory: the
        # same two calls succeed once the binding is released.
        if os.name != 'nt':
            self.skipTest('POSIX binds a descriptor rather than denying the rename')
        self.state_root.mkdir(parents=True)
        with MODULE_PATHS.bound_chain(self.state_root, self.knowledge, create=True) as bound:
            self.assertIsNotNone(bound)
            for mutation in (lambda: os.rename(self.knowledge, self.base / 'moved-knowledge'),
                             lambda: os.rmdir(self.knowledge)):
                with self.assertRaises(OSError):
                    mutation()
        os.rmdir(self.knowledge)

    def test_a_host_that_cannot_bind_directories_is_refused_not_trusted(self):
        self._vault_id()
        before = self.vaults_file.read_bytes()
        with patch.object(MODULE_PATHS, 'BINDING_SUPPORTED', False):
            for call in (self.registry.status, lambda: self.registry.add_vault(self.vault_root),
                         lambda: self.registry.list_references(binding()), self._pin):
                with self.assertRaises(KnowledgeRegistryError) as caught:
                    call()
                self.assertEqual(caught.exception.code, 'path_binding_unsupported')
        self.assertEqual(self.vaults_file.read_bytes(), before)

    # storage integrity ---------------------------------------------------

    def test_corrupt_registry_is_refused_and_never_reset_or_overwritten(self):
        self._vault_id()
        for payload in (b'{ not json', b'[]',
                        b'{"schema_version":1,"local_only":true,"vaults":[],"extra":1}',
                        b'{"schema_version":2,"local_only":true,"vaults":[]}',
                        b'{"schema_version":1,"local_only":false,"vaults":[]}',
                        b'{"schema_version":1,"local_only":true,"vaults":[{"vault_id":"x"}]}'):
            with self.subTest(payload=payload[:24]):
                self.vaults_file.write_bytes(payload)
                for call in (self.registry.status, lambda: self.registry.add_vault(self.vault_root)):
                    with self.assertRaises(KnowledgeRegistryError) as caught:
                        call()
                    self.assertEqual(caught.exception.code, 'registry_corrupt')
                self.assertEqual(self.vaults_file.read_bytes(), payload)

    def test_corrupt_reference_entry_is_refused_without_dropping_the_file(self):
        saved = self._pin()
        document = json.loads(self._task_file().read_text(encoding='utf-8'))
        document['references'][0]['unexpected'] = True
        payload = json.dumps(document, ensure_ascii=True).encode('utf-8')
        self._task_file().write_bytes(payload)
        for call in (lambda: self.registry.list_references(binding()),
                     lambda: self.registry.unpin_reference(binding(), saved['reference_id']),
                     self._pin):
            with self.assertRaises(KnowledgeRegistryError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'registry_corrupt')
        self.assertEqual(self._task_file().read_bytes(), payload)

    def test_oversize_registry_document_is_refused_and_left_untouched(self):
        self._vault_id()
        payload = b'{"schema_version":1,"local_only":true,"vaults":[],"pad":"' + b'x' * (256 * 1024) + b'"}'
        self.vaults_file.write_bytes(payload)
        with self.assertRaises(KnowledgeRegistryError) as caught:
            self.registry.status()
        self.assertEqual(caught.exception.code, 'registry_too_large')
        self.assertEqual(self.vaults_file.read_bytes(), payload)

    def test_failed_replace_keeps_the_previous_document_and_no_temporary_file(self):
        saved = self._pin()
        before = self._task_file().read_bytes()
        with patch.object(MODULE.os, 'replace', side_effect=OSError('denied')):
            with self.assertRaises(KnowledgeRegistryError) as caught:
                self.registry.unpin_reference(binding(), saved['reference_id'])
        self.assertEqual(caught.exception.code, 'registry_unwritable')
        self.assertEqual(self._task_file().read_bytes(), before)
        self.assertEqual(self.registry.list_references(binding()), [saved])
        leftovers = [path.name for path in self.knowledge.rglob('*.tmp')]
        self.assertEqual(leftovers, [])

    def test_registry_directory_is_not_created_by_reads(self):
        self.assertEqual(self.registry.status(), {'vaults': [], 'local_only': True})
        self.assertEqual(self.registry.list_references(binding()), [])
        self.assertFalse(self.knowledge.exists())

    def test_invalid_state_root_and_lease_timeout_are_refused(self):
        for arguments, code in ((('',), 'invalid_state_root'), ((None,), 'invalid_state_root'),
                                ((str(self.state_root), -1), 'invalid_lease_timeout'),
                                ((str(self.state_root), 'soon'), 'invalid_lease_timeout')):
            with self.subTest(code=code):
                with self.assertRaises(KnowledgeRegistryError) as caught:
                    KnowledgeRegistry(*arguments)
                self.assertEqual(caught.exception.code, code)

    # interprocess lease --------------------------------------------------

    def test_write_lease_is_held_across_processes(self):
        self._vault_id()
        script = self.base / 'lease_holder.py'
        script.write_text(LEASE_HOLDER, encoding='utf-8')
        ready, release = self.base / 'ready', self.base / 'release'
        child = subprocess.Popen([sys.executable, str(script), str(MODULE_PATH), str(ROOT),
                                  str(SHELL), str(self.state_root), str(ready), str(release)])
        self.addCleanup(child.kill)
        deadline = time.monotonic() + 30
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), 'lease holder did not start')
        blocked = KnowledgeRegistry(self.state_root, lease_timeout=0.3)
        with self.assertRaises(KnowledgeRegistryError) as caught:
            blocked.add_vault(self.base)
        self.assertEqual(caught.exception.code, 'registry_busy')
        release.write_bytes(b'')
        self.assertEqual(child.wait(timeout=30), 0)
        self.assertEqual(blocked.add_vault(self.base)['label'], self.base.name)


if __name__ == '__main__':
    unittest.main()
