"""Read one chosen Markdown reference into a Task-bound context envelope.

Run with python -m workstack.knowledge_context --help. No live Store is opened.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

from .knowledge_reference import KnowledgeReferenceError, MarkdownVault


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise KnowledgeReferenceError('invalid_task_identity')
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise KnowledgeReferenceError('invalid_task_identity') from error
    if str(parsed) != value or parsed.int == 0:
        raise KnowledgeReferenceError('invalid_task_identity')
    return value


def _load_task_export(path: Path) -> dict:
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise KnowledgeReferenceError('task_export_too_large')
    try:
        return json.loads(raw.decode('utf-8-sig'))
    except (RecursionError, ValueError) as error:
        # Content that is undecodable, malformed or nested past the decoder's
        # depth is an invalid export, not an unavailable Task or vault. The
        # depth failure is a RecursionError, which is not a ValueError.
        raise KnowledgeReferenceError('invalid_task_export') from error


def task_binding(task: dict, workspace_uid: str) -> dict:
    if not isinstance(task, dict):
        raise KnowledgeReferenceError('invalid_task_export')
    task = task.get('data', task)
    if isinstance(task, dict):
        task = task.get('task', task)
    if not isinstance(task, dict):
        raise KnowledgeReferenceError('invalid_task_export')
    task_id, revision = task.get('id'), task.get('revision')
    if not isinstance(task_id, str) or not task_id or len(task_id) > 128 or any(ord(c) < 32 for c in task_id):
        raise KnowledgeReferenceError('invalid_task_identity')
    if type(revision) is not int or revision < 0:
        raise KnowledgeReferenceError('invalid_task_revision')
    return {'workspace_uid': _uuid(workspace_uid), 'task_uid': _uuid(task.get('uid')),
            'task_id': task_id, 'task_revision': revision}


def build_context(task: dict, workspace_uid: str, vault: MarkdownVault, document: str,
                  *, start_line: int = 1, end_line: int = 40, expected_sha256: str | None = None) -> dict:
    binding = task_binding(task, workspace_uid)
    reference = vault.read(document, start_line=start_line, end_line=end_line, expected_sha256=expected_sha256)
    return {'schema': 'workstack.knowledge-context.v1', 'binding': binding,
            'references': [reference], 'generated': False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-export', type=Path, required=True)
    parser.add_argument('--workspace-uid', required=True)
    parser.add_argument('--vault-root', type=Path, required=True)
    parser.add_argument('--vault-id', required=True)
    parser.add_argument('--document', required=True, help='Vault-relative Markdown path, using /')
    parser.add_argument('--start-line', type=int, default=1)
    parser.add_argument('--end-line', type=int, default=40)
    parser.add_argument('--expected-sha256')
    args = parser.parse_args(argv)
    try:
        task = _load_task_export(args.task_export)
        result = build_context(task, args.workspace_uid, MarkdownVault(args.vault_root, args.vault_id), args.document,
                               start_line=args.start_line, end_line=args.end_line, expected_sha256=args.expected_sha256)
    except KnowledgeReferenceError as error:
        print(json.dumps({'error': {'code': error.code}}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({'error': {'code': 'task_or_vault_unavailable'}}))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
