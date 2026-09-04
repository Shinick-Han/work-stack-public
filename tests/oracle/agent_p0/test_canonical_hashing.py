import unittest

import fixture_support


class CanonicalHashingTest(unittest.TestCase):
    def setUp(self):
        self.runner = fixture_support.runner_module()
        self.vectors = fixture_support.read_jsonl(
            fixture_support.ORACLE_DIR / "golden" / "canonical-json.v1.jsonl"
        )

    def test_golden_vectors_reproduce_canonical_bytes(self):
        for vector in self.vectors:
            expected = vector["canonical"].encode("utf-8")
            self.assertEqual(
                self.runner.canonical_json_bytes(vector["input"]),
                expected,
                "vector %s" % vector["name"],
            )

    def test_digest_is_deterministic_and_order_insensitive(self):
        first = fixture_support.canonical_bytes({"a": 1, "b": [2, 3]})
        second = fixture_support.canonical_bytes({"b": [2, 3], "a": 1})
        self.assertEqual(first, second)
        self.assertEqual(
            fixture_support.sha256_hex(first),
            fixture_support.sha256_hex(second),
        )
        self.assertEqual(len(fixture_support.sha256_hex(first)), 64)

    def test_golden_files_are_one_canonical_object_per_line(self):
        for name in ("canonical-json.v1.jsonl", "diff-raw-parse.v1.jsonl", "sentinel-verdicts.v1.jsonl"):
            path = fixture_support.ORACLE_DIR / "golden" / name
            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"), name)
            for line in raw.decode("utf-8").splitlines():
                record = json_module_loads(line)
                self.assertEqual(
                    fixture_support.canonical_bytes(record),
                    (line + "\n").encode("utf-8"),
                    "golden line in %s is not canonical" % name,
                )


def json_module_loads(text):
    import json

    return json.loads(text)


class DiffRawParseTest(unittest.TestCase):
    def setUp(self):
        self.runner = fixture_support.runner_module()
        self.vectors = fixture_support.read_jsonl(
            fixture_support.ORACLE_DIR / "golden" / "diff-raw-parse.v1.jsonl"
        )

    def test_golden_vectors_parse_to_expected_entries(self):
        for vector in self.vectors:
            raw = vector["raw"].encode("utf-8")
            entries = self.runner.parse_diff_raw(raw)
            self.assertEqual(entries, vector["expected"], "vector %s" % vector["name"])

    def test_rename_paths_map_source_first(self):
        vector = [item for item in self.vectors if item["name"] == "rename"][0]
        entries = self.runner.parse_diff_raw(vector["raw"].encode("utf-8"))
        self.assertEqual(entries[0]["old_path"], "workstack/old_name.py")
        self.assertEqual(entries[0]["path"], "workstack/new_name.py")
        self.assertEqual(entries[0]["status"], "R")
        self.assertEqual(entries[0]["score"], 96)

    def test_diff_digest_ignores_entry_order(self):
        entries = self.runner.parse_diff_raw(
            (":100644 100644 " + "1" * 40 + " " + "2" * 40 + " M\0a.py\0:100644 100644 " + "3" * 40 + " " + "4" * 40 + " M\0b.py\0").encode("utf-8")
        )
        reordered = list(reversed(entries))
        self.assertEqual(self.runner.diff_digest(entries), self.runner.diff_digest(reordered))


if __name__ == "__main__":
    unittest.main()
