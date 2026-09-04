"""T-0008 contract: local profile authority must survive restart, and a genuine
workspace replacement must require an explicit confirmed rebind before the
selected profile admits it.

The boundary under test is the seam between three existing surfaces:

* ``workstack.store.Store.workspace_rebind_preview`` /
  ``Store.rebind_workspace_identity`` own the explicit, user-confirmed identity
  rebind of the Store itself;
* ``WorkStackDesktopHost._handle_ssot_message`` owns the ``rebind-start`` and
  ``rebind-complete`` coordination the frontend drives after that confirmation;
  and
* ``connection_registry_startup.ensure_connection_registry`` /
  ``select_active_profile_for_startup`` own which authority the desktop host
  selects on the next start.

None of those three is mocked or replaced. A real Store is created on disk and
registered as the active local profile, and the host messages are delivered to
the real handler, so a failure in this module is a statement about the product
seam rather than about a fixture. Only GUI rendering (the WebView2 post target)
and the ``webview`` import are stubbed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store, StoreExternalChangeError

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
MODULE_PATH = SHELL / "connection_registry_startup.py"
SPEC = importlib.util.spec_from_file_location(
    "connection_registry_startup_rebind_restart_test", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

DESKTOP_PATH = SHELL / "workstack_desktop.py"
DESKTOP_SPEC = importlib.util.spec_from_file_location(
    "workstack_desktop_rebind_restart_test", DESKTOP_PATH
)
assert DESKTOP_SPEC is not None and DESKTOP_SPEC.loader is not None
DESKTOP = importlib.util.module_from_spec(DESKTOP_SPEC)
sys.modules[DESKTOP_SPEC.name] = DESKTOP
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    DESKTOP_SPEC.loader.exec_module(DESKTOP)

IDEMPOTENCY_KEY = "workspace.rebind.restart.0001"
SSOT_HOST_PREFIX = "workstack-ssot-host"


class LocalRegistryRebindRestartContractTest(unittest.TestCase):
    """Selected authority stability across restart and explicit rebind."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.state_root = self.base / "state"
        self.state_root.mkdir()
        self.data_dir = self.base / "configured-ssot"
        self.runtime = self.base / "authority-runtime"
        self.environment = mock.patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": str(self.runtime)}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temporary.cleanup)

        # Containment guard. Store.__init__ mkdir's both its data root and its
        # runtime root (workstack/store.py:622-644), so every destination must
        # be proven inside the disposable temp root BEFORE it is constructed.
        fixture_root = Path(tempfile.gettempdir()).resolve()
        for target in (self.base, self.data_dir, self.runtime, self.state_root):
            resolved = Path(target).resolve()
            self.assertTrue(
                resolved == fixture_root or fixture_root in resolved.parents,
                f"fixture path escapes the disposable temp root: {resolved}",
            )

        self.store = Store(self.data_dir)
        self.stack = WorkStack(self.store)
        self.stack.add_task("Selected authority planning state")
        self.workspace_a = str(self.stack.workspace_projection()["workspace"]["id"])

        # Fixture guard: a missing Store file here would make every assertion
        # below a fixture error rather than a contract result.
        for name in MODULE.MINIMUM_LOCAL_STORE_FILES:
            self.assertTrue(
                (self.data_dir / name).is_file(),
                f"fixture Store is incomplete: {name} is missing",
            )

        # The product derives this from the state root, not from a build
        # version; see connection_registry_startup installation identity usage.
        self.installation_identity = str(self.state_root).casefold()
        # The runtime config the real startup path reads. data_dir is the
        # divergence-prone field; the registry, not this file, must decide.
        (self.state_root / "config.json").write_text(
            json.dumps({"data_dir": str(self.data_dir.resolve()), "port": 8765}),
            encoding="utf-8",
        )
        registry = self.ensure_registry()
        self.profile_a = registry.profiles[0]
        self.assertEqual(self.profile_a.expected_workspace_id, self.workspace_a)

    # -- helpers ---------------------------------------------------------

    def ensure_registry(self):
        """Run the startup path that a desktop start would run."""

        return MODULE.ensure_connection_registry(
            self.state_root,
            installation_identity=self.installation_identity,
            local_data_dir=str(self.data_dir.resolve()),
        )

    def restart_selection(self):
        """Simulate the next desktop start and return its typed selection."""

        self.ensure_registry()
        return MODULE.select_active_profile_for_startup(self.state_root)

    def active_profile(self):
        registry = MODULE.load_connection_registry(self.state_root)
        assert registry is not None
        active = [
            profile
            for profile in registry.profiles
            if profile.profile_id == registry.active_profile_id
        ]
        self.assertEqual(len(active), 1)
        return active[0]

    def registry_authority(self) -> tuple[str, str, str]:
        """The authority-bearing fields a rebind must not silently rewrite."""

        profile = self.active_profile()
        return (
            profile.profile_id,
            profile.expected_workspace_id,
            str(Path(profile.data_dir).resolve()),
        )

    def replace_configured_store_with_candidate(self) -> str:
        """Replace every authoritative byte in place, as an external actor would."""

        replacement_root = self.base / "replacement-source"
        replacement = Store(replacement_root)
        replacement_stack = WorkStack(replacement)
        replacement_stack.add_task("Replacement workspace planning state")
        candidate = str(replacement_stack.workspace_projection()["workspace"]["id"])
        self.assertNotEqual(candidate, self.workspace_a)
        for name in DEFAULTS:
            self.store.path(name).write_bytes(replacement.path(name).read_bytes())
        return candidate

    def local_host(self):
        """A desktop host whose registry runtime state comes from the real
        startup preparation path, not from hand-set attributes.

        ``_prepare_connection_registry_runtime`` is the product method that a
        real start runs; it populates ``connection_registry_snapshot``,
        ``connection_registry_digest``, ``runtime_connection_profile_id``,
        ``local_startup_selection``, ``active_connection_draft`` and
        ``remote_profile`` from the on-disk registry and the verified startup
        selection. Driving it here means any correct CAS completion has the
        exact state it would have in production.

        Only the WebView2 post target, the status dispatch and the ``webview``
        import are stubbed. The rebind handler, the registry mutation surface
        and startup identity verification are the real product code.
        """

        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.state_root = self.state_root
        host.install_root = self.state_root
        host.options = types.SimpleNamespace(url="")
        host.remote_authority_lock = threading.RLock()
        host.remote_rebind_target = ""
        host.remote_rebind_deadline = 0.0
        host.connection_registry_startup_enabled = True

        host._prepare_connection_registry_runtime()

        # Completeness check against the fields that method is documented to
        # set: a partially built fixture must fail here, not inside a contract
        # assertion.
        self.assertIsNotNone(host.connection_registry_snapshot)
        self.assertRegex(host.connection_registry_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(host.runtime_connection_profile_id, self.profile_a.profile_id)
        # The desktop module imports its own instance of the startup module, so
        # compare by class name rather than by this test's class object.
        self.assertEqual(
            type(host.local_startup_selection).__name__, "LocalStartupSelection"
        )
        self.assertEqual(host.local_startup_selection.data_dir, self.data_dir.resolve())
        self.assertEqual(
            host.local_startup_selection.expected_workspace_id, self.workspace_a
        )
        self.assertIsNone(host.remote_profile)
        self.assertEqual(host.active_connection_draft, {"storage_mode": "local"})

        host.core = mock.Mock()
        host.workstack_webview = types.SimpleNamespace(CoreWebView2=host.core)
        host.posted_status = mock.Mock()
        host._post_ssot_status = host.posted_status
        host._dispatch_ssot_status = host.posted_status
        return host

    def posted_rebind_ready(self, host) -> list[str]:
        payloads = []
        for call in host.core.PostWebMessageAsJson.call_args_list:
            document = json.loads(call.args[0])
            if document.get("type") == "workstack-ssot-rebind-ready":
                payloads.append(str(document.get("workspace_id")))
        return payloads

    def preview(self, candidate: str) -> dict[str, object]:
        preview = self.store.workspace_rebind_preview()
        self.assertEqual(preview["state"], "workspace-identity-mismatch")
        self.assertEqual(preview["manifest_workspace_id"], self.workspace_a)
        self.assertEqual(preview["candidate_workspace_id"], candidate)
        return preview

    def rebind(
        self,
        preview: dict[str, object],
        *,
        confirmed: bool = True,
        candidate_workspace_id: str | None = None,
        idempotency_key: str = IDEMPOTENCY_KEY,
    ) -> dict[str, object]:
        return self.store.rebind_workspace_identity(
            confirmed=confirmed,
            expected_manifest_workspace_id=str(preview["manifest_workspace_id"]),
            expected_candidate_workspace_id=(
                candidate_workspace_id or str(preview["candidate_workspace_id"])
            ),
            expected_manifest_digest=str(preview["manifest_digest"]),
            expected_candidate_digest=str(preview["candidate_digest"]),
            idempotency_key=idempotency_key,
        )

    # -- unchanged authority ---------------------------------------------

    def test_unchanged_authority_selection_survives_restart(self) -> None:
        """The positive case: nothing changed, so restart must change nothing."""

        before = self.registry_authority()
        selection = self.restart_selection()

        self.assertIsInstance(selection, MODULE.LocalStartupSelection)
        self.assertEqual(selection.expected_workspace_id, self.workspace_a)
        self.assertEqual(selection.data_dir, self.data_dir.resolve())
        self.assertEqual(selection.profile_id, self.profile_a.profile_id)
        self.assertEqual(self.registry_authority(), before)

    # -- replacement without confirmation ---------------------------------

    def test_replacement_without_confirmation_refuses_startup_without_rewriting_authority(
        self,
    ) -> None:
        candidate = self.replace_configured_store_with_candidate()
        before = self.registry_authority()

        self.assertEqual(self.store.sync_status()["state"], "invalid")
        with self.assertRaises(StoreExternalChangeError):
            self.stack.add_task("An unreviewed replacement must not accept writes")

        with self.assertRaises(RuntimeError) as refusal:
            self.restart_selection()
        self.assertIn("identity mismatch", str(refusal.exception).casefold())

        # The registry must still name the previously selected authority: an
        # unconfirmed replacement never rewrites the profile's expected UID.
        self.assertEqual(self.registry_authority(), before)
        self.assertEqual(self.active_profile().expected_workspace_id, self.workspace_a)
        self.assertNotEqual(self.active_profile().expected_workspace_id, candidate)

    def test_unconfirmed_or_wrong_rebind_refuses_without_authority_writes(self) -> None:
        candidate = self.replace_configured_store_with_candidate()
        preview = self.preview(candidate)
        before = self.registry_authority()

        with self.assertRaises(ValueError):
            self.rebind(preview, confirmed=False, idempotency_key="workspace.rebind.unconfirmed")

        wrong_candidate = "99999999-9999-4999-8999-999999999999"
        self.assertNotEqual(wrong_candidate, candidate)
        with self.assertRaises(StoreExternalChangeError):
            self.rebind(
                preview,
                candidate_workspace_id=wrong_candidate,
                idempotency_key="workspace.rebind.wrongcandidate",
            )

        self.assertEqual(self.store.sync_status()["state"], "invalid")
        self.assertEqual(self.store.sync_status()["workspace_id"], self.workspace_a)
        self.assertEqual(self.registry_authority(), before)

    # -- confirmed rebind --------------------------------------------------

    def test_confirmed_rebind_preserves_candidate_planning_bytes(self) -> None:
        candidate = self.replace_configured_store_with_candidate()
        preview = self.preview(candidate)
        before = {name: self.store.path(name).read_bytes() for name in sorted(DEFAULTS)}

        result = self.rebind(preview)

        self.assertEqual(result["state"], "in-sync")
        self.assertEqual(result["workspace_id"], candidate)
        self.assertFalse(result["recovery"]["planning_mutated"])
        after = {name: self.store.path(name).read_bytes() for name in sorted(DEFAULTS)}
        self.assertEqual(after, before)

    # -- host coordination around the confirmed rebind ---------------------

    def test_rebind_start_acknowledges_the_candidate_without_rewriting_authority(
        self,
    ) -> None:
        """Starting a rebind is not completing one.

        The acknowledgement must name the candidate the frontend asked about,
        and starting must not move the recorded authority: only a confirmed
        completion may do that. This deliberately says nothing about which
        internal fields the host uses to remember the in-flight rebind.
        """

        # The host starts on the selected authority, exactly as a running
        # desktop would, and only then is the store replaced underneath it.
        host = self.local_host()
        candidate = self.replace_configured_store_with_candidate()
        before = self.registry_authority()

        host._handle_ssot_message(f"{SSOT_HOST_PREFIX}|rebind-start|{candidate}")

        self.assertEqual(self.posted_rebind_ready(host), [candidate])
        self.assertEqual(self.registry_authority(), before)
        self.assertEqual(self.active_profile().expected_workspace_id, self.workspace_a)

    def test_non_canonical_completion_is_refused_without_authority_change(self) -> None:
        """Wrong completion evidence is refused by the existing handler."""

        host = self.local_host()
        before = self.registry_authority()

        host._handle_ssot_message(f"{SSOT_HOST_PREFIX}|rebind-complete|not-a-uuid")

        host.posted_status.assert_called_once()
        payload = host.posted_status.call_args.args[0]
        self.assertEqual(payload["state"], "error")
        self.assertIn("canonical UUID", str(payload["message"]))
        self.assertEqual(self.registry_authority(), before)

    def test_completion_without_a_matching_start_does_not_change_authority(self) -> None:
        """A canonical completion for a workspace that was never started, and
        that the Store never confirmed, must not move the recorded authority.

        The assertion is on the authority outcome, not on any particular
        refusal mechanism or exception type.
        """

        host = self.local_host()
        candidate = self.replace_configured_store_with_candidate()
        unrelated = "33333333-3333-4333-8333-333333333333"
        self.assertNotEqual(unrelated, candidate)
        self.assertNotEqual(unrelated, self.workspace_a)
        before = self.registry_authority()

        host._handle_ssot_message(f"{SSOT_HOST_PREFIX}|rebind-complete|{unrelated}")

        self.assertEqual(self.registry_authority(), before)
        self.assertEqual(self.store.sync_status()["workspace_id"], self.workspace_a)

    def test_confirmed_rebind_through_host_persists_identity_for_restart(self) -> None:
        """The full user path: confirm the Store rebind, let the host coordinate,
        then restart. The next start must select the rebound authority instead of
        refusing on the stale expected UID."""

        host = self.local_host()
        candidate = self.replace_configured_store_with_candidate()
        preview = self.preview(candidate)

        host._handle_ssot_message(f"{SSOT_HOST_PREFIX}|rebind-start|{candidate}")
        result = self.rebind(preview)
        self.assertEqual(result["state"], "in-sync")
        self.assertEqual(result["workspace_id"], candidate)
        host._handle_ssot_message(f"{SSOT_HOST_PREFIX}|rebind-complete|{candidate}")

        selection = self.restart_selection()

        self.assertIsInstance(selection, MODULE.LocalStartupSelection)
        self.assertEqual(selection.expected_workspace_id, candidate)
        self.assertEqual(selection.data_dir, self.data_dir.resolve())
        self.assertEqual(self.active_profile().expected_workspace_id, candidate)

    def test_confirmed_rebind_is_idempotent_for_the_same_key(self) -> None:
        candidate = self.replace_configured_store_with_candidate()
        preview = self.preview(candidate)

        first = self.rebind(preview)
        second = self.rebind(preview)

        self.assertEqual(second["state"], first["state"])
        self.assertEqual(second["workspace_id"], first["workspace_id"])
        self.assertEqual(self.store.sync_status()["workspace_id"], candidate)


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
