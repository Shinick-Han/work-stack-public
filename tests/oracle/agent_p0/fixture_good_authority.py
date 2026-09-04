"""Good fixture subject for probe P1 (authority-preflight).

Compliant authority admission: refusal paths never construct Store and never touch the
filesystem; Store is constructed exactly once and only after format and UID probes pass.
"""


def admit(*, data_dir, expected_workspace_uid, format_probe, uid_probe, store_factory):
    if format_probe(data_dir) != "v3":
        return "refused_format"
    if uid_probe(data_dir) != expected_workspace_uid:
        return "refused_uid"
    store_factory()
    return "admitted"
