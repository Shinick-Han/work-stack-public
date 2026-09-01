from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.domain_v4_composition import (
    V4DomainCompositionError,
    compose_experimental_v4_domain,
)
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority


NOW = "2026-09-01T12:00:00Z"


def _write_conversion(root: Path, conversion) -> None:
    def write(relative: str, body: bytes) -> None:
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    write("store.json", canonical_json_bytes(dict(conversion.store)))
    write("workspace.json", canonical_json_bytes(dict(conversion.workspace)))
    for kind, records in conversion.records.items():
        for record in records:
            uid = str(record["uid"])
            write(
                f"records/{kind}/{uid[:2]}/{uid}.json",
                canonical_json_bytes(dict(record)),
            )
    segments: dict[tuple[str, str], list[dict]] = {}
    for kind, events in conversion.streams.items():
        for event in events:
            segments.setdefault((kind, str(event["created_at"])[:7]), []).append(
                dict(event)
            )
    for (kind, month), events in sorted(segments.items()):
        body = b"".join(
            canonical_json_bytes(event) + b"\n"
            for event in sorted(events, key=lambda item: item["sequence"])
        )
        write(f"streams/{kind}/{month}.ndjson", body)


class ExperimentalV4DomainCompositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        legacy = WorkStack(Store(self.base / "v3"))
        with mock.patch("workstack.service.utc_now", return_value=NOW), mock.patch(
            "workstack.service.today", return_value=NOW[:10]
        ):
            legacy.add_task("Composition boundary")
        documents = {name: legacy.store.load(name) for name in DEFAULTS}
        self.conversion = convert_v3_documents(
            documents, candidate_created_at=NOW
        )
        self.authority = self.base / "authority"
        self.authority.mkdir()
        _write_conversion(self.authority, self.conversion)
        self.runtime = resolve_runtime_authority(
            self.authority,
            self.base / "runtime",
            str(self.conversion.store["workspace_uid"]),
        )
        self.runtime.runtime_root.mkdir(parents=True)
        manifest = build_v4_manifest(read_v4(self.authority), generation=0)
        publish_runtime_manifest(
            self.runtime.manifest_path, manifest, expected_digest=None
        )
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _compose(self):
        return compose_experimental_v4_domain(
            self.authority,
            self.runtime,
            enable_v4_domain=True,
            clock=lambda: NOW,
            uid_factory=lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            task_note_source_indexes=self.conversion.task_note_source_indexes,
        )

    def test_default_off_refuses_before_admission_or_filesystem_touch(self) -> None:
        missing_authority = self.base / "never-created-authority"
        missing_runtime = resolve_runtime_authority(
            missing_authority,
            self.base / "never-created-runtime",
            str(self.conversion.store["workspace_uid"]),
        )
        with mock.patch(
            "workstack.storage.domain_v4_composition."
            "admit_experimental_v4_mutation_repository"
        ) as admission:
            with self.assertRaises(V4DomainCompositionError) as caught:
                compose_experimental_v4_domain(
                    missing_authority,
                    missing_runtime,
                    clock=lambda: NOW,
                )
        self.assertEqual("V4_DOMAIN_OPT_IN_REQUIRED", caught.exception.code)
        admission.assert_not_called()
        self.assertFalse(missing_authority.exists())
        self.assertFalse(missing_runtime.runtime_root.exists())

    def test_every_backend_shares_one_exact_admitted_coordinate(self) -> None:
        domain = self._compose()
        coordinate = domain.coordinate
        self.assertEqual(self.authority.resolve(), coordinate.authority_root)
        self.assertEqual(self.runtime.runtime_root, coordinate.runtime_root)
        self.assertEqual(self.runtime.workspace_uid, coordinate.workspace_uid)
        self.assertEqual(0, coordinate.generation)
        self.assertIs(self.runtime, domain.admission.runtime)
        self.assertIs(domain.admission, domain.relationships.session)
        for backend in (
            domain.capture_reply,
            domain.tasks,
            domain.planning,
        ):
            self.assertEqual(coordinate.authority_root, backend.authority_root)
            self.assertIs(self.runtime, backend.runtime)
        for backend in (
            domain.intents,
            domain.objectives,
            domain.work_sessions,
        ):
            self.assertIs(self.runtime, backend._runtime)
        self.assertEqual(coordinate.authority_root, domain.query.repository._root)
        self.assertEqual(coordinate.generation, domain.query.repository._generation)
        self.assertEqual(coordinate, domain.assert_fresh())

    def test_missing_admission_and_mixed_authority_are_refused(self) -> None:
        with self.assertRaises(V4DomainCompositionError) as missing:
            compose_experimental_v4_domain(
                self.authority,
                None,
                enable_v4_domain=True,
                clock=lambda: NOW,
            )
        self.assertEqual("RUNTIME_AUTHORITY_REQUIRED", missing.exception.code)

        other = self.base / "other-authority"
        mixed = resolve_runtime_authority(
            other,
            self.base / "runtime",
            self.runtime.workspace_uid,
        )
        with self.assertRaises(V4DomainCompositionError) as mismatch:
            compose_experimental_v4_domain(
                self.authority,
                mixed,
                enable_v4_domain=True,
                clock=lambda: NOW,
            )
        self.assertEqual("RUNTIME_AUTHORITY_MISMATCH", mismatch.exception.code)

    def test_stale_authority_is_refused_and_composed_domain_detects_drift(self) -> None:
        domain = self._compose()
        workspace_path = self.authority / "workspace.json"
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
        workspace["name"] = "Changed outside admitted generation"
        workspace_path.write_bytes(canonical_json_bytes(workspace))

        with self.assertRaises(V4DomainCompositionError) as drift:
            domain.assert_fresh()
        self.assertEqual("V4_DOMAIN_AUTHORITY_STALE", drift.exception.code)
        with self.assertRaises(V4DomainCompositionError) as stale:
            self._compose()
        self.assertEqual("RUNTIME_MANIFEST_STALE", stale.exception.code)


if __name__ == "__main__":
    unittest.main()
