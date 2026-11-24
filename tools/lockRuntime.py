"""Regenerate the runtime lock files, one per platform, from runtime/requirements.in.

Run from the repository root after editing requirements.in or runtime.json:

    uv run --no-project python tools/lockRuntime.py

Each lock pins every package to an exact version with SHA-256 hashes, using
binary wheels only, for the Python version and minimum macOS version set in
runtime.json. A platform where some package has no suitable wheel fails here,
on the developer's machine, instead of on a user's computer.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "ArchaeoMaxEnt"
RUNTIME_DIR = PLUGIN_DIR / "runtime"


def main() -> int:
    """Compile and check one lock file per platform listed in runtime.json."""
    spec = json.loads((RUNTIME_DIR / "runtime.json").read_text(encoding="utf-8"))
    pythonVersion = ".".join(spec["python"].split(".")[:2])
    environment = {**os.environ, "MACOSX_DEPLOYMENT_TARGET": spec["macosMinimum"]}
    locksDir = RUNTIME_DIR / "locks"
    locksDir.mkdir(exist_ok=True)
    for key, target in spec["platforms"].items():
        lock = locksDir / f"{key}.lock"
        common = ["--python-platform", target, "--python-version", pythonVersion, "--only-binary", ":all:"]
        # Run inside runtime/ with relative paths, so the lock's comments are the same on every computer.
        subprocess.run(["uv", "pip", "compile", "requirements.in", *common, "--generate-hashes", "--no-header",
                        "--quiet", "-o", str(lock.relative_to(RUNTIME_DIR))],
                       check=True, env=environment, cwd=RUNTIME_DIR)
        # Prove the lock installs on that platform from wheels, with hashes enforced.
        subprocess.run(["uv", "pip", "install", "--dry-run", "--quiet", "--target", str(locksDir / ".dry-run"),
                        *common, "--require-hashes", "-r", str(lock)], check=True, env=environment)
        print(f"{key}: {lock.relative_to(PLUGIN_DIR.parent)}")
    shutil.rmtree(locksDir / ".dry-run", ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
