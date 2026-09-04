"""Bad mutant M5: falls back to exclusive-local mutation after an online failure.

Subject seam: probe_no_local_fallback.dispatch_after_online_failure
Defect: when the online owner is unavailable the subject opens the local mutation path
instead of stopping. Expected rejection: P0-NO-FALLBACK.
"""


def dispatch_after_online_failure(*, send, transport_unavailable, local_factory):
    try:
        send()
        return "committed"
    except transport_unavailable:
        local_factory().checkpoint({"intent": "agent:selftest:checkpoint-1"})
        return "committed"
