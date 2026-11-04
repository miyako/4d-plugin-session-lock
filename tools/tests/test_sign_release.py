import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import sign_release


class SigningGateTests(unittest.TestCase):
    def test_missing_credentials_fail_with_names_only(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as error:
                sign_release.sign(Path("unused"), Path("unused.zip"))
        for name in sign_release.REQUIRED_SECRETS:
            self.assertIn(name, str(error.exception))

    def test_command_error_does_not_render_secret_arguments_or_output(self):
        result = subprocess.CompletedProcess([], 1, b"", b"private-key-password")
        with patch("subprocess.run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, r"Import failed \(exit 1\)") as error:
                sign_release.command("Import", ["security", "private-key-password"])
        self.assertNotIn("private-key-password", str(error.exception))

    def test_command_timeout_does_not_render_secret_arguments(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                ["secret-password"], 1)):
            with self.assertRaisesRegex(RuntimeError, "Notarize timed out") as error:
                sign_release.command("Notarize", ["secret-password"])
        self.assertNotIn("secret-password", str(error.exception))

    def notarization_case(self, status):
        environment = {name: "test-value" for name in sign_release.REQUIRED_SECRETS}
        environment["APPLE_DEVELOPER_ID_CERTIFICATE"] = "dGVzdA=="
        environment["NOTARYTOOL_TEAM_ID"] = "TESTTEAM01"
        labels = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "release.zip"

            def fake_command(label, arguments, timeout=300):
                labels.append(label)
                stdout, stderr = b"", b""
                if label == "Discover valid signing identity":
                    stdout = b'1) AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "Developer ID Application: Example"\n'
                elif label == "Inspect Developer ID signature":
                    stderr = (
                        b"TeamIdentifier=TESTTEAM01\n"
                        b"Authority=Developer ID Application: Example\n"
                        b"flags=0x10000(runtime)\nTimestamp=Nov 4, 2026\n")
                elif label == "Build and extract-verify signed archive":
                    output.write_bytes(b"test archive bytes")
                elif label == "Notarize exact release ZIP":
                    stdout = ('{"id":"test-submission","status":"' + status + '"}').encode()
                return subprocess.CompletedProcess(arguments, 0, stdout, stderr)

            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(sign_release.sys, "platform", "darwin"), \
                    patch.object(sign_release, "command", side_effect=fake_command):
                if status == "Accepted":
                    sign_release.sign(root, output)
                    evidence = output.with_suffix(".zip.notarization.json").read_text()
                    self.assertIn('"status": "Accepted"', evidence)
                    self.assertIn("Not Authenticode-signed", evidence)
                else:
                    with self.assertRaisesRegex(RuntimeError, "explicitly return Accepted"):
                        sign_release.sign(root, output)
                    self.assertFalse(output.with_suffix(".zip.notarization.json").exists())
        self.assertIn("Delete ephemeral signing keychain", labels)

    def test_apple_accepted_is_required(self):
        self.notarization_case("Invalid")

    def test_explicit_apple_accepted_records_exact_archive(self):
        self.notarization_case("Accepted")


if __name__ == "__main__":
    unittest.main()
