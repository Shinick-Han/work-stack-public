"""Bad mutant M6: leaks an injected secret canary onto stdout.

Subject seam: probe_output_canary.emit
Defect: the renderer copies the injected token into the rendered envelope and emits a
second diagnostic line. Expected rejection: P0-SECRET-CANARY-STDOUT and
P0-STDOUT-SINGLE-JSON.
"""

import json


def emit(*, payload, secrets, out, err):
    rendered = dict(payload)
    rendered["meta"] = dict(payload["meta"])
    rendered["meta"]["token"] = secrets["token"]
    out.write(json.dumps(rendered, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    out.write("diagnostic tail\n")
    err.write("checkpoint rendered\n")
    return 0
