# OpenDocuments source access

An external adapter library for answering two questions about a document Work
Stack already knows by an opaque id: **is the source still what we recorded**,
and **may it be opened now**. It is not an HTTP endpoint, not a connector, and
it opens nothing itself.

Nothing here imports `workstack`. The Capture v1.1 retrieval contract is the
*reason* this module exists — it is where a verified `source_version` would come
from — but the dependency runs the other way, and the vocabulary is deliberately
parallel to `workstack.capture_retrieval`: what a document or an index says
about itself is a claim, and only a fact read at the source becomes a version.

## The trust boundary

A caller says **which document**, never **which file**.

- The only selector is an opaque `document_id` matching `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`.
  A path, UNC share, drive letter, `file:` or `https:` URL is refused as
  `invalid_document_id` — not normalised, not "cleaned up", not looked up.
- Bytes are reachable only through a `SourceMapping` an owner wrote: pinned
  `corpus`, configured `allowed_root`, `relative_location` relative to that
  root, and a `permitted_extensions` allow-list. An id with no mapping is
  `unknown_document`; a mapping filed under another corpus is `corpus_mismatch`.
- Nothing from an index chunk, a `sourcePath` field, retrieved text or model
  output is ever promoted to authority. Those live on the `reported_` side of
  the Capture split and this module does not read them.
- The mapping registry is host configuration held in process. It is **not** Work
  Stack SSOT, it is not exported, it is never written by this module, and it
  cannot be extended after construction — there is no `add`.
- Every public result carries the opaque id, corpus, backend, a closed
  status/code pair and version strings. No absolute path, configured root,
  directory name or file byte appears in it. The validated path exists only
  inside `OpenTarget`, which goes to the injected opener and nowhere else.

## Return schema

`verify_source(...) -> SourceStatus`, `SourceStatus.as_public_dict()`:

```
{
  "document_id":              str,          # opaque, or "<rejected>"
  "corpus":                   str | None,
  "backend":                  "nas" | "notion" | None,
  "status":                   <VERIFICATION_STATUSES>,
  "code":                     <VERIFICATION_CODES>,
  "source_version":           str | None,   # a fact THIS module read
  "expected_source_version":  str | None,   # what the caller brought
  "indexed_digest":           str | None,   # canonical claim, echoed only
  "open_allowed":             bool,         # true iff status == "current"
}
```

`authorize_open(...) -> OpenDecision`, `OpenDecision.as_public_dict()`:

```
{
  "document_id": str, "corpus": str | None, "backend": str | None,
  "opened": bool,                            # opener actually invoked
  "status": <OPEN_STATUSES>, "code": <OPEN_CODES>,
  "source_version": str | None, "expected_source_version": str | None,
}
```

`source_version` and `expected_source_version` are kept apart for the same
reason `capture_retrieval` keeps `reported_` apart from verified provenance:
collapsing them is how an unproven claim becomes an apparent proof.

Statuses: `current`, `stale`, `missing`, `unavailable`, `denied`, `refused`,
`revoked`, `unverifiable` — plus `opened` and `failed` for an open decision.

## What "current" means

Exactly one thing: the permitted file's **actual bytes** were read under a
bounded read, hashed to `sha256:<64 hex>`, and that hash equals the
`expected_source_version` the caller supplied.

- No expected version → `unverifiable` / `no_expected_version`. The computed
  hash is still returned so a caller can record a first baseline; a hash with
  nothing to compare against is not currentness.
- A different hash → `stale` / `hash_differs`.
- An `indexed_digest` **never** makes a document current, even when it equals
  the source hash. It is a digest of what the index holds.
- A question the filesystem could not answer stays `unverifiable`. It never
  degrades into `missing`, because "I could not tell" and "it is gone" lead a
  human to opposite actions -- and it never hardens into a *specific* finding
  such as "a reparse point" or "the share is offline" either.

### `indexed_digest` is a claim with a required shape

An index digest is accepted only as canonical `sha256:` followed by exactly 64
lowercase hex characters, or not at all. Anything else -- an absolute path, a
UNC share, a URL, bare hex, an uppercased digest, a wrong length, a trailing
newline, a non-string -- is `refused` / `invalid_indexed_digest`, and the text
that was offered is **not** carried into the result: `indexed_digest` is `None`
in the public projection of that refusal. Otherwise the projection that is
documented above as path-free would have been a way to get an arbitrary
absolute path echoed back into a log or a UI.

A well-formed digest is preserved verbatim beside the answer and remains an
unverified claim. It is never freshness authority, and it takes no part in any
status decision -- a canonical digest equal to the real source hash still leaves
a document `unverifiable` when no expected version was supplied.

(The patterns are anchored with `\Z`, not `$`. In Python `$` also matches
immediately before one trailing newline, so a `$`-anchored digest pattern would
have accepted `sha256:<64 hex>\n`.)

For a Notion mapping, `source_version` is whatever the injected verifier
attests (bounded opaque text), not a SHA-256.

## NAS containment

Two containment checks run, not one, because either alone is defeatable —
lexical analysis cannot see a junction, and resolution alone would follow one
out. A mapped `relative_location` is refused when it is:

| shape | code |
| --- | --- |
| `../x.pdf`, `a/../../x.pdf`, `./x.pdf` | `location_traversal` |
| `/etc/passwd`, `\win.ini`, `\\host\share\x.pdf`, `//host/share/x.pdf` | `location_absolute` |
| `C:\x.pdf`, `C:x.pdf` (drive-relative), `x.pdf:stream` (alternate data stream) | `location_drive_qualified` |
| `*`, `?`, `<`, `>`, `\|`, `"`, NUL, control characters | `location_forbidden_character` |
| `NUL.pdf`, `con.pdf`, `COM1.pdf` … | `location_reserved_name` |
| `x.pdf ` / `x.pdf.` (Windows silently strips these) | `location_trailing_dot_or_space` |
| `a//b.pdf` | `location_empty_segment` |
| normalised join leaving the root, or a resolution that really lands outside it | `location_escapes_root` |

`:` is refused anywhere, which removes the absolute path, the drive-relative
path and the NTFS alternate data stream with one rule.

**Links and reparse points.** Every component *below* the root is checked and
refused as `symlink_component` or `reparse_component`. `os.path.islink` is not
sufficient on Windows: a directory junction, which any unprivileged user can
create, reports `islink() == False` while still redirecting the descent, so the
`FILE_ATTRIBUTE_REPARSE_POINT` attribute decides. The root itself is exempt — it
is the owner's own designation, and an owner may legitimately approve a normal
mapped drive or a junction that stands for the share.

A component whose link status could not be read is never assumed safe — and
never asserted to be a reparse point either. The two answerable reasons keep
their own codes and their own statuses:

| what the OS said about an ancestor | status / code |
| --- | --- |
| it is a symlink | `refused` / `symlink_component` |
| it is a reparse point (junction) | `refused` / `reparse_component` |
| `PermissionError` reading its metadata | `denied` / `component_access_denied` |
| any other `OSError` | `unverifiable` / `indeterminate_access` |

All four stop the descent and no read happens, so the fail-closed behaviour is
identical. What differs is what a human is told: "this share has a junction in
it" and "I was not allowed to look" send an operator to different places.

**An unanswered resolution is not an escape.** The resolved containment check
asks the OS to place two paths — the approved root and the mapped target — and
that question can be refused or fail unexplained just like the metadata read
above it. `location_escapes_root` asserts something stronger than "the read
stopped": it asserts that containment *was* established and *did* fail, which
is a statement about the owner's mapping. So the same two codes apply, from
either side of the query:

| what the OS said when placing the root or the target | status / code |
| --- | --- |
| both resolved, and the target is outside the root | `refused` / `location_escapes_root` |
| `PermissionError` resolving either one | `denied` / `component_access_denied` |
| any other `OSError` resolving either one | `unverifiable` / `indeterminate_access` |

All three refuse the read and disable the open. Only the first one is a finding
about the mapping; the other two are findings about the machine, and telling an
owner their approved location escapes the root when the share merely denied the
query would send them to rewrite a mapping that is correct.

**Extensions.** The default allow-list holds ordinary document formats. It
excludes executables, scripts and shortcut/launcher formats, and
`ALWAYS_REFUSED_EXTENSIONS` outranks configuration: an owner may narrow the
allow-list, never widen it back into `.exe`, `.bat`, `.ps1`, `.lnk`, `.url`,
`.scr`, `.js` and the rest. The extension is read the way the OS reads it, so
`quarterly.pdf.exe` is an `.exe`.

**Availability is not existence.** An absent or non-directory root is
`unavailable` / `root_unavailable`; a live root with no such file is `missing` /
`file_absent`; an OS refusal is `denied` (`root_access_denied` or
`file_access_denied`); an undifferentiated `OSError` is `unverifiable` /
`indeterminate_access`. These are four different answers on purpose.

The root is called *unavailable* on the strength of an `OSError` only when the
error actually says the share or host did not answer — `EHOSTUNREACH`,
`ENETDOWN`, `ENODEV`, `ETIMEDOUT` and their neighbours, the shapes a
disconnected mapped drive or a dead NAS produces. Any other `OSError` is
undifferentiated and stays `unverifiable`: inferring "offline" from an
uncharacterised error would send someone to check a network link that may be
perfectly healthy.

**Bounded read.** 64 MiB (`MAX_VERIFIED_BYTES`). Oversized → `unverifiable` /
`file_too_large`. Bytes are consumed a chunk at a time into the digest and
dropped; no file body is retained, persisted, logged or returned anywhere. Size
and mtime are compared across the read from the same descriptor, and a
mismatch — or a byte count that disagrees with the final size — is
`unverifiable` / `changed_during_read`, never a hash.

## Opening

`authorize_open` is a **separate operation** and does not trust an earlier
verification. Mapping, corpus, revocation, containment, accessibility and the
expected source version are all revalidated inside the call, immediately before
the callback runs. Anything short of `current` — offline, denied, revoked,
stale, missing, unverified — disables the open. A recognised but revoked mapping
denies even when the file is still present: the withdrawal is the authority, not
the file's continued existence.

**One resolution, and the version check comes last.** The mapped target is
resolved exactly once per call, and the hash-and-compare is the last thing that
happens to it: the `Path` handed to the opener is the *same* path that was
resolved, stat-ed and hashed, so `source_version` always describes the file the
callback receives. There is no second resolution after the comparison, because
a second one could observe a file that appeared *after* the hash and pair it
with the version of the file observed before it. A consequence worth stating: an
ordinary edit landing while the target is being resolved is seen by the hash
that follows it, and the answer is `stale` / `hash_differs` with the callback
never called.

The injected opener is called as `opener(target: OpenTarget) -> None`:

```
OpenTarget(document_id, corpus, backend, source_version,
           path: Path | None,   # NAS: validated, absolute, inside the root
           url:  str  | None)   # Notion: the pinned, allow-listed HTTPS URL
```

Exactly one of `path` / `url` is set. **No command string is built and no shell
is involved anywhere in this module.** A `str` passed as `opener` is refused as
`invalid_opener` rather than interpreted. An opener that raises yields `failed` /
`opener_failed`, and its message — which may name the path — is not echoed.

### Remaining exposure, stated plainly

- **External-app open TOCTOU.** A write that lands **after** the final hash is
  not detected — while the callback runs, or after the external application has
  the path. A process running as the same OS user with write access inside the
  approved root can replace the file at that point. That gap is inherent to
  handing a path to another program: closing it would require path-to-external-
  application semantics the filesystem does not offer, and it is **not** closed
  here. What is promised is narrower and exact: the version reported is the
  version of the bytes the callback was handed.
- **Not atomic sandboxing.** Containment, reparse and extension checks are made
  at the moment they are made. This slice does not promise race-free filesystem
  security against an adversary who is already the same OS user, and must not be
  described as if it did.
- The root policy may itself designate a normal approved drive. Approving one is
  the owner's decision, and this module does not second-guess it.

## Notion: a seam, not an integration

There is **no Notion connector, client, API token or network call in this
packet**. What exists is:

1. A pinned HTTPS URL on the mapping, validated by `unsafe_notion_url_code`:
   `https` only; host exactly `notion.so` / `www.notion.so` or an exact
   `.notion.site` suffix; no userinfo, no port, no query, no fragment; ASCII
   host with no `xn--` label; bounded, safe path. Query and fragment are refused
   rather than stripped — a pinned URL that needed them was not pinned, and
   silently editing an owner's URL would make the mapping mean something the
   owner did not write. Host comparison is case-insensitive per RFC 3986, so
   `WWW.NOTION.SO` **is** the allow-listed host.
2. An injected trusted verifier seam:

```
OriginVerifier = (OriginRequest(document_id, corpus, url)) -> OriginAttestation | None
OriginAttestation(document_id: str, source_version: str | None)
```

The request carries the opaque id and the *pinned* URL from the mapping, so a
verifier cannot be steered at another page by anything a caller passed. An
attestation must bind to the same `document_id` or it is
`verifier_document_mismatch`. Without a verifier the answer is `unverifiable` /
`no_origin_verifier` and opening is disabled — never an invented verification.
A verifier that returns no version is `verifier_no_version`; one that raises or
returns a foreign object is `verifier_failed`, with its message not echoed.

**Unimplemented origin adapters** (none ship here): a Notion API
`last_edited_time` / page-version verifier, an OAuth or integration-token
credential source, a NAS SMB/DFS remote metadata verifier, and any web fetcher.
Each would be a host-supplied implementation of `OriginVerifier`; adding one
requires no change to Work Stack Capture, because the fact this adapter produces
already has the shape `VerifiedSource` consumes.

## Not covered by this slice

Authentication and transport admission, the host-issued request ledger and
replay refusal, credential storage, UI, migration, and the Capture 1.1
projection or its storage. Those are the caller's obligations, unchanged.

## Where the code lives

One contract, four files. The split is structural only: no behaviour, status,
code, signature or refusal changed with it, and `source_access` re-exports every
supported name, so `from integrations.opendocuments.source_access import ...`
is exactly what it was.

| File | Holds | Public? |
| --- | --- | --- |
| `source_access.py` | The Notion seam (`_verify_notion`), the three entry points `verify_source` / `resolve_notion_source` / `authorize_open`, and the open-decision projection. | **Yes — the only supported import site.** |
| `source_access_types.py` | The closed status/code vocabulary, opaque-id and version patterns, the Notion host allow-list and `unsafe_notion_url_code`, `SourceMapping` / `SourceMappingRegistry`, `SourceStatus` / `OpenTarget` / `OpenDecision`, the `OriginVerifier` seam, and request shaping (`_resolve_request`). Touches no filesystem, no network and no opener. | Internal. |
| `source_access_nas.py` | Reaching the mapped file and reading it: the 64 MiB cap and chunk size, `_root_state`, `_nas_target`, the bounded `_hash_permitted_file`, and `_verify_nas`, which resolves the mapped target **once** and returns that same path with a `current` answer. | Internal. |
| `nas_paths.py` | Containment: location text and segment rules, the extension allow-list, the lexical join, reparse/junction descent and resolved containment. | Internal. |

Import direction is one way and acyclic:
`nas_paths` ← `source_access_types` ← `source_access_nas` ← `source_access`.

Two consequences worth stating, because they are the only observable effects of
the move:

- The bounded-read limits `MAX_VERIFIED_BYTES` and `READ_CHUNK_BYTES` are now
  *defined* in `source_access_nas` and re-exported from `source_access`. Reading
  either name from `source_access` is unchanged; **rebinding** one there no
  longer affects the reader, which reads its own module global. A test that
  wants to shrink the cap patches `source_access_nas`.
- The same applies to the internal seams `_read_chunk` and `_nas_target`: they
  live in `source_access_nas` and are patched there.
