#!/usr/bin/env python3
"""Fail-closed tool4d host gate with a no-plugin shutdown control."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def invoke(executable, project, method, label, logs):
    command = [str(executable), "--dataless", f"--startup-method={method}",
               f"--project={project}"]
    try:
        process = subprocess.run(command, capture_output=True, timeout=180)
        code = process.returncode
        stdout, stderr = process.stdout, process.stderr
        timed_out = False
    except subprocess.TimeoutExpired as error:
        code = None
        stdout, stderr = error.stdout or b"", error.stderr or b""
        timed_out = True
    (logs / f"{label}.stdout.log").write_bytes(stdout)
    (logs / f"{label}.stderr.log").write_bytes(stderr)
    text = stdout.decode("utf-8", errors="replace")
    marker = "BASELINE PASS" if label == "baseline" else "PASS"
    result = {
        "command": command, "exit": code, "timedOut": timed_out,
        "assertionsPassed": marker in text.splitlines(),
        "passed": code == 0 and marker in text.splitlines() and not timed_out,
    }
    print(f"{label}: {json.dumps(result)}")
    print(text)
    print(stderr.decode("utf-8", errors="replace"), file=sys.stderr)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--project-root", type=Path,
                        default=Path("SessionLock/SessionLock-test"))
    args = parser.parse_args()
    distribution = args.distribution.resolve()
    executable = distribution / (
        "tool4d/tool4d.exe" if os.name == "nt"
        else "tool4d.app/Contents/MacOS/tool4d")
    if not executable.is_file():
        parser.error(f"Missing tool4d executable: {executable}")
    logs = args.logs.resolve()
    logs.mkdir(parents=True, exist_ok=True)
    project_root = args.project_root.resolve()
    with tempfile.TemporaryDirectory(prefix="sessionlock-baseline-") as directory:
        baseline = Path(directory)
        shutil.copytree(project_root / "Project", baseline / "Project",
                        ignore=shutil.ignore_patterns("DerivedData"))
        for folder in ("Settings", "Resources"):
            if (project_root / folder).exists():
                shutil.copytree(project_root / folder, baseline / folder)
        method = baseline / "Project/Sources/Methods/ci_baseline.4dm"
        method.write_text(
            '//%attributes = {"invisible":true}\n'
            'LOG EVENT(Into system standard outputs; "BASELINE PASS"; Information message)\n',
            encoding="utf-8")
        control = invoke(executable, baseline / "Project/SessionLock.4DProject",
                         "ci_baseline", "baseline", logs)
        plugin = invoke(executable, project_root / "Project/SessionLock.4DProject",
                        "test_all", "plugin", logs)
    result = {"baseline": control, "plugin": plugin,
              "passed": control["passed"] and plugin["passed"]}
    (logs / "result.json").write_text(json.dumps(result, indent=2) + "\n",
                                      encoding="utf-8")
    if not result["passed"]:
        raise SystemExit("Full tool4d gate failed; inspect baseline and plugin diagnostics")


if __name__ == "__main__":
    main()
