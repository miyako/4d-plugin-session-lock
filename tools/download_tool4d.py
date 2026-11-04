#!/usr/bin/env python3
"""Download the matching tool4d runtime and record the exact archive checksum."""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    system, machine = platform.system(), platform.machine().lower()
    if system == "Windows" and machine in ("amd64", "x86_64"):
        folder, archive = "win", "tool4d_win.tar.xz"
    elif system == "Darwin" and machine in ("arm64", "x86_64"):
        folder, archive = "mac", f"tool4d_{machine}.tar.xz"
    else:
        parser.error(f"Unsupported host: {system} {machine}")
    url = f"https://resources-download.4d.com/release/21.x/21.1/latest/{folder}/{archive}"
    args.output.mkdir(parents=True, exist_ok=True)
    download = args.output / archive
    subprocess.run(["curl", "--fail", "--location", "--retry", "3",
                    "--connect-timeout", "30", "--max-time", "300", url,
                    "--output", str(download)], check=True)
    metadata = {"url": url, "sha256": hashlib.sha256(download.read_bytes()).hexdigest(),
                "version": "21.1", "host": system, "architecture": machine}
    (args.output / "download.json").write_text(json.dumps(metadata, indent=2) + "\n",
                                              encoding="utf-8")
    subprocess.run(["tar", "-xJf", str(download), "-C", str(args.output)], check=True)


if __name__ == "__main__":
    main()
