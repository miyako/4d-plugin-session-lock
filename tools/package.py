#!/usr/bin/env python3
"""SessionLock staging, provenance verification and portable ZIP creation.

Hashes always describe exact bytes, never newline-normalized content. Artifact
inventories always describe pre-signing binaries. Refresh records final signed
bytes without changing those inventories. It does not authenticate Developer ID
signatures or notarization; the caller must independently enforce those gates.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile


TARGETS = ("macos-universal", "windows-x64")
SDK = "4D-Plugin-SDK"
BUNDLE = "SessionLock.bundle"
RESOURCES = ("manifest.json", "LICENSE", "LICENSE.md")
COMMANDS = ("Start", "State", "Events", "Stop")
SIGNED = "developer-id-notarized"
MAC_BINARY = BUNDLE + "/Contents/MacOS/SessionLock"
CODE_RESOURCES = BUNDLE + "/Contents/_CodeSignature/CodeResources"
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class PackageError(ValueError):
    """Untrusted or inconsistent packaging input."""


def require(condition, message):
    if not condition:
        raise PackageError(message)


def portable(path):
    """Serialize a relative path, including a PureWindowsPath on POSIX."""
    return path.as_posix()


def safe_path(value):
    require(isinstance(value, str) and value, "Empty or non-string inventory path")
    path = PurePosixPath(value)
    require("\\" not in value and ":" not in value and "\0" not in value,
            "Non-portable inventory path: " + value)
    require(not path.is_absolute() and all(p not in ("", ".", "..") for p in value.split("/"))
            and path.as_posix() == value and not PureWindowsPath(value).drive,
            "Unsafe inventory path: " + value)
    return path


def digest(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def file_record(path):
    return digest(path.read_bytes())


def is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def no_link_ancestors(path):
    for ancestor in (path, *path.parents):
        require(not is_link(ancestor), "Symlink/junction path rejected: " + str(ancestor))


def scan(root):
    require(root.is_dir() and not is_link(root), "Not a real directory: " + str(root))
    result = {}
    folded = set()
    for path in sorted(root.rglob("*")):
        name = portable(path.relative_to(root))
        safe_path(name)
        require(name.casefold() not in folded, "Case-colliding paths: " + name)
        folded.add(name.casefold())
        require(not is_link(path), "Symlink/junction rejected: " + name)
        require(path.is_file() or path.is_dir(), "Special file rejected: " + name)
        if path.is_file():
            result[name] = file_record(path)
    return result


def validate_inventory(files):
    require(isinstance(files, dict) and files, "Missing file inventory")
    folded = set()
    for name, record in files.items():
        safe_path(name)
        require(name.casefold() not in folded, "Case-colliding inventory paths")
        folded.add(name.casefold())
        require(isinstance(record, dict) and set(record) == {"sha256", "size"},
                "Invalid file record")
        require(isinstance(record["sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]), "Invalid SHA-256")
        require(type(record["size"]) is int and record["size"] >= 0, "Invalid file size")


def check_tree(root, files, metadata):
    validate_inventory(files)
    actual = scan(root)
    require(metadata in actual, "Missing " + metadata)
    del actual[metadata]
    require(actual == files, "Inventory mismatch (modified, extra or missing files)")
    # Empty/unlisted directories are also rejected.
    allowed_dirs = set()
    for name in set(files) | {metadata}:
        allowed_dirs.update(p.as_posix() for p in PurePosixPath(name).parents if p.as_posix() != ".")
    for path in root.rglob("*"):
        if path.is_dir():
            require(portable(path.relative_to(root)) in allowed_dirs, "Unlisted directory")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(path):
    require(path.is_file() and not path.is_symlink(), "Missing or symlinked JSON: " + str(path))
    try:
        return json.loads(path.read_bytes(), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError("Invalid JSON: " + str(path)) from exc


def write_json(path, data):
    path.write_bytes((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def source_snapshot(repo):
    no_link_ancestors(repo / SDK)
    require(not git(repo, "status", "--porcelain", "--untracked-files=all").strip(),
            "Source must be clean, including untracked files")
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    require(SHA.fullmatch(commit), "Invalid source commit")
    entry = git(repo, "ls-tree", "HEAD", SDK).decode().split()
    require(len(entry) == 4 and entry[0:2] == ["160000", "commit"] and entry[3] == SDK,
            "SDK must be a pinned gitlink")
    sdk_commit = entry[2]
    require(git(repo / SDK, "rev-parse", "HEAD").decode().strip() == sdk_commit,
            "SDK HEAD does not match the gitlink")
    require(not git(repo / SDK, "status", "--porcelain", "--untracked-files=all").strip(),
            "SDK must be clean, including untracked files")
    inputs = {}
    for destination, source in (("LICENSE", "LICENSE"), ("manifest.json", "SessionLock/manifest.json"),
                                ("VERSION", "VERSION")):
        raw = git(repo, "show", commit + ":" + source)
        require((repo / source).read_bytes() == raw,
                source + " checkout bytes differ from Git; declare text eol=lf")
        inputs[destination] = raw
    inputs["LICENSE.md"] = git(repo / SDK, "show", sdk_commit + ":LICENSE.md")
    version = parse_version(inputs["VERSION"])
    return commit, sdk_commit, version, inputs


def parse_version(raw):
    try:
        version = raw.decode("ascii").strip()
    except UnicodeError as exc:
        raise PackageError("VERSION must be ASCII") from exc
    require(VERSION.fullmatch(version), "Invalid semantic VERSION")
    return version


def validate_manifest(raw):
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError("Invalid plugin manifest") from exc
    require(isinstance(value, dict) and set(value) == {"name", "id", "commands"},
            "Invalid manifest fields")
    require(value["name"] == "SessionLock" and type(value["id"]) is int
            and 15001 <= value["id"] <= 32767, "Invalid plugin identity")
    commands = value["commands"]
    require(isinstance(commands, list) and len(commands) == len(COMMANDS), "Invalid commands")
    for command, name in zip(commands, COMMANDS):
        require(isinstance(command, dict)
                and set(command) == {"theme", "syntax", "threadSafe"}
                and command["theme"] == "SessionLock"
                and command["syntax"] == "SessionLock " + name + "():J"
                and command["threadSafe"] is True, "Invalid command name, type or safety")


def validate_plist(raw, version):
    try:
        value = plistlib.loads(raw)
    except Exception as exc:
        raise PackageError("Invalid Info.plist") from exc
    require(isinstance(value, dict) and value.get("CFBundlePackageType") == "BNDL"
            and value.get("CFBundleExecutable") == "SessionLock"
            and value.get("CFBundleIdentifier") == "com.miyako.SessionLock",
            "Invalid macOS bundle identity")
    require(value.get("CFBundleVersion") == version
            and value.get("CFBundleShortVersionString") == version, "Info.plist version mismatch")


def validate_pe(raw):
    require(len(raw) >= 64 and raw[:2] == b"MZ", "Missing PE DOS header")
    offset = struct.unpack_from("<I", raw, 60)[0]
    require(offset >= 64 and offset + 24 <= len(raw), "PE header out of bounds")
    require(raw[offset:offset + 4] == b"PE\0\0", "Missing PE signature")
    machine, sections, _, _, _, optional_size, _ = struct.unpack_from("<HHIIIHH", raw, offset + 4)
    require(machine == 0x8664, "PE must be IMAGE_FILE_MACHINE_AMD64")
    require(optional_size >= 112 and offset + 24 + optional_size + sections * 40 <= len(raw),
            "PE optional/section headers out of bounds")
    require(struct.unpack_from("<H", raw, offset + 24)[0] == 0x20B, "PE must be PE32+")
    table = offset + 24 + optional_size
    for i in range(sections):
        size, pointer = struct.unpack_from("<II", raw, table + i * 40 + 16)
        require(size == 0 or (pointer >= table + sections * 40 and pointer + size <= len(raw)),
                "PE section out of bounds")


def validate_macho(raw):
    require(len(raw) >= 8, "Missing universal Mach-O header")
    magic = raw[:4]
    formats = {b"\xca\xfe\xba\xbe": (">", False), b"\xbe\xba\xfe\xca": ("<", False),
               b"\xca\xfe\xba\xbf": (">", True), b"\xbf\xba\xfe\xca": ("<", True)}
    require(magic in formats, "Mach-O must be a universal fat binary")
    endian, fat64 = formats[magic]
    count = struct.unpack_from(endian + "I", raw, 4)[0]
    entry_size = 32 if fat64 else 20
    end = 8 + count * entry_size
    require(count == 2 and end <= len(raw), "Expected exactly x86_64 and arm64 slices")
    cpus, ranges = set(), []
    for i in range(count):
        entry = 8 + i * entry_size
        cpu, subtype = struct.unpack_from(endian + "II", raw, entry)
        if fat64:
            offset, size, align, reserved = struct.unpack_from(endian + "QQII", raw, entry + 8)
            require(reserved == 0, "Invalid fat64 reserved field")
        else:
            offset, size, align = struct.unpack_from(endian + "III", raw, entry + 8)
        require(cpu in (0x01000007, 0x0100000C) and cpu not in cpus,
                "Unexpected or duplicate Mach-O architecture")
        cpus.add(cpu)
        require(align <= 31 and offset % (1 << align) == 0 and offset >= end
                and size >= 32 and offset + size <= len(raw), "Mach-O slice out of bounds/alignment")
        require(all(offset + size <= a or offset >= b for a, b in ranges),
                "Overlapping Mach-O slices")
        ranges.append((offset, offset + size))
        header = raw[offset:offset + size]
        require(header[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"), "Invalid 64-bit slice")
        slice_endian = "<" if header[:4] == b"\xcf\xfa\xed\xfe" else ">"
        slice_cpu, slice_subtype, filetype, ncmds, sizeofcmds = struct.unpack_from(
            slice_endian + "IIIII", header, 4)
        require(slice_cpu == cpu and slice_subtype == subtype and filetype == 8,
                "Slice identity must match a Mach-O bundle")
        require(32 + sizeofcmds <= size and ncmds <= sizeofcmds // 8,
                "Mach-O load commands out of bounds")
        cursor = 32
        for _ in range(ncmds):
            require(cursor + 8 <= 32 + sizeofcmds, "Truncated Mach-O load command")
            _, cmdsize = struct.unpack_from(slice_endian + "II", header, cursor)
            require(cmdsize >= 8 and cmdsize % 4 == 0 and cursor + cmdsize <= 32 + sizeofcmds,
                    "Invalid Mach-O load command size")
            cursor += cmdsize
        require(cursor == 32 + sizeofcmds, "Mach-O load command size mismatch")


def payload_name(target):
    return BUNDLE if target == TARGETS[0] else "SessionLock"


def artifact_paths(target):
    prefix = "payload/" + payload_name(target) + "/Contents/"
    paths = {prefix + "Resources/" + name for name in RESOURCES}
    paths.add(prefix + ("MacOS/SessionLock" if target == TARGETS[0] else "Windows64/SessionLock.4DX"))
    if target == TARGETS[0]:
        paths.add(prefix + "Info.plist")
    suffix = "" if target == TARGETS[0] else ".exe"
    paths.update("native-tests/" + name + suffix for name in ("core_tests", "backend_smoke"))
    paths.add("VERSION")
    return paths


def validate_metadata(value, target=None):
    require(isinstance(value, dict) and set(value) == {
        "schema", "target", "source_commit", "sdk_commit", "version", "version_bytes",
        "compiler", "signing", "inventory"}, "Invalid artifact metadata fields")
    require(value["schema"] == 1 and type(value["schema"]) is int, "Unknown artifact schema")
    require(value["target"] in TARGETS and (target is None or value["target"] == target),
            "Artifact target mismatch")
    for key in ("source_commit", "sdk_commit"):
        require(isinstance(value[key], str) and SHA.fullmatch(value[key]), "Invalid " + key)
    require(isinstance(value["compiler"], str) and value["compiler"].strip(), "Compiler is required")
    require(value["signing"] == "unsigned-pre-signing", "Only pre-signing artifacts are supported")
    require(isinstance(value["version_bytes"], str)
            and re.fullmatch(r"(?:[0-9a-f]{2})+", value["version_bytes"]), "Invalid VERSION bytes")
    raw_version = bytes.fromhex(value["version_bytes"])
    require(parse_version(raw_version) == value["version"], "VERSION metadata mismatch")
    validate_inventory(value["inventory"])
    require(set(value["inventory"]) == artifact_paths(value["target"]), "Unexpected artifact layout")
    require(value["inventory"]["VERSION"] == digest(raw_version), "VERSION hash mismatch")


def validate_payload(root, target, version, check_executable=False):
    contents = root / "Contents"
    validate_manifest((contents / "Resources/manifest.json").read_bytes())
    for name in ("LICENSE", "LICENSE.md"):
        require((contents / "Resources" / name).read_bytes().strip(), "Empty license")
    if target == TARGETS[0]:
        validate_plist((contents / "Info.plist").read_bytes(), version)
        validate_macho((contents / "MacOS/SessionLock").read_bytes())
        if check_executable and os.name != "nt":
            require((contents / "MacOS/SessionLock").stat().st_mode & 0o111,
                    "macOS binary must be executable")
    else:
        validate_pe((contents / "Windows64/SessionLock.4DX").read_bytes())


def load_artifact(root, target, check_executable=False):
    metadata = read_json(root / "artifact.json")
    validate_metadata(metadata, target)
    check_tree(root, metadata["inventory"], "artifact.json")
    validate_payload(root / "payload" / payload_name(target), target, metadata["version"],
                     check_executable)
    if check_executable and target == TARGETS[0] and os.name != "nt":
        for name in ("core_tests", "backend_smoke"):
            require((root / "native-tests" / name).stat().st_mode & 0o111,
                    "Native test must be executable: " + name)
    return metadata


def verify_artifact(root):
    metadata = read_json(root / "artifact.json")
    validate_metadata(metadata)
    return load_artifact(root, metadata["target"], check_executable=True)


def new_output(path):
    no_link_ancestors(path)
    require(not path.exists(), "Output must not already exist: " + str(path))
    path.mkdir(parents=True)


def executable(path):
    path.chmod(0o755)


def stage(repo, target, output, compiler):
    require(target in TARGETS, "Unknown target")
    require(isinstance(compiler, str) and compiler.strip(), "--compiler is required")
    commit, sdk_commit, version, inputs = source_snapshot(repo)
    source = repo / "SessionLock/SessionLock-test/Plugins" / payload_name(target)
    no_link_ancestors(source)
    require(not output.resolve().is_relative_to(source.resolve()),
            "Staging output cannot be inside the built plugin")
    source_files = scan(source)
    expected = {p.split("/", 2)[2] for p in artifact_paths(target) if p.startswith("payload/")}
    require(set(source_files) == expected, "Unexpected built plugin layout")
    for name in ("manifest.json", "LICENSE"):
        require((source / "Contents/Resources" / name).read_bytes() == inputs[name],
                "Built resource differs from repository: " + name)
    # SDK license is exported from the pinned object, never newline-normalized.
    tests = []
    suffix = "" if target == TARGETS[0] else ".exe"
    for name in ("core_tests", "backend_smoke"):
        candidates = [repo / "SessionLock/build" / (name + suffix),
                      repo / "SessionLock/build/Release" / (name + suffix)]
        candidates = [p for p in candidates if p.exists()]
        require(len(candidates) == 1 and candidates[0].is_file() and not candidates[0].is_symlink(),
                "Missing, ambiguous or symlinked native test: " + name)
        no_link_ancestors(candidates[0])
        tests.append(candidates[0])
    new_output(output)
    try:
        destination = output / "payload" / payload_name(target)
        shutil.copytree(source, destination)
        (destination / "Contents/Resources/LICENSE.md").write_bytes(inputs["LICENSE.md"])
        (output / "VERSION").write_bytes(inputs["VERSION"])
        (output / "native-tests").mkdir()
        for test in tests:
            shutil.copy2(test, output / "native-tests" / test.name)
            executable(output / "native-tests" / test.name)
        if target == TARGETS[0]:
            executable(destination / "Contents/MacOS/SessionLock")
        metadata = {
            "schema": 1, "target": target, "source_commit": commit, "sdk_commit": sdk_commit,
            "version": version, "version_bytes": inputs["VERSION"].hex(), "compiler": compiler,
            "signing": "unsigned-pre-signing", "inventory": scan(output),
        }
        write_json(output / "artifact.json", metadata)
        load_artifact(output, target, check_executable=True)
    except Exception:
        shutil.rmtree(output)
        raise
    return metadata


def matching_artifacts(mac, windows):
    for key in ("source_commit", "sdk_commit", "version", "version_bytes"):
        require(mac[key] == windows[key], "Artifacts disagree on " + key)
    for name in RESOURCES:
        paths = ["payload/" + payload_name(t) + "/Contents/Resources/" + name for t in TARGETS]
        require(mac["inventory"][paths[0]] == windows["inventory"][paths[1]],
                "Shared resource mismatch: " + name)


def combined_inventory(artifacts):
    result = {}
    for target in TARGETS:
        prefix = "payload/" + payload_name(target) + "/"
        for name, record in artifacts[target]["inventory"].items():
            if name.startswith(prefix):
                destination = BUNDLE + "/" + name[len(prefix):]
                require(destination not in result or result[destination] == record,
                        "Conflicting combined path")
                result[destination] = record
    return result


def combine(mac_root, windows_root, output):
    mac = load_artifact(mac_root, TARGETS[0])
    windows = load_artifact(windows_root, TARGETS[1])
    matching_artifacts(mac, windows)
    require(not any(output.resolve().is_relative_to(root.resolve())
                    for root in (mac_root, windows_root)),
            "Combined output cannot be inside a source artifact")
    new_output(output)
    try:
        shutil.copytree(mac_root / "payload" / BUNDLE, output / BUNDLE)
        windows_dir = output / BUNDLE / "Contents/Windows64"
        windows_dir.mkdir()
        shutil.copy2(windows_root / "payload/SessionLock/Contents/Windows64/SessionLock.4DX",
                     windows_dir / "SessionLock.4DX")
        # Artifact transports may discard modes; restore the executable contract.
        executable(output / BUNDLE / "Contents/MacOS/SessionLock")
        artifacts = dict(zip(TARGETS, (mac, windows)))
        provenance = {
            "schema": 1, "signing": "unsigned-pre-signing",
            "source_commit": mac["source_commit"], "sdk_commit": mac["sdk_commit"],
            "version": mac["version"], "artifacts": artifacts, "inventory": scan(output),
        }
        write_json(output / "provenance.json", provenance)
        verify(output)
    except Exception:
        shutil.rmtree(output)
        raise


def validate_provenance(provenance):
    require(isinstance(provenance, dict) and set(provenance) == {
        "schema", "signing", "source_commit", "sdk_commit", "version", "artifacts", "inventory"},
        "Invalid provenance fields")
    require(type(provenance["schema"]) is int and provenance["schema"] == 1
            and provenance["signing"] in ("unsigned-pre-signing", SIGNED),
            "Unsupported provenance schema/signing")
    artifacts = provenance["artifacts"]
    require(isinstance(artifacts, dict) and set(artifacts) == set(TARGETS), "Incomplete artifact set")
    for target in TARGETS:
        validate_metadata(artifacts[target], target)
    matching_artifacts(artifacts[TARGETS[0]], artifacts[TARGETS[1]])
    for key in ("source_commit", "sdk_commit", "version"):
        require(provenance[key] == artifacts[TARGETS[0]][key], "Provenance mismatch: " + key)
    original = combined_inventory(artifacts)
    final = provenance["inventory"]
    validate_inventory(final)
    if provenance["signing"] == "unsigned-pre-signing":
        require(final == original, "Final bytes differ from pre-signing artifact inventory")
    else:
        require(set(final) == set(original) | {CODE_RESOURCES},
                "Signing may only add Contents/_CodeSignature/CodeResources")
        for name, record in original.items():
            if name != MAC_BINARY:
                require(final[name] == record, "Signing changed an immutable source file: " + name)
        require(final[MAC_BINARY] != original[MAC_BINARY], "Signing did not change macOS code")
        require(final[CODE_RESOURCES]["size"] > 0, "Empty code-signature resource")


def validate_package(root, provenance):
    validate_provenance(provenance)
    check_tree(root, provenance["inventory"], "provenance.json")
    for target in TARGETS:
        validate_payload(root / BUNDLE, target, provenance["version"], check_executable=True)
    if provenance["signing"] == SIGNED:
        try:
            resources = plistlib.loads((root / CODE_RESOURCES).read_bytes())
        except Exception as exc:
            raise PackageError("Invalid code-signature resource plist") from exc
        require(isinstance(resources, dict) and resources, "Empty code-signature resource plist")


def verify(root):
    provenance = read_json(root / "provenance.json")
    validate_package(root, provenance)
    return provenance


def refresh(root, signing):
    """One-way, authorized final-byte recording, not signature authentication."""
    require(signing == SIGNED, "Unsupported signing declaration")
    original = read_json(root / "provenance.json")
    validate_provenance(original)
    require(original["signing"] == "unsigned-pre-signing",
            "Refresh requires original unsigned provenance; already finalized")
    final = dict(original)
    final["signing"] = signing
    final["inventory"] = scan(root)
    del final["inventory"]["provenance.json"]
    validate_package(root, final)
    # Replace only after every immutable source binding and final file validates.
    pending = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".provenance-", suffix=".json",
                                         dir=root.parent, delete=False) as handle:
            pending = Path(handle.name)
        write_json(pending, final)
        pending.replace(root / "provenance.json")
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return final


def extract_archive(archive_path, destination):
    """Extract our portable archive without trusting ZIP paths or file types."""
    new_output(destination)
    try:
        with zipfile.ZipFile(archive_path) as archive_file:
            seen = set()
            for entry in archive_file.infolist():
                safe_path(entry.filename)
                require(entry.filename.casefold() not in seen, "Duplicate ZIP path")
                seen.add(entry.filename.casefold())
                mode = entry.external_attr >> 16
                require(stat.S_ISREG(mode) and stat.S_IMODE(mode) <= 0o777,
                        "ZIP symlink/directory/special file or privileged mode rejected")
                require(entry.filename == "provenance.json" or entry.filename.startswith(BUNDLE + "/"),
                        "Unexpected ZIP root")
                path = destination.joinpath(*PurePosixPath(entry.filename).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                require(not path.exists(), "Duplicate or conflicting ZIP entry")
                with archive_file.open(entry) as source, path.open("xb") as sink:
                    shutil.copyfileobj(source, sink)
                path.chmod(stat.S_IMODE(mode))
        verify(destination)
    except Exception:
        shutil.rmtree(destination)
        raise


def archive(root, output):
    verify(root)
    no_link_ancestors(output)
    require(not output.exists() and not output.is_symlink(), "ZIP output already exists")
    sidecar = output.with_name(output.name + ".sha256")
    require(not sidecar.exists() and not sidecar.is_symlink(), "Checksum output already exists")
    # Keep both the archive and extraction outside the verified input tree.
    require(not output.resolve().is_relative_to(root.resolve()), "ZIP cannot be inside its input")
    output.parent.mkdir(parents=True, exist_ok=True)
    before = scan(root)
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive_file:
            for name in before:
                path = root.joinpath(*PurePosixPath(name).parts)
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = stat.S_IMODE(path.stat().st_mode)
                if os.name == "nt" and name == BUNDLE + "/Contents/MacOS/SessionLock":
                    mode = 0o755
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive_file.writestr(info, path.read_bytes())
        with tempfile.TemporaryDirectory(prefix=".package-check-", dir=output.parent) as scratch:
            extracted = Path(scratch) / "extracted"
            extract_archive(output, extracted)
            require(scan(extracted) == before, "ZIP round-trip changed bytes/layout")
            if os.name != "nt":
                for name in before:
                    require(stat.S_IMODE((root / name).stat().st_mode)
                            == stat.S_IMODE((extracted / name).stat().st_mode),
                            "ZIP round-trip changed permissions")
        require(scan(root) == before, "Input changed during archiving")
        sidecar.write_bytes((file_record(output)["sha256"] + "  " + output.name + "\n").encode("utf-8"))
    except Exception:
        output.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    staging = commands.add_parser("stage")
    staging.add_argument("--target", choices=TARGETS, required=True)
    staging.add_argument("--output", type=Path, required=True)
    staging.add_argument("--compiler", required=True, help="Compiler identity and version from build job")
    merging = commands.add_parser("combine")
    merging.add_argument("--mac", type=Path, required=True)
    merging.add_argument("--windows", type=Path, required=True)
    merging.add_argument("--output", type=Path, required=True)
    checking = commands.add_parser("verify")
    checking.add_argument("--root", type=Path, required=True)
    artifact_checking = commands.add_parser(
        "verify-artifact", help="Strictly verify a staged artifact before native test execution")
    artifact_checking.add_argument("--root", type=Path, required=True)
    finalizing = commands.add_parser(
        "refresh", help="Record signed bytes after caller-enforced signature/notarization gates")
    finalizing.add_argument("--root", type=Path, required=True)
    finalizing.add_argument("--signing", choices=(SIGNED,), required=True,
                            help="Caller declaration, not independently authenticated here")
    zipping = commands.add_parser("archive")
    zipping.add_argument("--root", type=Path, required=True)
    zipping.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
            stage(repo, args.target, args.output, args.compiler)
        elif args.command == "combine":
            combine(args.mac, args.windows, args.output)
        elif args.command == "verify":
            verify(args.root)
        elif args.command == "verify-artifact":
            verify_artifact(args.root)
        elif args.command == "refresh":
            refresh(args.root, args.signing)
        else:
            archive(args.root, args.output)
    except (PackageError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        parser.exit(1, "package: " + str(exc) + "\n")
    print(args.command + ": OK")


if __name__ == "__main__":
    main()
