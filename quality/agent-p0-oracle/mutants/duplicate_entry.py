"""Bad mutant M4: duplicates the Worklog entry across a bounded retry.

Subject seam: probe_idempotent_replay.replay_checkpoint
Defect: after the replay answer arrives, the subject records the checkpoint locally a
second time instead of trusting the server-side idempotent replay. Expected rejection:
P0-DUPLICATE-WORKLOG.
"""


def replay_checkpoint(*, send, body, intent_key, store, response_lost):
    try:
        send(body, intent_key)
    except response_lost:
        send(body, intent_key)
    store.record({"intent_key": intent_key, "body": body.decode("utf-8")})
    return "commit_state_recorded"
