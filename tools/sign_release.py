#!/usr/bin/env python3
"""Developer ID sign and notarize a verified package; never publish or tag."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

REQUIRED_SECRETS = (
    "APPLE_DEVELOPER_ID_CERTIFICATE",
    "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
    "KEYCHAIN_PASSWORD",
    "NOTARYTOOL_APPLE_ID",
    "NOTARYTOOL_TEAM_ID",
    "NOTARYTOOL_PASSWORD",
)


def command(label, arguments, timeout=300):
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{label} timed out") from None
    if result.returncode:
        if label in ("Sign complete combined bundle", "Verify signed macOS code object"):
            diagnostic = result.stderr.decode("utf-8", errors="replace")
            for name in REQUIRED_SECRETS:
                value = os.environ.get(name)
                if value:
                    diagnostic = diagnostic.replace(value, "***")
            print(diagnostic, file=sys.stderr)
        # Commands contain secrets. Never render argv or unfiltered tool output.
        raise RuntimeError(f"{label} failed (exit {result.returncode})")
    return result


def sign(root, output):
    missing = [name for name in REQUIRED_SECRETS if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing required Actions secrets: " + ", ".join(missing))
    if sys.platform != "darwin":
        raise RuntimeError("Signing requires a macOS runner")
    command("Package pre-sign verification",
            [sys.executable, "tools/package.py", "verify", "--root", str(root)])
    if output.exists():
        raise RuntimeError("Release output already exists")
    bundle = root / "SessionLock.bundle"
    binary = bundle / "Contents/MacOS/SessionLock"
    with tempfile.TemporaryDirectory(prefix="sessionlock-sign-",
                                     dir=os.environ.get("RUNNER_TEMP")) as temporary:
        directory = Path(temporary)
        keychain = directory / "release.keychain-db"
        certificate = directory / "certificate.p12"
        created = False
        search_changed = False
        original_search = []
        try:
            encoded = "".join(os.environ["APPLE_DEVELOPER_ID_CERTIFICATE"].split())
            certificate.write_bytes(base64.b64decode(encoded, validate=True))
            certificate.chmod(0o600)
            password = os.environ["KEYCHAIN_PASSWORD"]
            command("Create ephemeral signing keychain",
                    ["security", "create-keychain", "-p", password, str(keychain)])
            created = True
            command("Configure ephemeral signing keychain",
                    ["security", "set-keychain-settings", "-lut", "3600", str(keychain)])
            command("Unlock signing keychain",
                    ["security", "unlock-keychain", "-p", password, str(keychain)])
            original_search = shlex.split(command(
                "Read user keychain search list",
                ["security", "list-keychains", "-d", "user"]).stdout.decode())
            command("Register ephemeral signing keychain",
                    ["security", "list-keychains", "-d", "user", "-s",
                     str(keychain), *original_search])
            search_changed = True
            command("Import Developer ID certificate",
                    ["security", "import", str(certificate), "-P",
                     os.environ["APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD"],
                     "-A", "-t", "cert", "-f", "pkcs12", "-k", str(keychain)])
            certificate.unlink()
            command("Authorize codesign key access",
                    ["security", "set-key-partition-list", "-S",
                     "apple-tool:,apple:,codesign:", "-s", "-k", password, str(keychain)])
            identities = command("Discover valid signing identity",
                                 ["security", "find-identity", "-v", "-p",
                                  "codesigning", str(keychain)]).stdout.decode()
            matches = re.findall(r'([A-Fa-f0-9]{40}) "Developer ID Application:[^"]+"',
                                 identities)
            if len(matches) != 1:
                raise RuntimeError("Expected exactly one valid Developer ID Application identity")
            command("Sign complete combined bundle",
                    ["codesign", "--verbose", "--deep", "--timestamp", "--force",
                     "--options=runtime", "--keychain", str(keychain),
                     "--sign", matches[0], str(bundle)])
            command("Verify signed macOS code object",
                    ["codesign", "--verify", "--strict", "--all-architectures", str(binary)])
            details = command("Inspect Developer ID signature",
                              ["codesign", "--display", "--verbose=4", str(binary)])
            signature = details.stderr.decode() + details.stdout.decode()
            team = re.search(r"^TeamIdentifier=(.+)$", signature, re.MULTILINE)
            if not team or team.group(1).strip() != os.environ["NOTARYTOOL_TEAM_ID"]:
                raise RuntimeError("Developer ID signature does not match expected team")
            if ("Authority=Developer ID Application:" not in signature
                    or "runtime" not in signature
                    or not re.search(r"^Timestamp=.+$", signature, re.MULTILINE)):
                raise RuntimeError("Missing Developer ID authority, hardened runtime or secure timestamp")
            command("Record final signed bytes",
                    [sys.executable, "tools/package.py", "refresh", "--root", str(root),
                     "--signing", "developer-id-notarized"])
            command("Build and extract-verify signed archive",
                    [sys.executable, "tools/package.py", "archive", "--root", str(root),
                     "--output", str(output)])
            profile = "SessionLockRelease"
            command("Store temporary notarization credentials",
                    ["xcrun", "notarytool", "store-credentials", profile,
                     "--apple-id", os.environ["NOTARYTOOL_APPLE_ID"],
                     "--team-id", os.environ["NOTARYTOOL_TEAM_ID"],
                     "--password", os.environ["NOTARYTOOL_PASSWORD"],
                     "--keychain", str(keychain)])
            response = command("Notarize exact release ZIP",
                               ["xcrun", "notarytool", "submit", str(output),
                                "--keychain-profile", profile, "--keychain", str(keychain),
                                "--wait", "--output-format", "json"], timeout=1800)
            try:
                report = json.loads(response.stdout)
            except (ValueError, UnicodeError):
                raise RuntimeError("Notarytool did not return valid JSON") from None
            if report.get("status") != "Accepted" or not report.get("id"):
                raise RuntimeError("Apple notarization did not explicitly return Accepted")
            evidence = {
                "status": report["status"], "submissionId": report["id"],
                "archive": output.name,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "macOS": "Developer ID signed, secure timestamp, hardened runtime, notarization Accepted",
                "windows": "Not Authenticode-signed",
                "stapled": False,
            }
            output.with_suffix(output.suffix + ".notarization.json").write_text(
                json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        finally:
            if certificate.exists():
                certificate.unlink()
            try:
                if search_changed:
                    command("Restore original user keychain search list",
                            ["security", "list-keychains", "-d", "user", "-s",
                             *original_search])
            finally:
                if created:
                    command("Delete ephemeral signing keychain",
                            ["security", "delete-keychain", str(keychain)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        sign(args.root.resolve(), args.output.resolve())
    except (RuntimeError, ValueError, OSError) as error:
        # Decode/OS failures may include input snippets. Expose only safe messages.
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        parser.exit(1, f"Release signing blocked: {message}\n")


if __name__ == "__main__":
    main()
