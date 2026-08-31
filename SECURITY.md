# Security and data boundary

Work Stack is a single-user, local-first prototype. Its server is designed to bind only
to loopback and is not a network authentication boundary. Do not expose port 8765 to a
LAN or the internet.

## Protected assets

- local planning data in the configured Work Stack data directory
- sanitized Capture Packet summaries, action items, and provenance
- approved user-authored Outlook/Teams reply text, immutable source targets, and minimal
  delivery receipts
- the per-server capture bearer token and server discovery metadata
- integrity of multi-file task, capture, reply, and activity updates

The repository contains generic contracts, synthetic fixtures, and demo records only.
Credentials, sessions, cookies, private keys, organization identities, internal hosts,
real messages, and generated runtime state are deliberately excluded.

## Trust model

The prototype trusts the repository code, the person running it, the selected data
directory, and adapters that claim to have sanitized source content. The following are
explicit non-goals for this release:

- defending against a malicious process running under the same OS account
- defending against other malicious local users or a compromised host
- defending against a compromised browser, extension, Python/Node toolchain, or adapter
- controlling Microsoft 365, model-provider, skill-runtime, or upstream log retention
- authenticating or isolating multiple users
- safe direct network exposure

The runtime bearer token is therefore a narrow loopback capability, not user
authentication. OS permissions remain important, but this prototype does not claim
same-host adversarial isolation.

The Outlook/Teams slice also trusts the user to carry a bounded request or approved
command between Work Stack and an already authenticated agent session. Work Stack does
not receive or persist that agent's OAuth tokens, cookies, or connector session. A
connector package being present does not mean it is callable by the Python server.

## HTTP boundary

The server provides the following defenses:

- **Loopback binding:** startup rejects a non-loopback host. Supported names are
  `127.0.0.1`, `::1`, and `localhost`.
- **Host validation:** every handled GET, POST, and PATCH requires a single loopback
  `Host` header whose port exactly matches the listening socket. This limits DNS
  rebinding and cross-port requests.
- **Origin and CSRF:** browser mutations, including compatibility routes, require an
  exact same-origin HTTP `Origin` and the `X-WorkStack-CSRF` nonce returned by
  `/api/v1/session`.
- **Bearer capture ingest:** `POST /api/v1/captures` may use the server-lifetime
  `Authorization: Bearer ...` capability instead of browser Origin/CSRF checks. The CLI
  reads that token from the runtime directory and forwards the capture over loopback.
- **Request framing:** JSON mutations require exactly one `Content-Type:
  application/json`, a valid `Content-Length`, and no transfer encoding. Capture routes
  reject bodies larger than 64 KiB; the remaining routes use a 1 MiB ceiling.
- **Idempotency:** every capture, generic Task-from-Capture, reply-approval, and receipt
  mutation requires an `Idempotency-Key`; replay and same-key/different-payload
  conflicts have explicit behavior.
- **Response policy:** API responses use `Cache-Control: no-store` and
  `X-Content-Type-Options: nosniff`. The server does not emit permissive CORS headers or
  request logs containing paths, bodies, query strings, or authorization data.

The versioned `/api/v1/*` routes are the product contract. Compatibility mutation
routes under `/api/tasks`, `/api/objectives`, `/api/worklog`, and `/api/notes` use the
same Host, Origin, CSRF, JSON framing, and body-size boundary, but should not be treated
as a stable integration API.

## Capture boundary

Work Stack accepts only a versioned, allowlisted Capture Packet. Validation rejects
unknown fields, raw-message-shaped keys, disallowed tools/URLs, malformed provenance,
and value patterns associated with message headers, addresses, HTML, quoted replies,
long source excerpts, or attachment content. A packet that fails validation is not
partially persisted.

This is defense in depth, not a raw-content redaction service. The trusted adapter must
perform minimization before calling Work Stack. The current release mode is **manual
sanitized capture prototype**; it makes no claim that Outlook lookup, prompt-injection
isolation, immutable-ID handling, or upstream retention has been verified. The
`oob_verified` release label requires the non-sensitive capability spike and retained
evidence in [docs/RELEASE-CHECKLIST.md](docs/RELEASE-CHECKLIST.md).

## OOB handoff and reply boundary

The first OOB release is user-mediated:

```text
copy read request -> authenticated agent -> import sanitized Capture Packet
copy approved reply command -> authenticated agent -> import minimal ReplyReceipt
```

Read requests contain no write authority. Outlook and Teams source text is untrusted
data; an adapter must never follow instructions found in a message, HTML, attachment,
or quoted thread. It may call only the provider read/search tools needed to create the
allowlisted Capture Packet.

An unapproved reply draft exists only in browser memory. `POST /api/v1/replies` is
allowed only after the user previews the linked source target and plain-text body and
explicitly approves it. The server, not the browser, snapshots the exact allowlisted
source locator and binds it to the approved body with SHA-256 digests. The authenticated
agent must recompute both digests before calling the canonical Outlook or Teams reply
tool. It must not change recipients, destination, source message, or body.

A receipt is accepted only when its reply ID, provider, body digest, and target digest
match the stored command. Receipt schemas reject raw source bodies, HTML, attachments,
recipient lists, tokens, and arbitrary connector responses. `unknown` is terminal and
never causes automatic retry or reconciliation. Idempotency prevents duplicate local
records; the prototype does not claim externally provable exactly-once delivery.

Work Stack stores approved user-authored reply text because it is part of the user's
planning/action record. It stores only opaque target locators and minimal receipts from
Microsoft. It does not store Microsoft raw content, OAuth material, a recipient list, or
connector session state. The UI must describe the Copy/Import handoff and must not claim
background sync, provider health, or a direct server connection.

## Persistence and concurrency

The HTTP server holds an OS-level exclusive lease on the data directory for its full
lifetime and is the only writer while running. Direct-write CLI commands for the same
data directory fail closed during that time; `capture ingest` instead forwards to the
server. Offline writers acquire the same lease.

Writes use same-directory temporary files and atomic replacement. Multi-file mutations
first persist a validated recovery journal; startup replays a complete journal and
rejects malformed JSON or malformed journal records without silently replacing them.

Keep the data directory outside any static/document root and include it in the user's
normal encrypted backup policy. The runtime token and server metadata are ephemeral and
must not be backed up or committed.

## Release gate

Run from the repository root:

```powershell
python scripts/audit_export.py .
python -m unittest discover -s tests -v
npm --prefix frontend test
npm --prefix frontend run build
```

The default source policy checks the explicit product-source allowlist and excludes
reproducible dependencies, build output, caches, and VCS metadata. Before distributing
runtime data or another prepared export, scan the complete tree instead. Additional
environment-specific prohibited terms can be supplied without storing them:

```powershell
python scripts/audit_export.py <runtime-or-export-directory> --mode tree `
  --deny TERM_A --deny TERM_B
```

Complete [docs/RELEASE-CHECKLIST.md](docs/RELEASE-CHECKLIST.md) and choose exactly one
release label before distribution.
