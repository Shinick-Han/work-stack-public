# Work Stack 1.0.4 Microsoft sign-in receipt

## Outcome

Work Stack 1.0.4 replaces the former same-view popup rewrite with a real child
WebView2 authentication surface. Outlook, Teams, and OneNote now share one
persistent Microsoft-only WebView2 environment with Windows primary-account SSO
enabled. A recognized Microsoft authentication transition may follow HTTPS
enterprise federation redirects until it returns to the selected provider.

## Privacy boundary

The desktop host writes a bounded diagnostic log at
`%LOCALAPPDATA%\WorkStack\logs\microsoft-webview.log`. Each JSON line contains
only time, event, provider, scheme, hostname, decision, success, stage, and
WebView2 error status. URL paths, queries, fragments, page titles, selected
content, credentials, tokens, cookies, recipients, and attachments are never
written to this log.

## Verification

- Python: 203 tests passed; 1 platform-dependent symlink test skipped.
- Frontend: 45 files and 227 tests passed.
- Production React build: passed.
- Source export audit: 307 UTF-8 files passed.
- Installed-runtime probe: Outlook and Teams each initialized in the shared
  Microsoft environment and completed top-level navigation successfully.
- Restart probe: the same persistent Microsoft profile re-opened successfully.
- Installer verification: `WorkStack-Setup-1.0.4.ps1` passed the strict adjacent
  checksum verifier and an isolated install/upgrade/launch/owned-server-stop
  smoke test.

Final setup artifact before publication:

- bytes: `24353147`
- SHA-256: `9a39cf07738b2c2299fec805279725c3d98aafe779d9f864f86924f024aa420c`

## Nonclaims

- Local probes do not prove that every Conditional Access or custom federation
  policy in the company tenant will accept an embedded WebView.
- The first company-PC acceptance criterion is successful interactive sign-in
  to both Outlook and Teams followed by a tab switch and application restart.
- If that acceptance still fails, the content-blind diagnostic log is the next
  evidence source; no message content or token should be requested.
- This release does not change planning-state authority, source-capture consent,
  remote SSH ownership, or the Work Stack/Conduit docking contract.
