"""Bad mutant M3: generates a fresh idempotency key when replaying a lost response.

Subject seam: probe_idempotent_replay.replay_checkpoint
Defect: the bounded retry sends a mutated intent key, so the second attempt no longer
reuses the caller's key. Expected rejection: P0-FRESH-KEY.
"""


def replay_checkpoint(*, send, body, intent_key, store, response_lost):
    try:
        send(body, intent_key)
    except response_lost:
        send(body, intent_key + "-retry")
    return "commit_state_recorded"
