"""Literal-only frozen fixture tables for the P0 agent CLI contract.

This module imports nothing and performs no calls or I/O. It holds the four
literal assignment tables consumed by workstack.agent_cli_contract.
"""

_ADMISSION = {
    "approved_store_free_seam": "B1 uses bounded direct document reads of authority documents (workspace.json identity and format marker documents); it imports neither Store nor any workstack.storage module and creates no files or directories",
    "order": [
        "require explicit --data-dir and --workspace-uid",
        "resolve the data path without creating it",
        "require an existing directory and recognizable Work Stack authority",
        "inspect format and workspace identity without importing or constructing Store",
        "refuse v4 with capability_not_enabled; refuse missing, unknown and unrecognizable authorities with invalid_authority",
        "require actual workspace UID equal to expected workspace UID before any Task content is returned or mutation sent",
        "only then construct the v3 Store or contact its declared loopback server owner",
    ],
    "uid_rule": "the workspace UID is the canonical non-nil lowercase RFC 4122 UUID read from workspace.json id; expected and actual values are compared as canonical strings",
}

_BACKEND_RESULTS = {
    "checkpoint": {
        "commit_state": "committed|unknown",
        "entry": "the committed or replayed review-entry data mapping, null when commit_state is unknown",
        "replayed": "bool",
    },
    "context": {
        "entries": "raw worklog entries for the bounded 31-day window",
        "entry_keys": ["blockers", "date", "done", "next", "task_id"],
        "task": "the raw Task detail mapping",
        "workspace_uid": "str",
    },
    "status": {
        "keys": [
            "actual_workspace_uid",
            "capability_reason",
            "capability_supported",
            "contract",
            "data_dir_available",
            "exclusive_local_available",
            "expected_workspace_uid",
            "ready",
            "running_server_available",
            "storage_format",
        ]
    },
}

_ENVELOPE = {
    "data_shapes": {
        "agent.checkpoint": {
            "keys": ["blockers", "date", "done", "next", "task", "task_id"],
            "source": "the existing review-entry response data returned by WorkStack.add_worklog_v1 and POST /api/v1/review/entries",
        },
        "agent.context": {
            "keys": ["omitted", "recent_worklog", "task", "workspace_uid"],
            "omitted_categories": [
                "attachments",
                "captures",
                "objectives",
                "relationships",
                "work_sessions",
            ],
            "overflow_marker": "recent_worklog_overflow",
            "overflow_marker_channel": "data.omitted",
            "recent_worklog_entry_keys": ["blockers", "date", "done", "next"],
            "source": "the CLI-v1 context data shape with golden tests",
            "task_allowlist": [
                "detail",
                "due",
                "id",
                "priority",
                "revision",
                "status",
                "title",
                "uid",
            ],
        },
        "agent.status": {
            "keys": [
                "actual_workspace_uid",
                "capability_reason",
                "capability_supported",
                "contract",
                "data_dir_available",
                "exclusive_local_available",
                "expected_workspace_uid",
                "ready",
                "running_server_available",
                "storage_format",
            ],
            "source": "the AgentBackend.status result mapping",
        },
    },
    "failure": {
        "error_optional": {"retryable": "bool"},
        "error_required": {"code": "str", "details": "object", "message": "str"},
        "forbidden": ["data"],
        "required": {"contract": "str", "error": "object", "meta": "object"},
        "variants": {
            "commit_unknown": {
                "error_code": "commit_unknown",
                "meta_required": {
                    "command": "agent.checkpoint",
                    "commit_state": "unknown",
                    "intent_id": "str",
                    "task_id": "str",
                    "transport": "running-server",
                    "workspace_uid": "str",
                },
            },
            "ordinary_command_failure": {
                "meta_forbidden": ["commit_state"],
                "meta_required": {"command": "agent.<command>"},
            },
        },
    },
    "renderer": "compact sorted-key UTF-8 JSON, exactly one object, one trailing LF",
    "rules": [
        "success and failure are mutually exclusive: success has data and no error; failure has error and no data",
        "a field that does not apply to the executed command is omitted, never filled with a placeholder",
        "commit_state=committed appears only on successful agent.checkpoint",
        "commit_state=unknown appears only on commit_unknown after a POST mutation attempt and failed identical replay",
        "final serialized envelope, not merely data, is bounded to 32 KiB",
        "paths, CSRF values, tokens and raw server bodies never enter error details",
        "successful status emits data_dir_available as a boolean and never emits the resolved absolute path",
        "retryable appears only when the CLI can give a sound retry recommendation",
    ],
    "success": {
        "forbidden": ["error"],
        "required": {"contract": "str", "data": "object", "meta": "object"},
        "variants": {
            "agent.checkpoint": {
                "meta_optional": {},
                "meta_required": {
                    "command": "agent.checkpoint",
                    "commit_state": "committed",
                    "intent_id": "str",
                    "replayed": "bool",
                    "task_id": "str",
                    "transport": "running-server|exclusive-local",
                    "workspace_uid": "str",
                },
            },
            "agent.context": {
                "meta_optional": {},
                "meta_required": {
                    "command": "agent.context",
                    "task_id": "str",
                    "transport": "running-server|exclusive-local",
                    "workspace_uid": "str",
                },
            },
            "agent.status": {
                "meta_optional": {},
                "meta_required": {
                    "command": "agent.status",
                    "transport": "running-server|exclusive-local",
                    "workspace_uid": "str",
                },
            },
        },
    },
}

_TRANSPORT_RULES = {
    "automatic_retry_policy": "only one identical replay is automatic after possible POST response loss; session, storage, GET and pre-POST failures are never retried",
    "commit_unknown_precondition": "valid only after a POST may have reached the server and the identical bounded replay also cannot establish the result",
    "context_daily_review_gets": {
        "count": 31,
        "date_order": "today through today minus 30 days, newest first",
        "weekly_projection_forbidden": True,
    },
    "identical_replay": "both POST attempts reuse pre-serialized bytes and the same Idempotency-Key",
    "no_fresh_key_on_response_loss": True,
    "post_attempt_maximum": 2,
    "session_failure_omits_commit_state": True,
}
