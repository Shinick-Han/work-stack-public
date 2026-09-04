"""Bad mutant M2: mutates the authority tree while refusing.

Subject seam: probe_authority_preflight.admit
Defect: a refusal writes a recovery marker into the authority tree, so the before/after
tree digest changes. Expected rejection: P0-PREFLIGHT-TREE-MUTATION.
"""

from pathlib import Path


def admit(*, data_dir, expected_workspace_uid, format_probe, uid_probe, store_factory):
    if format_probe(data_dir) != "v3":
        marker = Path(data_dir) / "recovered.lock"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("recovery marker", encoding="utf-8")
        return "refused_format"
    if uid_probe(data_dir) != expected_workspace_uid:
        return "refused_uid"
    store_factory()
    return "admitted"
