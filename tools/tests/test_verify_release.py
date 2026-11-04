import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools import verify_release


class DownloadedReleaseTests(unittest.TestCase):
    def create(self, directory, source="a" * 40, extra=None):
        archive = directory / "SessionLock-1.0.0.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("provenance.json", json.dumps({
                "source_commit": source, "sdk_commit": "b" * 40,
                "version": "1.0.0", "signing": "developer-id-notarized"}))
            if extra:
                member = zipfile.ZipInfo("placeholder")
                member.filename = extra
                package.writestr(member, "unsafe")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
        archive.with_suffix(".zip.notarization.json").write_text(json.dumps({
            "status": "Accepted", "submissionId": "test-submission",
            "sha256": digest, "archive": archive.name}))
        return digest

    def test_exact_approved_download_calls_package_verifier(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            digest = self.create(directory)
            with patch("subprocess.run") as run, \
                    patch("subprocess.check_output", return_value="b" * 40):
                verify_release.verify(directory, "1.0.0", "a" * 40, digest)
                self.assertEqual(run.call_args.args[0][1:3], ["tools/package.py", "verify"])

    def test_altered_archive_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            digest = self.create(directory)
            with (directory / "SessionLock-1.0.0.zip").open("ab") as archive:
                archive.write(b"altered")
            with self.assertRaisesRegex(ValueError, "approved bytes"):
                verify_release.verify(directory, "1.0.0", "a" * 40, digest)

    def test_wrong_source_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            digest = self.create(directory, source="b" * 40)
            with self.assertRaisesRegex(ValueError, "approved source"):
                verify_release.verify(directory, "1.0.0", "a" * 40, digest)

    def test_traversal_and_windows_paths_rejected(self):
        for unsafe in ("../escape", "/absolute", "C:/escape", "folder\\escape"):
            with self.subTest(path=unsafe), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                digest = self.create(directory, extra=unsafe)
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    verify_release.verify(directory, "1.0.0", "a" * 40, digest)

    def test_rejected_notarization_cannot_be_substituted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            digest = self.create(directory)
            (directory / "SessionLock-1.0.0.zip.notarization.json").write_text(json.dumps({
                "status": "Invalid", "sha256": digest, "archive": "SessionLock-1.0.0.zip"}))
            with self.assertRaisesRegex(ValueError, "Accepted"):
                verify_release.verify(directory, "1.0.0", "a" * 40, digest)


if __name__ == "__main__":
    unittest.main()
