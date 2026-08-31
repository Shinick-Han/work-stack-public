# Work Stack M4 Microsoft Gate 0 Preflight

Date: 2026-08-30
Result: EXTERNAL EVIDENCE PENDING; PRODUCT GATES REMAIN CLOSED

## Product coordinate reviewed

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Branch: `codex/workstack-ui-actions-20260830`
- Starting documentation commit: `4362c114edc3fce609e36ced7c4139bdff7274bf`
- Push: not performed

## Checks performed

- Inspected the complete callable capability inventory exposed to the current Codex task.
- Searched the available plugin-management capability for a discover/connect path.
- Inspected the existing OOB contract, provider-gate code, fixtures, release matrix, and
  Gate 0 instructions.
- Confirmed that the current production artifact defaults all four Microsoft read/reply
  build flags to false.

## Finding

The current agent task exposes no Outlook Email or Microsoft Teams search/read/reply tool.
The available plugin-management surface in this task does not expose plugin discovery or a
Microsoft connector connection action. Therefore this task cannot perform the required real
tenant Gate 0 read spike or truthfully collect stable message/thread/version/target evidence.

This does not invalidate the fixture-backed Work Stack implementation. It prevents enabling
any provider lane for a dogfood build without evidence from an agent session where those OOB
connectors are actually callable.

## Enforced decision

- `VITE_WORKSTACK_OUTLOOK_READ_VERIFIED=false`
- `VITE_WORKSTACK_TEAMS_READ_VERIFIED=false`
- `VITE_WORKSTACK_OUTLOOK_REPLY_VERIFIED=false`
- `VITE_WORKSTACK_TEAMS_REPLY_VERIFIED=false`
- No Outlook or Teams message was read.
- No ReplyCommand was sent and no external write was attempted.
- No token, connection secret, source content, recipient, or locator was stored.
- The generic manual sanitized Capture path remains available.

## Exact resume gate

Resume M4 in an agent task that exposes the authenticated Outlook and Teams OOB tools, then
run the existing release matrix in this order: Outlook read, Teams read, one provider reply,
then the other provider reply. Use non-sensitive source records, retain only redacted evidence,
and enable each build flag only for the exact provider lane that passes.

M5 and later local-only maturation may proceed independently. This receipt is not a Gate 0
pass and must never be used to enable a Microsoft capability.
