"""`graph serve --knowledge-drivers-config` at the command-line boundary.

The Store, the WorkStack and the server are the same monkeypatch seams the CLI
characterization suite uses, so no store is created, no lease is taken and no
socket is opened here. A refusal case makes all three raise on contact: the
registry must be admitted *before* any of them is reached, and a test that
reaches one fails.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workstack import cli  # noqa: E402
from workstack.knowledge_driver_registry import REGISTRY_SCHEMA  # noqa: E402

UPSTREAM = "8f14e45f-ce8a-4b0e-9c2a-1d1f0a3b5c77"
SECRET = "SUPER-SECRET-TOKEN-VALUE"
EXECUTABLE = os.path.join(os.path.abspath(os.sep), "opt", "adapters", "od-adapter")

VALID = json.dumps({
    "schema": REGISTRY_SCHEMA,
    "drivers": [{
        "alias": "od-primary",
        "upstream_workspace_uid": UPSTREAM,
        "command": [EXECUTABLE, "--serve"],
        "environment": {"OD_KEY_FILE": SECRET},
    }],
})


class _CliDriverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.data_dir = str(self.directory / "data")

    def _config(self, text: str, name: str = "drivers.json") -> str:
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _serve(self, *flags: str) -> tuple[int, Mock, Mock]:
        """Run one `graph serve` with the store, stack and server stood in for."""

        stack = Mock()
        with (
            patch.object(cli, "Store", return_value=Mock()),
            patch.object(cli, "WorkStack", return_value=stack),
            patch.object(cli, "serve") as serve,
        ):
            result = cli.main(["--data-dir", self.data_dir, "graph", "serve", *flags])
        return result, serve, stack

    def _refused(self, *flags: str) -> str:
        """Run one refused `graph serve`; the seams fail the test on contact."""

        stderr = io.StringIO()
        unreachable = AssertionError("reached after a refused driver registry")
        with (
            patch.object(cli, "Store", side_effect=unreachable) as store,
            patch.object(cli, "WorkStack", side_effect=unreachable) as work_stack,
            patch.object(cli, "serve", side_effect=unreachable) as serve,
            patch.object(cli.sys, "stderr", stderr),
        ):
            result = cli.main(["--data-dir", self.data_dir, "graph", "serve", *flags])
        self.assertEqual(2, result)
        store.assert_not_called()
        work_stack.assert_not_called()
        serve.assert_not_called()
        return stderr.getvalue()


class AdmittedDriverServeTests(_CliDriverTestCase):
    def test_the_admitted_binding_reaches_the_server_call(self) -> None:
        result, serve, stack = self._serve(
            "--knowledge-drivers-config", self._config(VALID)
        )

        self.assertEqual(0, result)
        serve.assert_called_once()
        self.assertEqual((stack, "127.0.0.1", 8765), serve.call_args.args)
        self.assertEqual(
            {"public_port", "knowledge_drivers"}, set(serve.call_args.kwargs)
        )
        self.assertIsNone(serve.call_args.kwargs["public_port"])
        drivers = serve.call_args.kwargs["knowledge_drivers"]
        self.assertEqual(["od-primary"], list(drivers))
        self.assertEqual((EXECUTABLE, "--serve"), drivers["od-primary"].command)
        self.assertEqual(
            {"OD_KEY_FILE": SECRET}, dict(drivers["od-primary"].environment)
        )
        self.assertEqual(UPSTREAM, drivers["od-primary"].upstream_workspace_uid)

    def test_no_flag_makes_exactly_the_call_it_made_before(self) -> None:
        result, serve, stack = self._serve()

        self.assertEqual(0, result)
        serve.assert_called_once_with(stack, "127.0.0.1", 8765)

    def test_the_public_port_call_is_unchanged_without_a_registry(self) -> None:
        result, serve, stack = self._serve("--public-port", "9000")

        self.assertEqual(0, result)
        serve.assert_called_once_with(stack, "127.0.0.1", 8765, public_port=9000)

    def test_a_registry_and_a_public_port_travel_together(self) -> None:
        result, serve, _ = self._serve(
            "--public-port", "9000", "--knowledge-drivers-config", self._config(VALID)
        )

        self.assertEqual(0, result)
        self.assertEqual(9000, serve.call_args.kwargs["public_port"])
        self.assertEqual(
            ["od-primary"], list(serve.call_args.kwargs["knowledge_drivers"])
        )

    def test_the_parent_lifetime_binding_still_runs_before_the_server(self) -> None:
        calls: list[str] = []
        stack = Mock()
        with (
            patch.object(cli, "Store", return_value=Mock()),
            patch.object(cli, "WorkStack", return_value=stack),
            patch.object(
                cli,
                "_bind_linux_process_lifetime_to_parent",
                side_effect=lambda: calls.append("bind"),
            ),
            patch.object(
                cli, "serve", side_effect=lambda *a, **k: calls.append("serve")
            ),
        ):
            result = cli.main([
                "--data-dir", self.data_dir, "graph", "serve",
                "--exit-with-parent",
                "--knowledge-drivers-config", self._config(VALID),
            ])

        self.assertEqual(0, result)
        self.assertEqual(["bind", "serve"], calls)


class RefusedDriverServeTests(_CliDriverTestCase):
    def test_every_malformed_registry_refuses_before_the_store_exists(self) -> None:
        cases = {
            "driver_config_unreadable": str(self.directory / "absent.json"),
            "driver_config_path_not_absolute": os.path.join("rel", "drivers.json"),
            "unknown_driver_registry_schema": self._config(
                VALID.replace(REGISTRY_SCHEMA, "workstack.other.v1"), "schema.json"
            ),
            "duplicate_json_key": self._config(
                '{"schema": "a", "schema": "b", "drivers": []}', "duplicate.json"
            ),
            "driver_config_too_large": self._config(
                json.dumps({
                    "schema": REGISTRY_SCHEMA,
                    "drivers": [],
                    "padding": "x" * (64 * 1024),
                }),
                "large.json",
            ),
        }
        for code, path in cases.items():
            with self.subTest(code=code):
                self.assertEqual(
                    "error: {}\n".format(code),
                    self._refused("--knowledge-drivers-config", path),
                )

    def test_a_duplicate_alias_refuses_even_with_seed_demo_requested(self) -> None:
        document = json.loads(VALID)
        document["drivers"].append(dict(document["drivers"][0]))
        path = self._config(json.dumps(document), "duplicate-alias.json")

        stderr = self._refused("--seed-demo", "--knowledge-drivers-config", path)

        self.assertEqual("error: duplicate_driver_alias\n", stderr)

    def test_a_refusal_repeats_nothing_the_operator_wrote(self) -> None:
        document = json.loads(VALID)
        document["drivers"][0]["command"] = ["od-adapter", "--serve"]
        path = self._config(json.dumps(document), "relative-argv.json")
        before = Path(path).read_bytes()

        stderr = self._refused("--knowledge-drivers-config", path)

        self.assertEqual("error: invalid_driver_command\n", stderr)
        for leaked in (SECRET, "od-primary", "OD_KEY_FILE", "od-adapter", path):
            self.assertNotIn(leaked, stderr)
        self.assertEqual(before, Path(path).read_bytes())


class GraphDispatchSeamTests(_CliDriverTestCase):
    """The `STACK_COMMANDS["graph"]` entry is still the flagless dispatch seam."""

    def _replaced_graph(self, *flags: str) -> tuple[int, list[tuple], str]:
        """Run one `graph serve` with the mapping entry replaced by a refusal."""

        calls: list[tuple] = []
        stderr = io.StringIO()

        def refusing_graph(*args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))
            raise OSError("graph handler replaced")

        with (
            patch.object(cli, "Store", return_value=Mock()),
            patch.object(cli, "WorkStack", return_value=self.stack),
            patch.dict(cli.STACK_COMMANDS, {"graph": refusing_graph}),
            patch.object(
                cli, "serve", side_effect=AssertionError("the real serve was reached")
            ) as serve,
            patch.object(cli.sys, "stderr", stderr),
        ):
            result = cli.main(["--data-dir", self.data_dir, "graph", "serve", *flags])
        self.serve = serve
        return result, calls, stderr.getvalue()

    def setUp(self) -> None:
        super().setUp()
        self.stack = Mock()

    def test_a_flagless_serve_is_stopped_by_the_replaced_mapping_entry(self) -> None:
        result, calls, stderr = self._replaced_graph()

        self.assertEqual(2, result)
        self.assertEqual("error: graph handler replaced\n", stderr)
        self.serve.assert_not_called()
        self.assertEqual(1, len(calls))
        arguments, kwargs = calls[0]
        # Exactly the two positional arguments the mapping was always called with.
        self.assertEqual({}, kwargs)
        self.assertEqual(2, len(arguments))
        self.assertEqual("serve", arguments[0].action)
        self.assertIs(self.stack, arguments[1])

    def test_a_configured_serve_takes_the_registry_aware_call(self) -> None:
        with patch.object(cli, "serve") as serve:
            with (
                patch.object(cli, "Store", return_value=Mock()),
                patch.object(cli, "WorkStack", return_value=self.stack),
                patch.dict(
                    cli.STACK_COMMANDS,
                    {"graph": Mock(side_effect=AssertionError("flagless dispatch"))},
                ),
            ):
                result = cli.main([
                    "--data-dir", self.data_dir, "graph", "serve",
                    "--knowledge-drivers-config", self._config(VALID),
                ])

        self.assertEqual(0, result)
        self.assertEqual(
            ["od-primary"], list(serve.call_args.kwargs["knowledge_drivers"])
        )


if __name__ == "__main__":
    unittest.main()
