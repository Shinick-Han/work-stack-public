# Desktop Context Inbox Embedded Source Receipt

Date: 2026-08-31

## Decision

The shell-level Work Stack, Outlook, Teams, and OneNote toolbar was the wrong product boundary and has been removed. Work Stack is now the permanent application surface. Context Inbox owns the Microsoft provider tabs and the rectangle in which a second native WebView2 is composed.

An ordinary iframe is not used. The React UI reports only the selected provider and its visible layout rectangle to the native shell. The shell positions a separate Microsoft-only WebView2 over that rectangle. Leaving Context Inbox hides the native Microsoft surface.

## Product coordinate

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Branch: `codex/workstack-source-providers-20260831`
- Product commit: `0721b19abc34a611d6e72f6918223ba20efce1e7`
- Product tree: `6e8a8c5e16a2e3480aaf550a4c7635dc79a01121`

## Implemented behavior

- Context Inbox renders Outlook, Teams, and OneNote as its own tabs when the WebView2 host bridge is present.
- Switching a Context Inbox provider sends only a fixed provider key and rounded layout coordinates.
- The Work Stack WebView accepts only the configured loopback origin.
- The Microsoft WebView accepts only allowlisted Microsoft HTTPS hosts.
- The Microsoft WebView is clipped through a native viewport panel, so partial page scrolling does not redraw the source from the wrong origin.
- Leaving Context Inbox sends an explicit hide message.
- The existing browser product retains the three external-web-app cards and reviewed clipboard capture fallback.
- The shell no longer has a provider navigation toolbar.

## Verification

- Python suite: 155 tests passed; 1 environment-dependent symbolic-link test skipped.
- Frontend suite: 41 files and 208 tests passed.
- Frontend production build: passed.
- WebView2 shell compilation: passed.
- Compiled shell SHA-256: `9a16c4312dd4fa4219fb5a087b690a71d56858cfbdf73536668f0026fb9993fe`.
- Source privacy/export audit: passed for 292 UTF-8 text files before this receipt was added.
- Normal-browser Context Inbox fallback was inspected on a disposable loopback demo store and retained its existing provider cards and capture actions.
- Staged whitespace audit: passed.

## Remaining gates

The newly compiled unsigned shell cannot be executed on this PC because Windows application control blocks new unsigned executable hashes. The composition code therefore has compile, unit, component, build, and source-policy evidence, but not a direct runtime screenshot of the final two-WebView composition.

Before promotion to the installed product:

1. provide a code-signing or administrator-approved application-control deployment path;
2. run the exact signed build and inspect provider switching, scroll clipping, resize, route exit, and relaunch;
3. confirm Outlook, Teams, and OneNote company-tenant sign-in and session persistence;
4. package the signed shell and updated frontend into the installer;
5. replace the primary shortcut only after those gates pass.

## Explicit nonclaims

- This commit has not been pushed.
- The installed Work Stack application and primary shortcut have not been replaced by this composition build.
- The final two-WebView composition has not run under the current application-control policy.
- No Microsoft page content, cookie, credential, token, recipient, or hidden DOM is read by the bridge.
- Clipboard capture remains explicit and reviewed; no clipboard monitoring was added.
- No Conduit client, docking change, daemon, cloud relay, polling, back-sync, or bulk import was added.
- No planning data or SSOT content was changed by the implementation or verification.
