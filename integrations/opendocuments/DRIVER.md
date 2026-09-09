# OpenDocuments knowledge driver (pilot)

This file documents `driver_config.py`, `driver_main.py`, the source-checkout
entry `driver_entry.py`, and their launcher, `scripts/knowledge_driver_launcher.py`.
`CLIENT.md` documents the chat transport, `MAPPING.md` the retrieval mapper,
`MANUAL-QUERY.md` the reviewed query bridge. This lane adds the **process**
those three were missing, and no new retrieval, mapping or import rule.

It is a **source-checkout pilot**, not an installed or remote integration.
`driver_entry.py` is trusted checkout bootstrap: `[absolute_python,
absolute_driver_entry.py]`. A future standalone packaged adapter is a different
artifact and is not this file.

## Shape

```
operator shell
  └─ scripts/knowledge_driver_launcher.py --drivers-config <abs> --data-dir <abs>
       ├─ reads drivers.json                        (nonsecret, outside the store)
       ├─ reads each driver's od-driver.json        (nonsecret, outside the store)
       └─ create_server(..., knowledge_drivers=…)   on 127.0.0.1 only
             └─ POST /api/v1/knowledge/requests/execute
                   └─ child: python <abs>/integrations/opendocuments/driver_entry.py
                        ├─ puts checkout root first on sys.path (from __file__)
                        ├─ reads od-driver.json     (WORKSTACK_OD_DRIVER_CONFIG)
                        ├─ reads the key file       (api_key_file) — only here
                        └─ run_reviewed_query → one POST /api/v1/chat
```

## The two configuration files

Both live **outside** the Work Stack data directory, in a directory the
operator chose (for example `%LOCALAPPDATA%\WorkStack\config\`). Neither is
read from the store, and neither has a default location: every path on the
command line and inside these documents is absolute.

### `workstack.opendocuments-driver.v1` — one driver's own document

```jsonc
{
  "schema": "workstack.opendocuments-driver.v1",
  "connection_alias": "team-nas",            // the ledger's alias for this driver
  "upstream_workspace_uid": "<canonical uuid>", // Work Stack's identity
  "od_workspace_id": "ws_opendocuments_1",   // OpenDocuments' identity — a different string
  "origin": "https://opendocuments.example", // https, or http on 127.0.0.1 / ::1
  "profile": "fast",                         // the corpus-only profile, exactly
  "timeout_seconds": 20.0,                   // 0.05 .. 60, inside the owner's 75 s budget
  "corpus_grants": ["nas-team-share"],       // 1..8 unique corpus aliases
  "source_catalog": {                        // 1..64 entries, operator-curated
    "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee1": {
      "document_ref": "od-page-7f3ba1d34f50c884600112ab",
      "source_type": "notion.page",          // or "nas.file"
      "display_title": "Release quality gate"
    }
  },
  "api_key_file": "C:\\...\\od-key.txt"      // absolute; read only by the child
}
```

Exactly these ten keys. **There is no `api_key` field**, and a document that
carries one is refused rather than ignored. The file is bounded at 64 KiB and
decoded strictly: duplicate keys, non-finite numbers and excessive nesting are
refusals, not surprises.

Two rules that are easy to get wrong:

- **Catalog keys must be canonical lowercase UUIDs.** The mapper looks a
  `documentId` up exactly (`retrieval_mapper_fields.classify_source`) while it
  compares chunk ownership casefolded. An uppercase key would admit here and
  then silently drop every hit as out-of-catalog. The loader refuses it instead.
- **`display_title` is a label, not a path.** The released title gate refuses
  location-shaped titles (`capture_retrieval._safe_title`), so a value copied
  from a NAS path will be refused at compose time. Curate titles.

The catalog is **hand-written by the operator**. Nothing builds one, and a
search result never extends one. There is no supported export from
OpenDocuments for this lane (`CLIENT.md` forbids document/admin calls).

### `workstack.knowledge-drivers.v1` — the launcher's registry

```jsonc
{"schema": "workstack.knowledge-drivers.v1", "drivers": [{
  "alias": "team-nas",
  "upstream_workspace_uid": "<canonical uuid>",
  "command": ["C:\\Python\\python.exe", "C:\\...\\integrations\\opendocuments\\driver_entry.py"],
  "environment": {
    "WORKSTACK_OD_DRIVER_CONFIG": "C:\\...\\od-driver.json",
    "SystemRoot": "C:\\Windows",
    "PATH": "C:\\Windows\\System32"
  }
}]}
```

1 to 8 entries, unique aliases, `command[0]` absolute. The `environment` is the
**whole** environment the child will see: nothing is inherited from the
operator's shell, so on Windows `SystemRoot`/`WINDIR` and `PATH` must be stated
or socket and TLS initialisation fails. `driver_entry.py` does **not** need
`PYTHONPATH`: it puts this checkout first on `sys.path` from its own file
path, so a foreign cwd cannot supply `workstack` or `integrations`. `PYTHONPATH`
remains an optional closed name if an operator still wants it. `-m driver_main`
is not the supported source-checkout invocation. `WORKSTACK_OD_DRIVER_CONFIG`
is required, and the launcher reads the document it names to check that its
`connection_alias` and `upstream_workspace_uid` are the ones the registry claims
— before any store lease is taken.

**The variable names are a closed set** (pilot launcher policy, not a change to
the generic `KnowledgeDriverBinding`, which still accepts whatever mapping its
embedding process states):

| required | optional |
| --- | --- |
| `WORKSTACK_OD_DRIVER_CONFIG` | `PYTHONPATH`, `SystemRoot`, `WINDIR`, `PATH`, `TEMP`, `TMP`, `LANG`, `LC_ALL` |

Any other name is refused, and so are two names that differ only in case, since
that is how some operating systems resolve them. The optional names are matched
case-insensitively, so Windows' own `SystemRoot` is written as Windows writes
it. **`WORKSTACK_OD_DRIVER_CONFIG` is matched exactly**: the child reads that
one variable with a single case-sensitive lookup, so a registry spelling it
`workstack_od_driver_config` is refused
(`unknown_driver_environment_name`) rather than quietly rewritten — on a
case-sensitive system the rewrite would admit here and then leave the child with
no configuration to find. The values are operator-pinned.
There is no `os.environ` merge anywhere, and no key variable: a credential has
no name to travel in.

## Credentials, stated precisely

The key lives in its own file, named by `api_key_file`, and
`driver_config.load_driver_key` is called **only by the child**, and only after
the request, the configuration and the policy match have all been admitted.

- The precise claim: **the launcher and the owner process never read the key.**
  It is not in the registry, not in the child's environment, not on an argv, not
  in a log line and not in any refusal.
- What is *not* claimed: that the key exists "only in the child". The bytes are
  in a file, and any process running as the same OS user can read that file.
  This is **same-user operator trust made explicit, not a sandbox.**
- The operator owns the file's permissions (on Windows, restrict it to your own
  account; on POSIX, `chmod 600`). This lane adds no OS security dependency and
  reads no real credential store.
- **What the key file may contain:** 1 to 256 printable ASCII octets
  (`0x21`–`0x7E`, so no space and no control byte), optionally followed by
  exactly one terminal newline — `LF`, or the `CRLF` a Windows editor writes.
  A maximal 256-octet key therefore lives in a file of up to 258 octets. A
  second newline, a lone `CR`, a longer key, a byte outside that range, any
  non-ASCII byte and an empty file are all refused, and the refusal names a
  stage (`driver_key_refused`) and never a byte.
- Rotation, per-user secret storage (DPAPI / Keychain / keyring) and an
  installed configuration UI remain **unsolved** and out of scope here.
- That the key is scoped to the intended OpenDocuments workspace is an operator
  configuration fact. A chat response cannot prove it (`CLIENT.md`).

## The child's contract

Input is one `workstack.knowledge-execute.v1` envelope on stdin, at most 16 KiB:
exactly `{schema, request, connection}`, with `connection` exactly
`{alias, upstream_workspace_uid, policy_revision}`. The `request` is the issued
KnowledgeRequest v1 projection the owner already admitted against the live
ledger, policy, Task and window; the child re-checks its **structure and
bounds** only. It never rebuilds a `RequestAuthority` from the request's own
fields — the parent remains the ledger and Task authority, and it re-admits the
request after the child answers.

Refusals before any socket is opened:

- `connection.alias` ≠ `connection_alias`, or `connection.upstream_workspace_uid`
  ≠ `upstream_workspace_uid`;
- `set(request.corpus_refs)` ≠ `set(corpus_grants)` — **set equality, not
  containment.** The frozen upstream chat route has no collection field, so a
  narrower request is a filter this driver cannot honour; it refuses rather than
  answering a small question with a whole-workspace search;
- an expired window, re-read against this process' own clock. There is no
  renew, extend or reissue anywhere in this lane.

**No answer and no actions are generated.** The composer copies the upstream
`answer`, chunk `content`, confidence `reason` and `sourcePath` nowhere: an item
carries the catalog-derived evidence list, a title that is either that evidence's
own title or the constant `Search evidence`, and **empty** `action_items` and
`tags`. Linking evidence for a person to review is the whole claim; no summary is
synthesised and no action is extracted.

`item_id` is `uuid5(OD_DRIVER_ITEM_NAMESPACE, request_id)`, so the same request
always proposes the same item and the upstream `queryId` never enters the
identity. Exactly one `POST /api/v1/chat`, and no retry on any branch —
including `outcome_unknown`.

## Exit semantics

| Outcome | stdout | exit |
| --- | --- | --- |
| composed envelope | one canonical `workstack.knowledge-import.v1` document ≤ 64 KiB | `0` |
| anything else | **empty** | nonzero |

A refusal writes at most one closed code from `driver_main.DIAGNOSTIC_CODES`
(`driver_input_refused`, `driver_config_refused`, `driver_policy_refused`,
`driver_key_refused`, `driver_query_refused`, `driver_output_refused`) to
stderr — never a message, a traceback, a response body, a path, a key or the
query. The child takes no options; an argv is refused.

The owner's transport collapses every nonzero exit into its own
`driver_outcome_unknown`, so a deterministic pre-network refusal reaches the
operator as an unknown outcome. That is conservative and accepted: the
diagnosis path is `--check-config`, not an in-band error envelope. This lane
adds no second protocol on stdout.

## Running the pilot

```
python scripts/knowledge_driver_launcher.py --drivers-config <abs> --check-config
python scripts/knowledge_driver_launcher.py --drivers-config <abs> --data-dir <abs> [--port 0]
```

- `--check-config` admits the registry, every driver document, every catalog and
  every origin, then exits. It **opens no key file, spawns no child, binds no
  socket and takes no store lease.** The real key is verified only by a real
  child run.
- `--data-dir` is required for a run, absolute, and must be **empty or absent**.
  The pilot creates its own store; it will not open, migrate or adopt a
  directory that already holds work.
- The host is fixed at `127.0.0.1`. `--port` defaults to `0` (the OS picks).
  The only thing printed on a successful start is the loopback URL. Stop it with
  Ctrl-C.
- The registry path and the pinned executable are trusted operator input, typed
  by the person running the command. Nothing is ever run through a shell.

## Not covered, and not to be marked done

- Production credential storage, rotation and an installed configuration UI.
- A supported source for `source_catalog`; today it is hand-curated.
- Installed or remote packaging: `driver_entry.py` is source-checkout
  bootstrap, not a console script or frozen executable. A packaged adapter is a
  separate lane and a different command.
- Per-collection scoping, if upstream ever gains it.
- Exactly-once, crash resume, or automatic retry of an unknown outcome.
- Any claim that this is an OS sandbox, or that a customer's NAS/Notion
  permissions have been verified.
