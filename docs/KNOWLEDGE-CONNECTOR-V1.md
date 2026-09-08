# Task-bound Markdown references

Status (2026-09-08): the read-only reference API/CLI, Task Context panel,
device-local persistence, explicit external document search and selected-reference
Task brief are integrated and installed in 1.0.10. Independent review and actual
packaged WebView2 search/link/copy composition passed; the vault was pre-registered,
so native folder-dialog interaction remains unverified. Personal references were
preserved and installed-host search was checked. Search currently covers an
external 30-document snapshot, not the full vault. See the
[delivery evidence](WORKSTACK-KNOWLEDGE-SEARCH-DELIVERY-2026-09-08.ko.md).
T-0028's product-direction decision is complete; T-0033 remains in progress for
real-use validation and bounded UX follow-ups. See the
[current ROI backlog](WORKSTACK-ROI-BACKLOG-2026-09-08.ko.md).

Work Stack retains the relationship between work and evidence. Obsidian or an LLM
Wiki retains the documents. This adapter reads one explicitly named `.md` document;
it does not crawl a vault, build an index, invoke a model, execute Markdown, access
the network, open a live Work Stack Store or write the source.

## Invocation

Run from a source checkout, using a Task exported through the existing read API:

```powershell
python -m workstack.knowledge_context --task-export task.json --workspace-uid 66666666-6666-4666-8666-666666666666 --vault-root ./sample-vault --vault-id personal-wiki --document projects/review.md --start-line 2 --end-line 12
```

The Task export may be a bare Task, `data.task` or `data`. It must contain `id`,
canonical nonzero UUID `uid`, and a nonnegative integer `revision`. The workspace
UID is supplied explicitly. This binds output to a supplied snapshot; it does not
prove the exported snapshot is still the live Task revision. The Task panel
discards responses whose workspace/Task identity or revision no longer matches.

Supply `--expected-sha256 <previous source_sha256>` to distinguish unchanged and
changed source bytes. With no prior hash, freshness is `uncompared`, not verified
fresh. This hash covers the original UTF-8 bytes, including a BOM or line endings.

Output schema `workstack.knowledge-context.v1` carries the Task/workspace binding
and one `markdown-vault` reference: vault ID, relative document path, bounded title,
requested one-based line span clipped to the file, plain-text excerpt, truncation
flag and source revision. No absolute vault path is included in the output.

## Read and trust boundaries

- Explicit root and relative Markdown path; no arbitrary URL, shell or external opener.
- 512 KiB maximum document, 80 requested lines, 6,000 excerpt characters, 64 KiB Task export.
- Reject traversal, absolute paths, Windows alternate data streams and device names,
  hidden components, symlinks/reparse points, nonregular files, invalid UTF-8 and NULs.
- Check path/handle identity and metadata before and after reading; discard output
  if observed changes occur. This is not a sandbox against a privileged hostile
  process replacing and restoring filesystem paths between checks. Only choose
  a user-controlled local vault; a stronger OS handle-based authority is needed for
  adversarial shared folders.
- Missing/renamed documents are not silently redirected to another same-named file.
- Excerpts have `trust: external_reference`. Consumers must render as text and must
  not promote embedded instructions to agent/system instructions. This is not the
  existing sanitized Outlook/Teams Capture Packet contract.
- The desktop registry binds its minted vault ID to the explicitly chosen root.
  The standalone CLI uses caller-managed IDs and does not establish persistent identity.

Errors contain closed codes without raw source content or absolute paths. Missing,
unreadable, changed-during-read, empty, out-of-range and invalid inputs are separate.

## Desktop use and persistence

From a Task's **Resume** tab, open **View all context**, then use
**Knowledge sources → Choose folder** to select a local Markdown folder,
enter a vault-relative `.md` path and line range, and select **Preview**. Add a
reason and select **Link** to keep the reference. **Read** checks the current source
against the linked hash; **Unlink** removes only the saved reference.

The device registry stores selected roots and reference metadata under the desktop
StateRoot's `knowledge` directory. It stores no excerpts, is partitioned by workspace
and Task UUID, and is excluded from workspace sync and backup. The UI labels this
device-only boundary. A browser without the desktop bridge cannot read local vaults.

Registry operations bind directories at and below the host-designated StateRoot
for the duration of the operation. Windows handles deny deletion/rename while bound;
POSIX operations use directory descriptors. This registry-write protection is
separate from the source-reader limitations described above. Corrupt state is
refused rather than reset. Source documents are never modified.

## Selected reference context

Open the Task context view, select up to eight linked references and choose
**Prepare resume brief**. Preparation checks the current saved metadata, reads each
selected span and checks the live workspace and Task revision before and after.
Removed or changed saved records stop preparation. Source changes require explicit
acknowledgment before **Copy JSON** or **Download JSON**.

The supplementary `workstack.knowledge-context.v1` envelope is bounded to 32 KiB
UTF-8 and preserves source hashes, freshness, external-reference trust and
`generated: false`. It accompanies the existing Task handoff; it does not change the
canonical Task CLI or Conduit contract, invoke a model or send to an agent. The
prepared snapshot can become outdated afterward. Changing owners or closing the
panel invalidates pending work, including a deferred clipboard fallback; an already
submitted native clipboard operation cannot be revoked.

## Next slice

External related-document search is now described in [KNOWLEDGE-SEARCH.md](KNOWLEDGE-SEARCH.md).
See [integration status](KNOWLEDGE-INTEGRATION-STATUS-2026-09-08.ko.md) for the current
scope and remaining connector work. Packaged desktop acceptance must still identify
the exact installed build. This Markdown connector does not relax Capture URL rules,
write source documents, store Notion credentials or require an LLM Wiki Compiler.
