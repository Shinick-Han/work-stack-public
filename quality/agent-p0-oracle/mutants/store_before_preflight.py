"""Bad mutant M1: constructs Store before authority preflight completes.

Subject seam: probe_authority_preflight.admit
Defect: store_factory() is invoked unconditionally before the format/UID probes, so every
refusal path constructs Store. Expected rejection: P0-STORE-BEFORE-PREFLIGHT.
"""


def admit(*, data_dir, expected_workspace_uid, format_probe, uid_probe, store_factory):
    store_factory()
    if format_probe(data_dir) != "v3":
        return "refused_format"
    if uid_probe(data_dir) != expected_workspace_uid:
        return "refused_uid"
    return "admitted"
