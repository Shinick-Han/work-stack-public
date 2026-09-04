"""Good fixture subject for the transport lane probes (P2, P3, P4).

Implements all three frozen probe seams correctly:

- replay_checkpoint: one bounded identical replay on response loss;
- dispatch_after_online_failure: never opens the local mutation path after an online failure;
- emit: renders exactly one JSON object plus LF and never leaks injected secrets.
"""

import json


def replay_checkpoint(*, send, body, intent_key, store, response_lost):
    try:
        send(body, intent_key)
    except response_lost:
        send(body, intent_key)
    return "commit_state_recorded"


def dispatch_after_online_failure(*, send, transport_unavailable, local_factory):
    try:
        send()
        return "committed"
    except transport_unavailable:
        try:
            send()
            return "committed"
        except transport_unavailable:
            return "commit_unknown"


def emit(*, payload, secrets, out, err):
    out.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return 0
