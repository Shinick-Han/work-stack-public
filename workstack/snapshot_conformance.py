"""Read-only runner for the frozen Work Stack/Conduit snapshot conformance kit."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .snapshot import (
    SnapshotValidationError,
    canonical_snapshot_bytes,
    snapshot_digest,
    validate_snapshot_bytes,
    validate_snapshot_object,
    validate_text,
)
from .snapshot_safety import evaluate_safety
from .unicode17 import UNICODE_DATA_VERSION


CONTRACT_SHA256 = "cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70"
SAFETY_ROOT = "sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148"
KIT_ROOT = "sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _verify_bundle(root: Path, expected_root: str) -> None:
    manifest_path = root / "MANIFEST.sha256"
    manifest = manifest_path.read_bytes()
    if "sha256:" + _sha256(manifest) != expected_root:
        raise AssertionError("frozen bundle root mismatch")
    payloads: set[Path] = set()
    for line in manifest.decode("utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        payloads.add(Path(relative))
        path = root / Path(relative)
        if not path.is_file() or _sha256(path.read_bytes()) != expected:
            raise AssertionError("frozen bundle payload mismatch: {}".format(relative))
    if (root / "BUNDLE_ROOT.txt").read_text(encoding="utf-8").strip() != expected_root:
        raise AssertionError("frozen bundle root declaration mismatch")
    expected_roster = payloads | {Path("MANIFEST.sha256"), Path("BUNDLE_ROOT.txt")}
    actual_roster = {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file()
    }
    if actual_roster != expected_roster:
        raise AssertionError("frozen bundle roster mismatch")


def _construct(specification: dict[str, Any]) -> str:
    if "literal" in specification:
        return specification["literal"]
    if "utf16_code_units" in specification:
        raw = b"".join(
            int(unit).to_bytes(2, "little")
            for unit in specification["utf16_code_units"]
        )
        return raw.decode("utf-16-le", errors="surrogatepass")
    repeat = specification["repeat"]
    return chr(repeat["scalar"]) * repeat["count"]


def _mutate_bytes(base: bytes, operations: list[dict[str, Any]]) -> bytes:
    value = base
    for operation in operations:
        kind = operation["op"]
        if kind == "prepend_hex":
            value = bytes.fromhex(operation["hex"]) + value
        elif kind == "append_hex":
            value += bytes.fromhex(operation["hex"])
        elif kind == "remove_suffix_hex":
            suffix = bytes.fromhex(operation["hex"])
            if not value.endswith(suffix):
                raise AssertionError("invalid frozen remove-suffix recipe")
            value = value[:-len(suffix)]
        elif kind == "replace_suffix_hex":
            suffix = bytes.fromhex(operation["find_hex"])
            if not value.endswith(suffix):
                raise AssertionError("invalid frozen replace-suffix recipe")
            value = value[:-len(suffix)] + bytes.fromhex(operation["replacement_hex"])
        elif kind in {"replace_utf8_once", "replace_utf8_once_with_hex"}:
            needle = operation["needle_utf8"].encode("utf-8")
            if value.count(needle) != 1:
                raise AssertionError("invalid frozen replacement recipe")
            replacement = (
                operation["replacement_utf8"].encode("utf-8")
                if kind == "replace_utf8_once"
                else bytes.fromhex(operation["replacement_hex"])
            )
            value = value.replace(needle, replacement, 1)
        elif kind == "insert_after_prefix_utf8":
            prefix = operation["prefix_utf8"].encode("utf-8")
            if not value.startswith(prefix):
                raise AssertionError("invalid frozen prefix recipe")
            insertion = bytes.fromhex(operation["byte_hex"]) * operation["count"]
            value = prefix + insertion + value[len(prefix):]
        else:
            raise AssertionError("unknown frozen byte recipe")
    return value


def _mutate_object(base: bytes, operations: list[dict[str, Any]]) -> dict[str, Any]:
    value = json.loads(base)
    for operation in operations:
        kind = operation["op"]
        field = operation["field"]
        if kind in {"set", "add"}:
            value[field] = copy.deepcopy(operation["value"])
        elif kind == "delete":
            del value[field]
        elif kind == "set_constructed":
            value[field] = _construct(operation["construction"])
        else:
            raise AssertionError("unknown frozen object recipe")
    return value


def _assert_refusal(error: SnapshotValidationError, expected: dict[str, Any]) -> None:
    actual = error.as_dict()
    for key in ("stage", "reason", "field", "public_code"):
        expected_key = "code" if key == "public_code" else key
        if expected_key in expected:
            actual_value = actual.get("code") if key == "public_code" else actual.get(key)
            if actual_value != expected[expected_key]:
                raise AssertionError(
                    "conformance classification mismatch: {} != {}".format(
                        actual_value, expected[expected_key]
                    )
                )


def _run_invalid_case(
    case: dict[str, Any], bases: dict[str, bytes]
) -> None:
    base = bases[case["base"]]
    construction = case["construction"]
    expected = case["expected"]
    try:
        kind = construction["type"]
        if kind == "byte_mutation":
            validate_snapshot_bytes(_mutate_bytes(base, construction["operations"]))
        elif kind == "reserialize_with_key_order":
            source = json.loads(base)
            ordered = {key: source[key] for key in construction["key_order"]}
            raw = json.dumps(
                ordered, ensure_ascii=False, separators=(",", ":"), sort_keys=False
            ).encode("utf-8") + b"\n"
            validate_snapshot_bytes(raw)
        elif kind == "object_mutation":
            validate_snapshot_object(_mutate_object(base, construction["operations"]))
        elif kind == "digest_override":
            validate_snapshot_bytes(base, construction["supplied_digest"])
        else:
            raise AssertionError("unknown frozen invalid recipe type")
    except SnapshotValidationError as error:
        _assert_refusal(error, expected)
        return
    raise AssertionError("invalid fixture was accepted: {}".format(case["id"]))


def _run_safety_case(case: dict[str, Any]) -> None:
    fragments = case["input"]["fragments"]
    if not fragments or any(not fragment for fragment in fragments):
        raise AssertionError("safety fragments must be nonempty")
    value = "".join(fragments)
    result = evaluate_safety(value, case["field"])
    if result != case["expected"]:
        raise AssertionError("safety case mismatch: {}".format(case["id"]))
    diagnostic = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if value in diagnostic or any(fragment in diagnostic for fragment in fragments):
        raise AssertionError("safety diagnostic echoed candidate content")


def _run_boundary_case(case: dict[str, Any]) -> None:
    value = _construct(case["construction"])
    expected = case["expected"]
    try:
        validate_text(value, case["field"])
    except SnapshotValidationError as error:
        if expected["decision"] != "REFUSE":
            raise AssertionError("allowed boundary fixture was refused") from error
        _assert_refusal(error, expected)
        return
    if expected["decision"] != "ALLOW":
        raise AssertionError("invalid boundary fixture was accepted")


def run_conformance_kit(root: Path | str) -> dict[str, Any]:
    """Execute the complete frozen kit without writing generated cases to disk."""

    kit = Path(root)
    _verify_bundle(kit, KIT_ROOT)
    _verify_bundle(kit / "safety", SAFETY_ROOT)
    contract_path = kit / "contract" / "CONDUIT_WORKSTACK_DOCKING_CONTRACT_V1_REVISION_4.md"
    if _sha256(contract_path.read_bytes()) != CONTRACT_SHA256:
        raise AssertionError("frozen contract mismatch")

    digest_index = json.loads(
        (kit / "fixtures" / "valid" / "expected-digests.json").read_text(
            encoding="utf-8"
        )
    )
    bases: dict[str, bytes] = {}
    for fixture in digest_index["fixtures"]:
        raw = (kit / "fixtures" / "valid" / fixture["path"]).read_bytes()
        if len(raw) != fixture["bytes"] or snapshot_digest(raw) != fixture["sha256"]:
            raise AssertionError("valid fixture digest mismatch")
        validated = validate_snapshot_bytes(raw, fixture["sha256"])
        if canonical_snapshot_bytes(validated) != raw:
            raise AssertionError("valid fixture canonical mismatch")
        bases[fixture["id"].removeprefix("valid-")] = raw

    invalid = json.loads(
        (kit / "fixtures" / "invalid" / "invalid-cases.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
    for case in invalid:
        _run_invalid_case(case, bases)

    safety = json.loads(
        (kit / "safety" / "snapshot-v1-safety-cases.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
    for case in safety:
        _run_safety_case(case)

    boundaries = json.loads(
        (kit / "safety" / "snapshot-v1-text-boundary-cases.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
    for case in boundaries:
        _run_boundary_case(case)

    return {
        "contract_sha256": CONTRACT_SHA256,
        "safety_root": SAFETY_ROOT,
        "kit_root": KIT_ROOT,
        "valid": len(digest_index["fixtures"]),
        "invalid": len(invalid),
        "safety": len(safety),
        "text_boundaries": len(boundaries),
        "unicode_data_version": UNICODE_DATA_VERSION,
    }
