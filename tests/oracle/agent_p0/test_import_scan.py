import unittest

import fixture_support


class ImportScanTest(unittest.TestCase):
    def setUp(self):
        self.runner = fixture_support.runner_module()

    def test_clean_module_passes(self):
        source = "import json\nfrom pathlib import Path\n\nVALUE = 1\n"
        violations = self.runner.scan_python_source("workstack/agent_x.py", source, ["subprocess", "workstack.store"], ["os.system", "subprocess.*"])
        self.assertEqual(violations, [])

    def test_plain_import_is_rejected(self):
        source = "import subprocess\n"
        violations = self.runner.scan_python_source("workstack/agent_x.py", source, ["subprocess"], [])
        self.assertEqual(violations, ["workstack/agent_x.py: forbidden import subprocess"])

    def test_from_import_with_prefix_is_rejected(self):
        source = "from workstack.storage.lease import StorageWriterLease\n"
        violations = self.runner.scan_python_source("workstack/agent_x.py", source, ["workstack.storage"], [])
        self.assertEqual(violations, ["workstack/agent_x.py: forbidden import workstack.storage.lease"])

    def test_prefix_must_not_match_similar_names(self):
        source = "import workstack.storekeeper\n"
        violations = self.runner.scan_python_source("workstack/agent_x.py", source, ["workstack.store"], [])
        self.assertEqual(violations, [])

    def test_forbidden_call_exact_match(self):
        source = "import os\n\nos.system('x')\n"
        violations = self.runner.scan_python_source("workstack/agent_x.py", source, [], ["os.system"])
        self.assertEqual(violations, ["workstack/agent_x.py: forbidden call os.system"])

    def test_forbidden_call_wildcard(self):
        source = "import subprocess\n\nsubprocess.run(['x'])\nsubprocess.Popen(['y'])\n"
        violations = self.runner.scan_python_source("workstack/agent_x.py", source, [], ["subprocess.*"])
        self.assertEqual(
            violations,
            [
                "workstack/agent_x.py: forbidden call subprocess.run",
                "workstack/agent_x.py: forbidden call subprocess.Popen",
            ],
        )

    def test_nested_attribute_call_is_detected(self):
        source = "import http.client\n\nconnection = http.client.HTTPConnection('localhost')\n"
        violations = self.runner.scan_python_source("workstack/agent_x.py", source, ["http.client"], [])
        self.assertEqual(violations, ["workstack/agent_x.py: forbidden import http.client"])

    def test_syntax_error_is_reported(self):
        violations = self.runner.scan_python_source("workstack/agent_x.py", "def broken(:\n", [], [])
        self.assertTrue(any("syntax error" in item for item in violations))
