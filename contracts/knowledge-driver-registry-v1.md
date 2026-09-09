# Knowledge driver registry v1 contract

Status: `workstack/knowledge_driver_registry.py` implements this document exactly, and
`work-stack graph serve --knowledge-drivers-config <absolute path>` is its only caller.
This is an **operator configuration file**, not a wire contract and not a request surface.
It carries no OpenDocuments knowledge: the adapter it pins is an executable the operator
installed, and every adapter-specific cross-check — which upstream that executable really
talks to, whether its key file exists, what its own configuration says — stays the
adapter's responsibility. Nothing here is a sandbox.

Omitting the flag is the unchanged default: no driver is pinned, and every execution
reports the explicit `knowledge_driver_unavailable` state.

## The document

```json
{
  "schema": "workstack.knowledge-drivers.v1",
  "drivers": [
    {
      "alias": "od-primary",
      "upstream_workspace_uid": "8f14e45f-ce8a-4b0e-9c2a-1d1f0a3b5c77",
      "command": ["C:\\adapters\\od-adapter.exe", "--serve"],
      "environment": {"OD_CONFIG_FILE": "C:\\adapters\\od.json"}
    }
  ]
}
```

The top level is an object with **exactly** `schema` and `drivers`; `drivers` is an array of
**1–8** entries. Each entry requires `alias`, `upstream_workspace_uid`, `command`
and `environment`, and may also contain `verification`. Other fields, missing required
fields or a second `schema` key are refusals, not warnings.

The optional `verification` object has exactly `command` and `environment`, with
the same pinned argv and environment rules. Omission disables source checks;
explicit null, partial objects and extra fields are refused. Verification uses
the entry's existing alias and upstream identity. It never falls back to the
search command. See [Explicit source verification](knowledge-verification-v1.md).

`environment` is the **whole** environment the child will get: nothing is inherited from the
server process, no `os.environ` value is merged in and no name is filled in from a default.
Put a *reference* to a credential here — a path the adapter reads — rather than the secret
itself; this file is ordinary operator-owned material on one OS account, and this contract
does not claim that an arbitrary string can be recognised as a secret.

## When it is read

`_dispatch_process_owner` loads the registry **before** it constructs a `Store`. The store,
its exclusive lease, the listening socket and `--seed-demo` all happen after admission, so a
malformed registry refuses the start of a server that never touched the data directory. The
file is read **once**, at startup: there is no watch, no reload, no re-read per request, and
a running server's registry is fixed for the life of that process.

The path must be **absolute** in native form, so the operator's pin is not resolved against a
working directory. The pinned executable is *not* resolved, stated or launched during
admission: no child process runs while a registry is being admitted.

## Admission

| Rule | Refusal code |
| --- | --- |
| path absolute, non-empty | `driver_config_path_required`, `driver_config_path_not_absolute` |
| file opens and reads | `driver_config_unreadable` (an `OSError`) |
| at most 64 KiB | `driver_config_too_large` |
| strict UTF-8 JSON, no duplicate key, no non-finite number, bounded depth | `invalid_encoding`, `invalid_json`, `duplicate_json_key`, `non_finite_number`, `request_too_deep` |
| top level an object with exactly `schema` and `drivers` | `invalid_driver_registry`, `unknown_field`, `missing_field` |
| `schema` equal to `workstack.knowledge-drivers.v1` | `unknown_driver_registry_schema` |
| `drivers` an array of 1–8 objects with four required fields and optional `verification` only | `invalid_driver_registry`, `driver_registry_empty`, `driver_registry_full`, `invalid_driver_binding`, `unknown_field`, `missing_field` |
| each `alias` a string, and no alias repeated | `invalid_driver_alias`, `duplicate_driver_alias` |
| `environment` an object, no two names differing only in case | `invalid_driver_environment`, `duplicate_driver_environment_name` |
| alias grammar, canonical non-nil UUID, argv shape, environment types | `invalid_driver_alias`, `invalid_driver_upstream_workspace_uid`, `invalid_driver_command`, `invalid_driver_environment` |

The JSON grammar is `knowledge_request.decode_strict_json` and the last row is
`knowledge_execution_runtime.admit_drivers`, both reused rather than restated: a registry this
loader accepts cannot be refused later for its shape, and the admitted mapping is the same
immutable copy the server has always taken.

Two names that differ only in case — `PATH` and `Path` — are refused because Windows resolves
an environment name case-insensitively: they are one name to the child and an ambiguity here.

**Every refusal is a code and nothing else.** The configured path, the document, an argv part,
an environment name or value and the caught OS message never appear in the message, the
process exit line or a log, so a file naming a credential path is not echoed by refusing it.
Exit status is the CLI's ordinary `2`. A refused file is never rewritten, repaired or removed.

## Hosting and acceptance limits

The desktop profile and remote launch composition now carry an explicitly
configured registry path through to the owner CLI. An absent registry remains
the legacy startup path. A remote registry path names a file on the remote
host; this is not a transfer of local secrets or configuration files.

Source-level propagation and a private staged CLI have separate validation
receipts. They do not establish company SSH, installed GUI, customer corpus,
or automatic-update acceptance. Those environment checks remain separate.
