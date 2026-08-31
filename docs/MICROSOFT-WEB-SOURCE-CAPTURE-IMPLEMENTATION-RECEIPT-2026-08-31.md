# Microsoft Web Source Capture Implementation Receipt

## Reviewed coordinate

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Worktree: `.worktrees/source-providers` under the local Work Stack workspace root
- Branch: `codex/workstack-source-providers-20260831`
- Base: `4ef51e6be4464f48a7e8a4cb18d2d860ffd1bdb2`
- Product commits:
  - `b52cbcdff8867ef86b868810239ba677076687bc` (`feat: add reviewed Microsoft web source capture`)
  - `d38af1501daf26d12e52963dc5e659e1ba83cd4e` (`chore: normalize source capture files`)
- Product tree: `da3d306dba617609a1a3e36625e1d92556f348db`
- Push status: pushed to `origin/codex/workstack-source-providers-20260831` after explicit user authorization.

## Delivered behavior

The Source Inbox now exposes a provider registry for Outlook, Teams, and OneNote. Each provider opens the original Microsoft web application and offers an explicit reviewed capture path. The common review dialog lets the user edit the Task title and action detail, select one Objective, set priority and due date, and review or omit the source URL before any planning state is created.

The browser integration is a bounded Manifest V3 extension under `integrations/microsoft-web-capture`:

- Outlook prefers selected text.
- Teams and OneNote accept selected text when exposed and otherwise read text only after the user clicks the capture button following an explicit copy.
- The extension holds one pending draft in extension-local storage, opens Source Inbox, waits for a same-origin acknowledgement, and deletes the pending draft.
- The extension never calls the planning API, monitors the clipboard, stores Microsoft credentials, or claims OOB verification.

Work Stack converts the reviewed draft into the existing Capture Packet contract with `provider: manual`, `capture_mode: manual`, adapter `microsoft-web-capture`, and resource type `microsoft-web.<provider>`. It then uses the existing capture ingest and capture-to-Task APIs. A capture identity is fixed when the review dialog opens, so an explicit unchanged retry reuses the same capture coordinate rather than creating a new source.

## Changed paths

- `frontend/src/app/App.tsx`
- `frontend/src/features/inbox/InboxPage.tsx`
- `frontend/src/features/inbox/InboxPage.test.tsx`
- `frontend/src/features/inbox/SourceCaptureDialog.tsx`
- `frontend/src/features/inbox/sourceCapture.ts`
- `frontend/src/features/inbox/sourceCapture.test.ts`
- `frontend/src/features/inbox/sourceProviders.ts`
- `frontend/src/styles.css`
- `integrations/microsoft-web-capture/README.md`
- `integrations/microsoft-web-capture/manifest.json`
- `integrations/microsoft-web-capture/source.js`
- `integrations/microsoft-web-capture/source.css`
- `integrations/microsoft-web-capture/bridge.js`
- `integrations/microsoft-web-capture/options.html`
- `integrations/microsoft-web-capture/options.css`
- `integrations/microsoft-web-capture/options.js`

## Verification evidence

- Frontend: 40 files, 205 tests passed.
- Python: 150 tests passed, 1 environment-dependent symbolic-link test skipped.
- Production build: passed (`tsc -b` and Vite); only pre-existing dependency annotation and large-chunk warnings remained.
- Source export audit: passed, 287 UTF-8 source-policy files.
- Extension static checks: all three JavaScript entry points passed syntax checks and the manifest parsed successfully.
- Browser product test: on isolated data at `http://127.0.0.1:8792`, the Source Inbox displayed Outlook, Teams, and OneNote. A reviewed Teams draft created `T-0031`, retained the selected Objective, priority, detail, safe Microsoft source URL, and one linked context item, then opened the new Task in Workspace.
- Retry regression: a forced first transport failure followed by an explicit retry retained the same `capturedAt` identity.
- `git diff --check`: passed after normalization.
- Windows rollout: the checksum-verified 1.0.0 setup updated the installed product, preserved the configured planning-data directory, created verified pre-launch backups, and returned `ready` from `/api/v1/health` on port 8765. The desktop shortcut was replaced with the installed launcher, which starts the server before opening the browser.

## Compatibility and privacy boundaries

- The frozen Work Stack-to-Conduit docking contract and Conduit export behavior were not changed.
- Work Stack remains the sole planning-state authority; export remains read-only and this feature does not contact Conduit.
- Existing Microsoft Gate 0 controls remain fail-closed. Browser-selected content is not labeled OOB verified.
- Source URLs are retained only for HTTPS Microsoft hosts already allowed by the product boundary. Credential- or recipient-shaped locators are refused by the established capture validator. Personal `teams.live.com` capture still works, but its URL is intentionally omitted from persisted planning context because it is outside that frozen host allowlist.
- No raw email, message thread, recipients, attachments, hidden DOM content, cookies, or Microsoft tokens are collected.

## Remaining bounded work

- Load the unpacked extension in the target Edge/Chrome profile and set its Source Inbox URL. Remote Linux use requests permission only for the configured Work Stack origin.
- Validate corporate tenant URLs for Outlook, Teams, OneNote, and SharePoint in the actual company environment; add only proven host patterns.
- Package or policy-deploy the extension after the unpacked pilot. This implementation does not silently install a browser extension.
- Rich provider metadata, background activity feeds, replies, attachments, and Graph/OOB synchronization remain separate future capabilities. They are not required for the explicit content-to-Task path delivered here.

## Explicit nonclaims

This receipt does not claim that Microsoft web apps are embedded inside the browser-hosted Work Stack page, that Teams exposes selectable message DOM, that all OneNote surfaces share one URL shape, that live Microsoft activity is synchronized, or that the branch has been independently merged. No planning data or the six protected dirty paths in the original checkpoint worktree were modified.
