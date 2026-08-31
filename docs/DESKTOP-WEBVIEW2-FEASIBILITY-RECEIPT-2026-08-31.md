# Work Stack Desktop WebView2 Feasibility Receipt

Date: 2026-08-31

## Outcome

The lightweight desktop direction is functionally feasible, while normal installation remains policy-gated on this PC. Work Stack can keep its existing Python server and React product while using the installed Microsoft Edge WebView2 Runtime as its own Windows application window. Electron, Rust, the .NET SDK, Visual Studio, and a second product backend are not required for this bounded shell.

The recommended rollout is staged:

1. Keep the primary shortcut on the reliable isolated Chrome app-window launcher.
2. Use the separate WebView2 preview shortcut to test real company-tenant authentication and Conditional Access.
3. Add an approved signing or application-control deployment path.
4. Promote WebView2 into the shipping installer and primary shortcut only after both environment gates pass.

## Reviewed product coordinate

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Branch: `codex/workstack-source-providers-20260831`
- Product commit: `103e2f102300c6c9c6c8c88bed74e28bdfde6b6b`
- Product tree: `907942f387c220861443839aa1ae175ded174d19`

## Implemented slice

- `desktop/webview2-shell/WorkStackShell.cs`
  - owns a native Windows window and top-level WebView2;
  - exposes Work Stack, Outlook, Teams, and OneNote navigation;
  - uses an explicit loopback/Microsoft HTTPS navigation allowlist;
  - stores the isolated WebView profile under `%LOCALAPPDATA%\WorkStack`;
  - disables developer tools, password autosave, and general autofill;
  - does not read page content, cookies, credentials, tokens, recipients, or hidden DOM;
  - starts the installed Work Stack server only when needed and stops it only when the shell started it.
- `desktop/webview2-shell/Build-WorkStackShell.ps1`
  - builds with the Windows .NET Framework 4.8 compiler already on the machine;
  - consumes pinned WebView2 SDK `1.0.4129.50`;
  - verifies package SHA-256 `d3934f482d484b89fb4825df720c710664e1143a1e90f7b3a60794ef33f473d2`.
- `scripts/windows/Start-WorkStack.ps1`
  - replaces the unreliable normal-browser launch with a visible isolated Chrome/Edge app window.

## Runtime evidence

- Installed Evergreen WebView2 Runtime: `151.0.4129.107`.
- Functionally exercised preview SHA-256: `2e180e06f1691ed500dd22a7ba3e67be3c6928f36679d0bdb22fbbba61ce497c`.
- Clean post-commit-source verification build SHA-256: `c3e011a03bab1ed7d77634102059a16ccb3a0163539858e5891a92eb1e0966ac`.
- Existing-server lifecycle gate: closing the shell left the pre-existing server ready.
- Owned-server lifecycle gate: after the shell started a stopped server, a normal shell close stopped that server.
- Content-blind top-level navigation probes:
  - Outlook: success, final host `outlook.office.com`;
  - Teams: success, final host `teams.microsoft.com`;
  - OneNote: success, final host `login.microsoftonline.com`.

These probes record only success, final host, WebView2 runtime version, and navigation error enumeration. They do not inspect page content or authentication material.

The clean rebuilt executable and the same files copied into a dedicated `%LOCALAPPDATA%` preview directory were blocked at process start with the Windows message "application control policy blocked this file." The previously exercised preview executable remains runnable. This is evidence of a new-binary trust or allow-policy gate, not a WebView rendering failure. No attempt was made to bypass that policy.

## Automated verification

- Python product suite: 154 tests passed; 1 environment-dependent symbolic-link test skipped.
- Frontend suite: 40 files and 205 tests passed.
- Frontend production build: passed.
- WebView2 shell clean rebuild: passed.
- Source privacy/export audit: passed for 289 UTF-8 text files before this receipt was added.
- Staged diff whitespace audit: passed.

The production build retains the existing warning that some generated JavaScript chunks exceed the configured size advisory. It is not introduced by the desktop shell.

## Remaining promotion gate

Two independent promotion gates remain:

1. The user must open Outlook, Teams, and OneNote from the WebView2 preview and confirm that the actual company tenant permits interactive sign-in, session persistence, and its Conditional Access flow. Successful public top-level navigation does not prove that organization-specific policy will permit the embedded profile.
2. The shipping executable needs a deployment method accepted by Windows application control, such as an approved code-signing certificate, enterprise allow policy, or another administrator-approved packaging location. Copying a newly built unsigned executable into a normal per-user install directory is not sufficient on this PC.

If the tenant blocks embedded authentication, the fallback remains the browser-extension capture model already proven for Outlook and Teams. Work Stack must not scrape OAuth tokens or copy browser credentials to bypass that policy.

## Explicit nonclaims

- The preview shell is not yet installed by the shipping Work Stack installer.
- The primary shortcut has not yet been promoted to WebView2.
- A newly rebuilt unsigned shell is not currently accepted by this PC's application-control policy.
- Microsoft sign-in, Conditional Access, and company-tenant session persistence have not yet been accepted by the user.
- The shell does not inject capture controls into Microsoft pages.
- No Conduit client, docking change, cloud relay, polling daemon, back-sync, or bulk import was added.
- No planning state, task content, or SSOT data was changed by this implementation or its probes.
