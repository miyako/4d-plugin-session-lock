import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import host


class HostGateTests(unittest.TestCase):
    def invoke(self, label, code, stdout, stderr=b""):
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary)
            process = subprocess.CompletedProcess([], code, stdout, stderr)
            with patch("subprocess.run", return_value=process):
                result = host.invoke(Path("tool4d"), Path("project.4DProject"),
                                     "method", label, logs)
            self.assertEqual((logs / f"{label}.stdout.log").read_bytes(), stdout)
            self.assertEqual((logs / f"{label}.stderr.log").read_bytes(), stderr)
            json.dumps(result)
            return result

    def test_exact_marker_and_exit_zero_required(self):
        self.assertTrue(self.invoke("plugin", 0, b"PASS\n")["passed"])
        self.assertFalse(self.invoke("plugin", 0, b"NOT PASS\n")["passed"])
        self.assertFalse(self.invoke("plugin", 0, b"")["passed"])

    def test_post_pass_crash_is_not_success(self):
        result = self.invoke("plugin", -11, b"PASS\n", b"shutdown crash")
        self.assertTrue(result["assertionsPassed"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["exit"], -11)

    def test_baseline_has_its_own_exact_marker(self):
        self.assertTrue(self.invoke("baseline", 0, b"BASELINE PASS\n")["passed"])
        self.assertFalse(self.invoke("baseline", 0, b"PASS\n")["passed"])
        self.assertFalse(self.invoke("baseline", -11, b"BASELINE PASS\n")["passed"])

    def test_timeout_preserves_partial_logs_and_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary)
            error = subprocess.TimeoutExpired([], 180, b"PASS\n", b"partial error")
            with patch("subprocess.run", side_effect=error):
                result = host.invoke(Path("tool4d"), Path("project.4DProject"),
                                     "method", "plugin", logs)
            self.assertTrue(result["timedOut"])
            self.assertFalse(result["passed"])
            self.assertIsNone(result["exit"])
            self.assertEqual((logs / "plugin.stderr.log").read_bytes(), b"partial error")


if __name__ == "__main__":
    unittest.main()
