"""Good fixture subject for probe P2 (idempotent-replay).

Compliant bounded replay: the lost-response retry resends identical body bytes with the
caller's intent key and never records a duplicate entry locally.
"""


def replay_checkpoint(*, send, body, intent_key, store, response_lost):
    try:
        send(body, intent_key)
    except response_lost:
        send(body, intent_key)
    return "commit_state_recorded"
