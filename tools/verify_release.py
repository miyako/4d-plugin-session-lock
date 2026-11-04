#!/usr/bin/env python3
"""Verify exact signed candidate/draft/published assets without publishing."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import zipfile


def verify(directory, version, source, expected_sha):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Invalid version")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Expected immutable source commit")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("Expected archive SHA-256")
    archive = directory / f"SessionLock-{version}.zip"
    checksum = archive.with_suffix(".zip.sha256")
    report = json.loads(archive.with_suffix(".zip.notarization.json").read_text())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != expected_sha:
        raise ValueError("Downloaded archive differs from approved bytes")
    checksum_fields = checksum.read_text().split()
    if checksum_fields != [digest, archive.name]:
        raise ValueError("Checksum sidecar does not identify exact archive bytes")
    if (report.get("status") != "Accepted" or not report.get("submissionId")
            or report.get("sha256") != digest or report.get("archive") != archive.name):
        raise ValueError("Missing Accepted evidence for exact downloaded archive")
    with tempfile.TemporaryDirectory(prefix="sessionlock-release-check-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(archive) as package:
            names = set()
            for entry in package.infolist():
                raw_name = entry.orig_filename
                path = PurePosixPath(raw_name)
                mode = entry.external_attr >> 16
                if (raw_name in names or "\\" in raw_name
                        or path.is_absolute() or ".." in path.parts
                        or ":" in raw_name or stat.S_ISLNK(mode)
                        or path.as_posix() != raw_name.rstrip("/")):
                    raise ValueError("Unsafe release ZIP member")
                names.add(raw_name)
            package.extractall(root)
            if sys.platform != "win32":
                for entry in package.infolist():
                    mode = entry.external_attr >> 16
                    if mode and not entry.is_dir():
                        root.joinpath(*PurePosixPath(entry.filename).parts).chmod(
                            stat.S_IMODE(mode))
        provenance = json.loads((root / "provenance.json").read_text())
        if (provenance.get("source_commit") != source or provenance.get("version") != version
                or provenance.get("signing") != "developer-id-notarized"):
            raise ValueError("Release provenance does not match approved source/version")
        expected_sdk = subprocess.check_output(
            ["git", "rev-parse", f"{source}:4D-Plugin-SDK"], text=True).strip()
        if provenance.get("sdk_commit") != expected_sdk:
            raise ValueError("Release provenance does not match approved SDK gitlink")
        subprocess.run([sys.executable, "tools/package.py", "verify", "--root", str(root)],
                       check=True)
    print(f"Verified exact Accepted release archive: {archive.name} {digest}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        verify(args.directory, args.version, args.source, args.sha256)
    except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Release asset verification failed: {error}\n")


if __name__ == "__main__":
    main()
