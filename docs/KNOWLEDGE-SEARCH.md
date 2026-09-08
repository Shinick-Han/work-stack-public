# Related-document search for a Task

Status (2026-09-08): the `search-references` host operation, provider configuration,
candidate verification and Task Context panel wiring are implemented. A compatible
external provider must be installed separately. The supported source is Local Markdown;
this contract does not yet accept OpenDocuments or Notion results directly. See
[current integration status](KNOWLEDGE-INTEGRATION-STATUS-2026-09-08.ko.md) for the UI,
reuse boundaries and remaining work. Fixture checks and installed/native acceptance
are distinct; this protocol document alone is not a release acceptance receipt.

Work Stack still builds no index. An explicitly configured local provider
answers one query with candidate spans; the host re-reads every candidate
through the existing Markdown vault reader before anything is shown. The
provider says where to look. It never supplies excerpt text, a Task change or a
command, and the corpus it describes is an immutable snapshot that may already
be behind the vault on disk.

## Bridge operation

`search-references` joins the existing knowledge bridge envelope
(`workstack-knowledge-request` / `workstack-knowledge-response`, schema
version 1). The request carries exactly the existing `binding` (workspace UID,
Task UID, Task ID, Task revision), the `vault_id` of a vault already chosen on
this device, and `query`. No command, executable, root or URL is accepted from
the UI; any additional member refuses the request.

`query` is trimmed, must be nonempty and at most 1,000 characters, and carries
no control characters. Ordinary whitespace -- space, tab, newline, carriage
return -- is text; C0, DEL and C1 characters are refused as `invalid_query`.
The trimmed query is what the provider receives and what the response echoes.

Successful `data` is exactly:

```
{
  "binding": {workspace_uid, task_uid, task_id, task_revision},
  "query": "<trimmed query>",
  "corpus": {"label": string, "document_count": integer >= 0, "indexed_at": RFC3339},
  "matches": KnowledgeReadReference[],   // at most 5
  "omitted_count": integer >= 0
}
```

`matches` are ordinary `KnowledgeReadReference` documents from the existing
reader -- the schema is unchanged -- so a match already carries its vault ID,
relative path, title, line span, `source_sha256`, `freshness`, external-reference
trust and read-only flag. A search excerpt is a preview: it is bounded to 1,200
characters and marked `excerpt_truncated` when cut, so five previews always fit
inside the host's bounded response. Linking a candidate goes through the
existing preview, reason and **Link** path; search does not link anything and
writes nothing to the registry.

The active workspace is checked before and after the operation, exactly as the
existing knowledge operations do, and the Task panel keeps discarding responses
whose workspace, Task identity or revision no longer matches. The UI applies a
90-second request timeout to this operation only.

## What `omitted_count` means

Every candidate is re-read from the vault with its own `source_sha256` as the
expected hash. A candidate is presented only when the reader reports
`freshness: unchanged`. A candidate whose document changed, disappeared, moved,
resolved outside the vault or was never a readable relative Markdown path is
omitted and counted; it is not a current source, and the provider's snapshot is
never allowed to stand in for one. The UI shows a concise note that some hits
were left out; it does not name them.

Verification stops once five matches are confirmed, so `omitted_count` reports
candidates that failed verification, not candidates that were never needed.

## Provider configuration

The optional host-private file `<StateRoot>/knowledge/search-provider.json` is
read through the same bound path chain as the rest of the registry, so a link
planted over it is refused rather than followed. The UI cannot write it; the
coordinator provisions it after review. Its schema is closed:

```json
{"schema_version": 1, "command": ["C:\\tools\\python.exe", "C:\\lab\\knowledge_search.py"]}
```

`command` is one absolute executable followed by fixed arguments: at most 16
parts, each at most 512 characters and free of control characters. The
executable must be an absolute path with no `..` component, and must be a
regular file that is not a symlink or reparse point. The command is launched
without a shell, so an argument is never interpreted.

- No file: `search_unconfigured`. The UI says search is not configured here.
- Any other shape, path or launch failure: `search_unavailable`.
- Runtime budget exceeded: `search_timeout`.
- Unusable provider output: `search_invalid_response`.

Every message stays generic: no vault root, no provider path, no credential and
no provider diagnostic reaches the UI.

## Provider protocol

The provider receives one UTF-8 JSON document on stdin:

```json
{"schema_version": 1, "query": "release notes", "vault_root": "<absolute root of the registered vault>", "limit": 5}
```

It writes one UTF-8 JSON document on stdout:

```json
{
  "schema_version": 1,
  "corpus": {"label": "LightRAG naive (30 docs)", "document_count": 30, "indexed_at": "2026-09-08T09:15:00Z"},
  "candidates": [{"document_path": "notes/review.md", "start_line": 12, "end_line": 34, "source_sha256": "<64 lowercase hex>"}]
}
```

Bounds the host enforces on that document: at most 20 candidates; one-based
line spans of at most 80 lines; lowercase 64-hex digests; a corpus label that
is nonempty, at most 120 characters, single-line and free of path separators,
so a local root cannot be smuggled into the scope line; a document count that
is a plain nonnegative integer; and an RFC 3339 timestamp. Duplicate JSON
members, JSON constants (`NaN`, `Infinity`) and any unexpected member refuse
the answer as `search_invalid_response`.

Bounds the host enforces on the process: at most 75 seconds of runtime, at most
64 KiB of stdout, and bounded stderr that is read only so the provider cannot
block on a full pipe and is then discarded. All three pipes are serviced
concurrently, so a provider that never reads its request cannot wedge the host.
On timeout or oversized output the owned process is terminated, killed if it
does not stop, and reaped; the refusal is reported rather than a partial answer.

This is not a sandbox against a hostile provider -- the coordinator chooses what
runs. What is bounded is the damage a misbehaving provider can do to the host,
and the fact that its claims about documents are always re-checked against the
vault.

## Provider adapter

The coordinator owns
`C:/ws-orca/workstack-wiki-lab/lightrag-experiment/knowledge_search.py`. It
reuses the existing native LightRAG naive retrieval and rerank over its own
30-document snapshot and maps chunk content back to source vault paths and line
spans through immutable document IDs. It generates no answer: the point is to
find evidence, not to write prose. Graph and mixed retrieval stay optional and
later. Because that snapshot is not the whole vault, the corpus label and count
the UI displays must describe the snapshot honestly.

## Testing

`tests/test_knowledge_search.py` covers configuration shapes, the launch and
reaping path with synthetic script providers, response validation, candidate
verification and the registry wiring. `tests/test_knowledge_host.py` covers the
bridge envelope, query bounds, workspace invalidation and the public payload
shape.

```powershell
python -m unittest tests.test_knowledge_search tests.test_knowledge_host
```

Every vault, provider and corpus in those tests is a synthetic fixture. They say
the contract holds against fixtures. They do not say the personal vault, the
real LightRAG snapshot or the packaged desktop build accepts.
