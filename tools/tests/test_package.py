"""Hermetic packaging tests; all Git repositories and binaries are local fixtures."""

import copy
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import plistlib
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "package.py"
SPEC = importlib.util.spec_from_file_location("sessionlock_package", SCRIPT)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


def macho():
    raw = bytearray(128)
    struct.pack_into(">II", raw, 0, 0xCAFEBABE, 2)
    for index, (cpu, subtype) in enumerate(((0x01000007, 3), (0x0100000C, 0))):
        offset = 64 + index * 32
        struct.pack_into(">IIIII", raw, 8 + index * 20, cpu, subtype, offset, 32, 4)
        struct.pack_into("<IIIIIIII", raw, offset, 0xFEEDFACF, cpu, subtype, 8, 0, 0, 0, 0)
    return bytes(raw)


def pe():
    raw = bytearray(256)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 60, 64)
    raw[64:68] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", raw, 68, 0x8664, 1, 0, 0, 0, 112, 0x2022)
    struct.pack_into("<H", raw, 88, 0x20B)
    struct.pack_into("<II", raw, 200 + 16, 16, 240)
    return bytes(raw)


class PackagingTest(unittest.TestCase):
    def run_git(self, repo, *args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], stderr=subprocess.PIPE, env=self.git_env)

    def init_git(self, repo):
        repo.mkdir()
        self.run_git(repo, "init", "--quiet")
        self.run_git(repo, "config", "user.name", "Packaging Test")
        self.run_git(repo, "config", "user.email", "package@example.invalid")
        self.run_git(repo, "config", "core.autocrlf", "false")

    def commit(self, repo):
        self.run_git(repo, "add", ".")
        self.run_git(repo, "commit", "--quiet", "-m", "fixture")

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix=".package-test-", dir=Path.cwd())
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.repo = self.base / "source"
        # Ignore machine-global Git configuration, signing hooks and filters.
        self.git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
                            GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            self.git_env.pop(key, None)
        sdk = self.base / "upstream-sdk"
        self.init_git(sdk)
        (sdk / "LICENSE.md").write_bytes(b"Canonical SDK license\nSecond line\n")
        self.commit(sdk)
        self.init_git(self.repo)
        self.run_git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add",
                     "--quiet", str(sdk), package.SDK)
        (self.repo / "SessionLock").mkdir()
        (self.repo / ".gitignore").write_text(
            "SessionLock/build/\nSessionLock/SessionLock-test/Plugins/\n", encoding="utf-8")
        (self.repo / ".gitattributes").write_text(
            "VERSION text eol=lf\nLICENSE text eol=lf\n"
            "SessionLock/manifest.json text eol=lf\n", encoding="utf-8")
        (self.repo / "VERSION").write_bytes(b"1.0.0\n")
        (self.repo / "LICENSE").write_bytes(b"MIT License\nFixture copyright\n")
        self.manifest = {
            "name": "SessionLock", "id": 31047,
            "commands": [{"theme": "SessionLock", "syntax": "SessionLock " + name + "():J",
                          "threadSafe": True} for name in package.COMMANDS],
        }
        package.write_json(self.repo / "SessionLock/manifest.json", self.manifest)
        (self.repo / "control.txt").write_bytes(b"unconstrained\ncontrol\n")
        self.commit(self.repo)
        self.create_builds()
        self.mac = self.base / "mac"
        self.windows = self.base / "windows"
        self.combined = self.base / "combined"

    def create_builds(self):
        for target in package.TARGETS:
            root = self.repo / "SessionLock/SessionLock-test/Plugins" / package.payload_name(target)
            resources = root / "Contents/Resources"
            resources.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.repo / "SessionLock/manifest.json", resources / "manifest.json")
            shutil.copyfile(self.repo / "LICENSE", resources / "LICENSE")
            shutil.copyfile(self.repo / package.SDK / "LICENSE.md", resources / "LICENSE.md")
            if target == package.TARGETS[0]:
                binary = root / "Contents/MacOS/SessionLock"
                binary.parent.mkdir()
                binary.write_bytes(macho())
                (root / "Contents/Info.plist").write_bytes(plistlib.dumps({
                    "CFBundlePackageType": "BNDL", "CFBundleExecutable": "SessionLock",
                    "CFBundleIdentifier": "com.miyako.SessionLock",
                    "CFBundleVersion": "1.0.0", "CFBundleShortVersionString": "1.0.0",
                }))
            else:
                binary = root / "Contents/Windows64/SessionLock.4DX"
                binary.parent.mkdir()
                binary.write_bytes(pe())
        build = self.repo / "SessionLock/build"
        build.mkdir()
        for name in ("core_tests", "backend_smoke"):
            (build / name).write_bytes(b"native macOS test fixture")
            release = build / "Release"
            release.mkdir(exist_ok=True)
            (release / (name + ".exe")).write_bytes(b"native Windows test fixture")

    def stage_both(self):
        package.stage(self.repo, package.TARGETS[0], self.mac, "AppleClang 17.0.0")
        package.stage(self.repo, package.TARGETS[1], self.windows, "MSVC 19.44")

    def combine(self):
        self.stage_both()
        package.combine(self.mac, self.windows, self.combined)

    def simulate_signing(self):
        binary = self.combined / package.MAC_BINARY
        binary.write_bytes(binary.read_bytes() + b"synthetic signed-byte fixture")
        resources = self.combined / package.CODE_RESOURCES
        resources.parent.mkdir()
        resources.write_bytes(plistlib.dumps({"files2": {}, "rules2": {}}))

    def rewrite_artifact(self, root, mutate):
        value = package.read_json(root / "artifact.json")
        mutate(value)
        package.write_json(root / "artifact.json", value)

    def test_stage_combine_archive_and_cli_verify(self):
        self.combine()
        source_before = package.scan(self.repo / "SessionLock/SessionLock-test/Plugins")
        provenance = package.verify(self.combined)
        self.assertEqual(provenance["source_commit"],
                         self.run_git(self.repo, "rev-parse", "HEAD").decode().strip())
        self.assertEqual(provenance["signing"], "unsigned-pre-signing")
        self.assertEqual(set(provenance["artifacts"]), set(package.TARGETS))
        archive = self.base / "SessionLock-1.0.0.zip"
        package.archive(self.combined, archive)
        checksum = package.file_record(archive)["sha256"] + "  " + archive.name + "\n"
        self.assertEqual(archive.with_name(archive.name + ".sha256").read_bytes(),
                         checksum.encode())
        extracted = self.base / "extracted"
        package.extract_archive(archive, extracted)
        self.assertEqual(package.scan(extracted), package.scan(self.combined))
        with zipfile.ZipFile(archive) as zipped:
            entry = zipped.getinfo("SessionLock.bundle/Contents/MacOS/SessionLock")
            self.assertEqual(stat.S_IMODE(entry.external_attr >> 16), 0o755)
            self.assertNotIn("\\", "".join(zipped.namelist()))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((extracted / package.BUNDLE /
                                         "Contents/MacOS/SessionLock").stat().st_mode), 0o755)
        for command in (["verify", "--root", str(extracted)],
                        ["--help"]):
            completed = subprocess.run([sys_executable(), str(SCRIPT), *command],
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(source_before, package.scan(self.repo / "SessionLock/SessionLock-test/Plugins"))

    def test_cli_stage_compiler_required_and_success(self):
        args = [sys_executable(), str(SCRIPT), "stage", "--target", "macos-universal",
                "--output", str(self.mac)]
        failed = subprocess.run(args, cwd=self.repo, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        passed = subprocess.run(args + ["--compiler", "AppleClang 17.0.0"], cwd=self.repo,
                                capture_output=True)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        package.load_artifact(self.mac, package.TARGETS[0])

    def test_verify_artifact_cli_detects_target_and_checks_native_tests(self):
        self.stage_both()
        for root, target in zip((self.mac, self.windows), package.TARGETS):
            with self.subTest(target=target):
                result = subprocess.run(
                    [sys_executable(), str(SCRIPT), "verify-artifact", "--root", str(root)],
                    capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(package.verify_artifact(root)["target"], target)
        test = self.mac / "native-tests/backend_smoke"
        original = test.read_bytes()
        test.write_bytes(original + b"modified")
        result = subprocess.run(
            [sys_executable(), str(SCRIPT), "verify-artifact", "--root", str(self.mac)],
            capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Inventory mismatch", result.stderr)
        test.write_bytes(original)
        if os.name != "nt":
            test.chmod(0o644)
            with self.assertRaisesRegex(package.PackageError, "Native test must be executable"):
                package.verify_artifact(self.mac)

    def test_exact_json_compiler_and_ignored_in_repository_outputs(self):
        compiler = ' {"id": "AppleClang", "version": "17.0.0"} '
        build = self.repo / "SessionLock/build"
        mac, windows, combined = (build / name for name in ("mac", "windows", "package"))
        metadata = package.stage(self.repo, package.TARGETS[0], mac, compiler)
        self.assertEqual(metadata["compiler"], compiler)
        self.assertEqual(package.read_json(mac / "artifact.json")["compiler"], compiler)
        package.stage(self.repo, package.TARGETS[1], windows,
                      '{"id":"MSVC","version":"19.44"}')
        package.combine(mac, windows, combined)
        package.verify(combined)
        self.assertEqual(self.run_git(self.repo, "status", "--porcelain").strip(), b"")
        package.source_snapshot(self.repo)

    def test_chmod_is_requested_on_every_host(self):
        original = Path.chmod
        calls = []

        def capture(path, mode, *args, **kwargs):
            calls.append((path, mode))
            return original(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "chmod", capture):
            package.stage(self.repo, package.TARGETS[0], self.mac, "AppleClang 17")
        binary = self.mac / "payload/SessionLock.bundle/Contents/MacOS/SessionLock"
        self.assertIn((binary, 0o755), calls)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o755)

    def test_artifact_transport_modes_restored_and_archive_modes_preserved(self):
        self.stage_both()
        binary = self.mac / "payload/SessionLock.bundle/Contents/MacOS/SessionLock"
        binary.chmod(0o644)
        package.combine(self.mac, self.windows, self.combined)
        final_binary = self.combined / "SessionLock.bundle/Contents/MacOS/SessionLock"
        license_file = self.combined / "SessionLock.bundle/Contents/Resources/LICENSE"
        license_file.chmod(0o640)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(final_binary.stat().st_mode), 0o755)
            final_binary.chmod(0o644)
            with self.assertRaisesRegex(package.PackageError, "executable"):
                package.verify(self.combined)
            final_binary.chmod(0o755)
        zipped = self.base / "modes.zip"
        package.archive(self.combined, zipped)
        with zipfile.ZipFile(zipped) as archive_file:
            mode = archive_file.getinfo(
                "SessionLock.bundle/Contents/Resources/LICENSE").external_attr >> 16
            self.assertEqual(stat.S_IMODE(mode), stat.S_IMODE(license_file.stat().st_mode))

    def test_dirty_source_and_untracked_source_rejected(self):
        license_file = self.repo / "LICENSE"
        original = license_file.read_bytes()
        license_file.write_bytes(original + b"dirty\n")
        with self.assertRaises(package.PackageError):
            package.stage(self.repo, package.TARGETS[0], self.mac, "compiler")
        license_file.write_bytes(original)
        (self.repo / "unexpected.txt").write_bytes(b"untracked")
        with self.assertRaises(package.PackageError):
            package.stage(self.repo, package.TARGETS[0], self.mac, "compiler")
        self.assertFalse(self.mac.exists())

    def test_sdk_dirty_and_head_mismatch_rejected(self):
        sdk = self.repo / package.SDK
        license_file = sdk / "LICENSE.md"
        original = license_file.read_bytes()
        license_file.write_bytes(b"dirty")
        with self.assertRaises(package.PackageError):
            package.source_snapshot(self.repo)
        license_file.write_bytes(original)
        (sdk / "untracked").write_bytes(b"untracked")
        with self.assertRaises(package.PackageError):
            package.source_snapshot(self.repo)
        (sdk / "untracked").unlink()
        self.run_git(sdk, "config", "user.name", "Test")
        self.run_git(sdk, "config", "user.email", "test@example.invalid")
        license_file.write_bytes(original + b"new revision\n")
        self.commit(sdk)
        with self.assertRaises(package.PackageError):
            package.source_snapshot(self.repo)

    def test_artifact_metadata_mismatches(self):
        self.stage_both()
        original = package.read_json(self.windows / "artifact.json")
        changes = {
            "source_commit": "a" * 40, "sdk_commit": "b" * 40, "target": "macos-universal",
            "version": "2.0.0", "compiler": "", "signing": "developer-id-notarized",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                modified = copy.deepcopy(original)
                modified[field] = value
                package.write_json(self.windows / "artifact.json", modified)
                with self.assertRaises(package.PackageError):
                    package.combine(self.mac, self.windows, self.combined)
                self.assertFalse(self.combined.exists())
        package.write_json(self.windows / "artifact.json", original)
        # Even a self-consistent alternate version is rejected across targets.
        alternate = copy.deepcopy(original)
        alternate["version"] = "2.0.0"
        alternate["version_bytes"] = b"2.0.0\n".hex()
        (self.windows / "VERSION").write_bytes(b"2.0.0\n")
        alternate["inventory"]["VERSION"] = package.digest(b"2.0.0\n")
        package.write_json(self.windows / "artifact.json", alternate)
        with self.assertRaisesRegex(package.PackageError, "version"):
            package.combine(self.mac, self.windows, self.combined)

    def test_shared_resources_mismatch_with_updated_hash_rejected(self):
        self.stage_both()
        resource = "payload/SessionLock/Contents/Resources/LICENSE"
        (self.windows / resource).write_bytes(b"Different MIT license\n")
        self.rewrite_artifact(self.windows, lambda m: m["inventory"].update(
            {resource: package.file_record(self.windows / resource)}))
        with self.assertRaisesRegex(package.PackageError, "Shared resource"):
            package.combine(self.mac, self.windows, self.combined)

    def test_stage_stale_resources_and_extra_build_files_rejected(self):
        root = self.repo / "SessionLock/SessionLock-test/Plugins/SessionLock.bundle"
        license_file = root / "Contents/Resources/LICENSE"
        original = license_file.read_bytes()
        license_file.write_bytes(b"stale")
        with self.assertRaisesRegex(package.PackageError, "resource"):
            package.stage(self.repo, package.TARGETS[0], self.mac, "compiler")
        license_file.write_bytes(original)
        (root / "extra").write_bytes(b"extra")
        with self.assertRaisesRegex(package.PackageError, "layout"):
            package.stage(self.repo, package.TARGETS[0], self.mac, "compiler")

    def test_inventory_tampering_at_both_boundaries(self):
        self.combine()
        for root, loader, binary in (
                (self.mac, lambda: package.load_artifact(self.mac, package.TARGETS[0]),
                 "payload/SessionLock.bundle/Contents/MacOS/SessionLock"),
                (self.combined, lambda: package.verify(self.combined),
                 "SessionLock.bundle/Contents/MacOS/SessionLock")):
            original = (root / binary).read_bytes()
            for kind in ("modified", "missing", "extra", "empty-directory"):
                with self.subTest(root=root.name, kind=kind):
                    if kind == "modified":
                        (root / binary).write_bytes(original + b"changed")
                    elif kind == "missing":
                        (root / binary).unlink()
                    elif kind == "extra":
                        (root / "extra").write_bytes(b"extra")
                    else:
                        (root / "empty").mkdir()
                    with self.assertRaises(package.PackageError):
                        loader()
                    if kind in ("modified", "missing"):
                        (root / binary).write_bytes(original)
                        package.executable(root / binary)
                    elif kind == "extra":
                        (root / "extra").unlink()
                    else:
                        (root / "empty").rmdir()

    def test_symlinks_rejected(self):
        self.combine()
        destination = self.combined / "link"
        try:
            destination.symlink_to(self.repo / "LICENSE")
        except (OSError, NotImplementedError):
            self.skipTest("Host does not permit symlink creation")
        with self.assertRaisesRegex(package.PackageError, "Symlink"):
            package.verify(self.combined)
        destination.unlink()
        source_binary = self.mac / "payload/SessionLock.bundle/Contents/MacOS/SessionLock"
        source_binary.unlink()
        source_binary.symlink_to(self.repo / "LICENSE")
        with self.assertRaisesRegex(package.PackageError, "Symlink"):
            package.load_artifact(self.mac, package.TARGETS[0])

    def test_portable_windows_paths_and_unsafe_inventory(self):
        windows_path = PureWindowsPath(r"C:\stage\payload\SessionLock\Contents\Windows64\SessionLock.4DX")
        relative = windows_path.relative_to(PureWindowsPath(r"C:\stage"))
        serialized = package.portable(relative)
        self.assertEqual(serialized, "payload/SessionLock/Contents/Windows64/SessionLock.4DX")
        package.safe_path(serialized)
        for name in ("../escape", "/absolute", "C:/file", "a\\b", "a/../b", "a//b",
                     "./a", "a/./b", "a/", "", "a\0b", "//server/share"):
            with self.subTest(name=name), self.assertRaises(package.PackageError):
                package.safe_path(name)
        self.stage_both()
        self.rewrite_artifact(self.mac, lambda m: m["inventory"].update(
            {"../escape": package.digest(b"x")}))
        with self.assertRaises(package.PackageError):
            package.load_artifact(self.mac, package.TARGETS[0])

    def test_crlf_checkout_and_sdk_object_export(self):
        clone = self.base / "crlf-clone"
        self.run_git(self.base, "-c", "protocol.file.allow=always", "clone", "--quiet",
                     "--no-checkout", str(self.repo), str(clone))
        self.run_git(clone, "config", "core.autocrlf", "true")
        self.run_git(clone, "config", "core.eol", "crlf")
        self.run_git(clone, "checkout", "--quiet", "HEAD")
        self.run_git(clone, "-c", "protocol.file.allow=always", "submodule", "update",
                     "--init", "--quiet")
        sdk = clone / package.SDK
        self.run_git(sdk, "config", "core.autocrlf", "true")
        self.run_git(sdk, "config", "core.eol", "crlf")
        (sdk / "LICENSE.md").unlink()
        self.run_git(sdk, "checkout", "--", "LICENSE.md")
        self.assertIn(b"\r\n", (sdk / "LICENSE.md").read_bytes())
        self.assertEqual((clone / "control.txt").read_bytes(), b"unconstrained\r\ncontrol\r\n")
        for name in ("LICENSE", "VERSION", "SessionLock/manifest.json"):
            self.assertEqual((clone / name).read_bytes(), (self.repo / name).read_bytes())
            self.assertNotIn(b"\r\n", (clone / name).read_bytes())
        shutil.copytree(self.repo / "SessionLock/SessionLock-test",
                        clone / "SessionLock/SessionLock-test")
        shutil.copytree(self.repo / "SessionLock/build", clone / "SessionLock/build")
        # Simulate the CMake SDK resource copy on a CRLF Windows checkout.
        built = clone / "SessionLock/SessionLock-test/Plugins/SessionLock/Contents/Resources/LICENSE.md"
        shutil.copyfile(sdk / "LICENSE.md", built)
        result = self.base / "crlf-stage"
        package.stage(clone, package.TARGETS[1], result, "MSVC 19.44")
        staged_license = result / "payload/SessionLock/Contents/Resources/LICENSE.md"
        self.assertEqual(staged_license.read_bytes(),
                         self.run_git(sdk, "show", "HEAD:LICENSE.md"))
        self.assertNotIn(b"\r\n", staged_license.read_bytes())

    def test_unconstrained_root_resource_conversion_rejected(self):
        attributes = self.repo / ".gitattributes"
        attributes.write_bytes(b"")
        self.commit(self.repo)
        self.run_git(self.repo, "config", "core.autocrlf", "true")
        self.run_git(self.repo, "config", "core.eol", "crlf")
        (self.repo / "LICENSE").unlink()
        self.run_git(self.repo, "checkout", "--", "LICENSE")
        self.assertIn(b"\r\n", (self.repo / "LICENSE").read_bytes())
        # Git considers CRLF clean; packaging must still detect byte drift.
        self.assertEqual(self.run_git(self.repo, "status", "--porcelain").strip(), b"")
        with self.assertRaisesRegex(package.PackageError, "checkout bytes"):
            package.source_snapshot(self.repo)

    def test_architecture_and_bounds_validation(self):
        package.validate_macho(macho())
        package.validate_pe(pe())
        invalid_machos = [b"", pe(), macho()[:80]]
        for offset, fmt, value in ((4, ">I", 3), (8, ">I", 7), (16, ">I", 999999),
                                   (20, ">I", 99999), (24, ">I", 32),
                                   (36, ">I", 64), (68, "<I", 0x0100000C),
                                   (80, "<I", 1), (84, "<I", 8)):
            raw = bytearray(macho())
            struct.pack_into(fmt, raw, offset, value)
            invalid_machos.append(bytes(raw))
        for index, raw in enumerate(invalid_machos):
            with self.subTest(macho=index), self.assertRaises(package.PackageError):
                package.validate_macho(raw)
        invalid_pes = [b"", macho(), pe()[:100]]
        for offset, fmt, value in ((60, "<I", 99999), (68, "<H", 0xAA64),
                                   (84, "<H", 999), (88, "<H", 0x10B),
                                   (216, "<I", 999999)):
            raw = bytearray(pe())
            struct.pack_into(fmt, raw, offset, value)
            invalid_pes.append(bytes(raw))
        for index, raw in enumerate(invalid_pes):
            with self.subTest(pe=index), self.assertRaises(package.PackageError):
                package.validate_pe(raw)

    def test_plist_and_manifest_validation(self):
        for field, value in (("CFBundlePackageType", "APPL"),
                             ("CFBundleExecutable", "../SessionLock"),
                             ("CFBundleIdentifier", "other"),
                             ("CFBundleVersion", "2.0.0")):
            original = plistlib.loads((self.repo / "SessionLock/SessionLock-test/Plugins" /
                                      "SessionLock.bundle/Contents/Info.plist").read_bytes())
            original[field] = value
            with self.subTest(field=field), self.assertRaises(package.PackageError):
                package.validate_plist(plistlib.dumps(original), "1.0.0")
        for field, value in (("name", "Other"), ("id", True), ("commands", [])):
            invalid = copy.deepcopy(self.manifest)
            invalid[field] = value
            with self.assertRaises(package.PackageError):
                package.validate_manifest(json.dumps(invalid).encode())
        for field, value in (("syntax", "SessionLock Start():O"),
                             ("threadSafe", "true"), ("theme", "Other")):
            invalid = copy.deepcopy(self.manifest)
            invalid["commands"][0][field] = value
            with self.assertRaises(package.PackageError):
                package.validate_manifest(json.dumps(invalid).encode())

    def test_final_inventory_cannot_hide_modified_binary(self):
        self.combine()
        name = "SessionLock.bundle/Contents/Windows64/SessionLock.4DX"
        binary = self.combined / name
        binary.write_bytes(binary.read_bytes() + b"signed or tampered")
        provenance = package.read_json(self.combined / "provenance.json")
        provenance["inventory"][name] = package.file_record(binary)
        package.write_json(self.combined / "provenance.json", provenance)
        with self.assertRaisesRegex(package.PackageError, "pre-signing"):
            package.verify(self.combined)

    def test_refresh_preserves_source_artifacts_and_signed_archive_round_trip(self):
        self.combine()
        original_bytes = (self.combined / "provenance.json").read_bytes()
        original = package.read_json(self.combined / "provenance.json")
        staged_bytes = [(root / "artifact.json").read_bytes() for root in (self.mac, self.windows)]
        self.simulate_signing()
        with self.assertRaises(package.PackageError):
            package.verify(self.combined)
        completed = subprocess.run(
            [sys_executable(), str(SCRIPT), "refresh", "--root", str(self.combined),
             "--signing", package.SIGNED], capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        final = package.verify(self.combined)
        self.assertEqual(final["signing"], package.SIGNED)
        self.assertEqual(final["artifacts"], original["artifacts"])
        self.assertEqual(staged_bytes, [(root / "artifact.json").read_bytes()
                                       for root in (self.mac, self.windows)])
        self.assertNotEqual((self.combined / "provenance.json").read_bytes(), original_bytes)
        archive = self.base / "signed.zip"
        package.archive(self.combined, archive)
        extracted = self.base / "signed-extracted"
        package.extract_archive(archive, extracted)
        self.assertEqual(package.verify(extracted), final)
        with self.assertRaisesRegex(package.PackageError, "already finalized"):
            package.refresh(self.combined, package.SIGNED)
        # A finalized inventory cannot be rehashed after further byte changes.
        binary = self.combined / package.MAC_BINARY
        binary.write_bytes(binary.read_bytes() + b"tampered after finalization")
        with self.assertRaises(package.PackageError):
            package.verify(self.combined)
        with self.assertRaises(package.PackageError):
            package.refresh(self.combined, package.SIGNED)

    def test_refresh_rejects_unsigned_and_unsupported_declarations(self):
        self.combine()
        before = (self.combined / "provenance.json").read_bytes()
        for signing in (package.SIGNED, "ad-hoc", "unsigned-pre-signing", ""):
            with self.subTest(signing=signing), self.assertRaises(package.PackageError):
                package.refresh(self.combined, signing)
            self.assertEqual((self.combined / "provenance.json").read_bytes(), before)

    def test_refresh_rejects_changes_outside_signing_allowlist(self):
        self.combine()
        before = (self.combined / "provenance.json").read_bytes()
        self.simulate_signing()
        for name in ("Contents/Resources/LICENSE", "Contents/Resources/manifest.json",
                     "Contents/Resources/LICENSE.md", "Contents/Info.plist",
                     "Contents/Windows64/SessionLock.4DX"):
            path = self.combined / package.BUNDLE / name
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.subTest(name=name), self.assertRaisesRegex(
                    package.PackageError, "immutable"):
                package.refresh(self.combined, package.SIGNED)
            self.assertEqual((self.combined / "provenance.json").read_bytes(), before)
            path.write_bytes(original)
        extra = self.combined / package.BUNDLE / "Contents/_CodeSignature/extra"
        extra.write_bytes(b"unexpected")
        with self.assertRaises(package.PackageError):
            package.refresh(self.combined, package.SIGNED)
        extra.unlink()
        resources = self.combined / package.CODE_RESOURCES
        resources.write_bytes(b"not a plist")
        with self.assertRaisesRegex(package.PackageError, "resource plist"):
            package.refresh(self.combined, package.SIGNED)
        self.assertEqual((self.combined / "provenance.json").read_bytes(), before)

    def test_refresh_requires_valid_original_pre_signing_inventory(self):
        self.combine()
        self.simulate_signing()
        provenance = package.read_json(self.combined / "provenance.json")
        provenance["inventory"][package.MAC_BINARY] = package.file_record(
            self.combined / package.MAC_BINARY)
        package.write_json(self.combined / "provenance.json", provenance)
        before = (self.combined / "provenance.json").read_bytes()
        with self.assertRaisesRegex(package.PackageError, "pre-signing"):
            package.refresh(self.combined, package.SIGNED)
        self.assertEqual((self.combined / "provenance.json").read_bytes(), before)

    def test_archive_rejects_unsafe_entries(self):
        for index, name in enumerate(("../escape", "/absolute", "a\\b",
                                      "C:/file", "unlisted-file", "SessionLock.bundle/../escape")):
            zipped = self.base / ("bad-" + str(index) + ".zip")
            with zipfile.ZipFile(zipped, "w") as archive_file:
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive_file.writestr(info, b"bad")
            with self.subTest(name=name), self.assertRaises(package.PackageError):
                package.extract_archive(zipped, self.base / ("extract-" + str(index)))
        zipped = self.base / "symlink.zip"
        with zipfile.ZipFile(zipped, "w") as archive_file:
            info = zipfile.ZipInfo("SessionLock.bundle/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive_file.writestr(info, b"../../escape")
        with self.assertRaises(package.PackageError):
            package.extract_archive(zipped, self.base / "extract-symlink")

    def test_no_overwrites_or_archive_inside_root(self):
        self.combine()
        with self.assertRaises(package.PackageError):
            package.combine(self.mac, self.windows, self.combined)
        with self.assertRaisesRegex(package.PackageError, "source artifact"):
            package.combine(self.mac, self.windows, self.mac / "nested")
        built = self.repo / "SessionLock/SessionLock-test/Plugins/SessionLock.bundle"
        with self.assertRaisesRegex(package.PackageError, "inside the built plugin"):
            package.stage(self.repo, package.TARGETS[0], built / "nested", "compiler")
        with self.assertRaises(package.PackageError):
            package.archive(self.combined, self.combined / "archive.zip")
        self.assertFalse((self.combined / "archive.zip").exists())
        zip_path = self.base / "existing.zip"
        zip_path.write_bytes(b"preserve me")
        with self.assertRaises(package.PackageError):
            package.archive(self.combined, zip_path)
        self.assertEqual(zip_path.read_bytes(), b"preserve me")

    def test_duplicate_json_keys_rejected(self):
        value = self.base / "duplicates.json"
        value.write_bytes(b'{"schema":1,"schema":1}')
        with self.assertRaisesRegex(package.PackageError, "Duplicate"):
            package.read_json(value)


def sys_executable():
    import sys
    return sys.executable


if __name__ == "__main__":
    unittest.main()
