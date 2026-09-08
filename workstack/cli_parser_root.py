"""The `work-stack` root parser and the domain order it publishes.

`parser()` is the whole argparse surface: the same prog name, the same
`--data-dir` option, the same required `domain` subcommand, and the same
eleven domains in the same order, so `--help`, the exit-2 refusals and every
parsed Namespace field are byte-for-byte what the single-function builder
produced. Adding a domain means adding one call here and one builder beside it.
"""

from __future__ import annotations

import argparse

from .cli_parser_operations import (
    add_agent_parser,
    add_graph_parser,
    add_maintenance_parser,
    add_snapshot_parser,
    add_storage_parser,
)
from .cli_parser_planning import (
    add_backlog_parser,
    add_capture_parser,
    add_note_parser,
    add_okr_parser,
    add_weekly_parser,
    add_worklog_parser,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="work-stack")
    root.add_argument("--data-dir", help="override the local JSON data directory")
    sub = root.add_subparsers(dest="domain", required=True)
    add_backlog_parser(sub)
    add_okr_parser(sub)
    add_worklog_parser(sub)
    add_weekly_parser(sub)
    add_note_parser(sub)
    add_capture_parser(sub)
    add_agent_parser(sub)
    add_snapshot_parser(sub)
    add_storage_parser(sub)
    add_maintenance_parser(sub)
    add_graph_parser(sub)
    return root


__all__ = ("parser",)
