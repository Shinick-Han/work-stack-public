"""Atomic exclusive owner-receipt publication. Temporary directories only."""

from __future__ import annotations

import ast
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_owner as OWNER  # noqa: E402
import remote_receipt_guard as GUARD  # noqa: E402
import remote_receipt_io as IO  # noqa: E402
from remote_command_contract import token_hash  # noqa: E402


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OWN_TOKEN = "r5pending-token-not-enforced-01"
HOST_IDENTITY = "b" * 64
BOOT_IDENTITY = "c" * 64
PAYLOAD = b'{"workspace_id":"ok","pad":"' + (b"x" * 64) + b'"}\n'


def _receipt(data: Path, *, pid: int = 4242, start: str = "start-1") -> OWNER.OwnerReceipt:
    return OWNER.OwnerReceipt(
        workspace_id=WORKSPACE_ID,
        data_dir_digest=OWNER.local_path_digest(data),
        app_dir_digest="a" * 64,
        pid=pid,
        start_identity=start,
        release_id="1.0.7",
        token_hash=token_hash(OWN_TOKEN),
        host_identity=HOST_IDENTITY,
        boot_identity=BOOT_IDENTITY,
    )


class ReceiptPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.dest = self.data / OWNER.OWNER_FILENAME

    def test_chunked_writes_publish_the_complete_payload(self) -> None:
        real_write = os.write

        def chunked(fd: int, data: bytes | memoryview) -> int:
            view = memoryview(data)
            return real_write(fd, view[: min(11, len(view))])

        with mock.patch.object(IO.os, "write", chunked):
            IO.publish_bytes_exclusive(self.dest, PAYLOAD)
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)
        self.assertEqual(list(self.data.glob(f"{OWNER.OWNER_FILENAME}.*.tmp")), [])

    def test_a_partial_write_failure_leaves_the_destination_absent(self) -> None:
        real_write = os.write
        writes = {"count": 0}

        def short_then_fail(fd: int, data: bytes | memoryview) -> int:
            writes["count"] += 1
            view = memoryview(data)
            if writes["count"] == 1:
                return real_write(fd, view[: min(11, len(view))])
            raise OSError("deterministic publication failure")

        with mock.patch.object(IO.os, "write", short_then_fail):
            with self.assertRaises(IO.ReceiptPublicationUnavailable):
                IO.publish_bytes_exclusive(self.dest, PAYLOAD)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.data.glob(f"{OWNER.OWNER_FILENAME}.*.tmp")), [])

    def test_an_existing_receipt_is_never_replaced(self) -> None:
        original = b"already-published\n"
        self.dest.write_bytes(original)
        with self.assertRaises(IO.ReceiptAlreadyExists):
            IO.publish_bytes_exclusive(self.dest, PAYLOAD)
        self.assertEqual(self.dest.read_bytes(), original)
        self.assertEqual(list(self.data.glob(f"{OWNER.OWNER_FILENAME}.*.tmp")), [])

    def test_missing_link_primitive_refuses_without_writing_the_destination(self) -> None:
        with mock.patch.object(IO.os, "link", new=None):
            with self.assertRaises(IO.ReceiptPublicationUnavailable) as caught:
                IO.publish_bytes_exclusive(self.dest, PAYLOAD)
        self.assertIn("without replacing or partially writing", str(caught.exception))
        self.assertFalse(self.dest.exists())

    def test_cross_device_link_refuses_without_rename_fallback(self) -> None:
        def refuse_link(_source: str, _destination: str) -> None:
            raise OSError(errno.EXDEV, "cross-device")

        with mock.patch.object(IO.os, "link", refuse_link):
            with self.assertRaises(IO.ReceiptPublicationUnavailable):
                IO.publish_bytes_exclusive(self.dest, PAYLOAD)
        self.assertFalse(self.dest.exists())

    def test_exclusive_write_refuses_an_existing_owner_receipt(self) -> None:
        receipt = _receipt(self.data)
        encoded = OWNER.encode_owner_receipt(receipt)
        self.dest.write_bytes(encoded)
        with self.assertRaisesRegex(OWNER.EntryError, "REMOTE_LOCK_OWNED"):
            OWNER.write_owner_receipt_exclusive(self.data, receipt)
        self.assertEqual(self.dest.read_bytes(), encoded)

    def test_a_failed_close_is_not_repeated_and_still_removes_the_temp(self) -> None:
        """The close really closed the descriptor, then reported a writeback error.

        The old shape closed again from ``finally``; that second close raised
        ``EBADF``, escaped in place of the stable publication error, and left
        the temp file behind because the unlink was never reached.
        """

        real_close = os.close
        closes = {"count": 0}

        def close_then_report_writeback(fd: int) -> None:
            closes["count"] += 1
            real_close(fd)
            raise OSError(errno.EIO, "deterministic writeback failure on close")

        with mock.patch.object(IO.os, "close", close_then_report_writeback):
            with self.assertRaises(IO.ReceiptPublicationUnavailable) as caught:
                IO.publish_bytes_exclusive(self.dest, PAYLOAD)
        self.assertNotIsInstance(caught.exception, IO.ReceiptAlreadyExists)
        self.assertEqual(closes["count"], 1)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.data.glob(f"{OWNER.OWNER_FILENAME}.*.tmp")), [])

    def test_a_cleanup_close_failure_keeps_the_publication_error(self) -> None:
        """A write failure stays the reported failure even when cleanup cannot close."""

        real_close = os.close
        closes = {"count": 0}

        def refuse_write(_fd: int, _data: bytes | memoryview) -> int:
            raise OSError(errno.ENOSPC, "deterministic publication failure")

        def close_then_fail(fd: int) -> None:
            closes["count"] += 1
            real_close(fd)
            raise OSError(errno.EIO, "deterministic cleanup close failure")

        with mock.patch.object(IO.os, "write", refuse_write):
            with mock.patch.object(IO.os, "close", close_then_fail):
                with self.assertRaises(IO.ReceiptPublicationUnavailable) as caught:
                    IO.publish_bytes_exclusive(self.dest, PAYLOAD)
        self.assertIn("owner receipt write did not complete", str(caught.exception))
        self.assertEqual(closes["count"], 1)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.data.glob(f"{OWNER.OWNER_FILENAME}.*.tmp")), [])

    def test_publication_never_renames_or_replaces(self) -> None:
        tree = ast.parse((SHELL / "remote_receipt_io.py").read_text(encoding="utf-8"))
        names = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in {"rename", "replace"}
        ]
        self.assertEqual(names, [])


class PublishedReceiptCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.lease = self.data / ".workstack.lock"
        self.lease.write_bytes(b"real writer lease, owned by the store\n")

    def test_guard_contention_leaves_the_exact_receipt_and_is_uncertain(self) -> None:
        receipt = _receipt(self.data)
        OWNER.write_owner_receipt_exclusive(self.data, receipt)
        with mock.patch.object(GUARD, "_try_lock", return_value=False):
            detail = OWNER.remove_published_owner_receipt_if_still_ours(
                self.data,
                receipt,
                guard=OWNER.GuardWait(timeout_seconds=0.0, poll_seconds=0.01),
            )
        self.assertEqual(detail, "published owner receipt cleanup is uncertain")
        self.assertEqual(
            (self.data / OWNER.OWNER_FILENAME).read_bytes(),
            OWNER.encode_owner_receipt(receipt),
        )
        self.assertEqual(self.lease.read_bytes(), b"real writer lease, owned by the store\n")

    def test_a_replacement_is_left_untouched(self) -> None:
        ours = _receipt(self.data, pid=4242)
        other = _receipt(self.data, pid=5151, start="start-9")
        OWNER.write_owner_receipt_exclusive(self.data, ours)
        (self.data / OWNER.OWNER_FILENAME).write_bytes(OWNER.encode_owner_receipt(other))
        detail = OWNER.remove_published_owner_receipt_if_still_ours(self.data, ours)
        self.assertEqual(detail, "published owner receipt was replaced before cleanup")
        self.assertEqual(
            (self.data / OWNER.OWNER_FILENAME).read_bytes(),
            OWNER.encode_owner_receipt(other),
        )
        self.assertEqual(self.lease.read_bytes(), b"real writer lease, owned by the store\n")


if __name__ == "__main__":
    unittest.main()
