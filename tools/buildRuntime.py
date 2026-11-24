"""Build the modelling runtime .zip for the computer this script runs on (developer shortcut).

Run from the repository root:

    uv run --no-project python tools/buildRuntime.py

The work is done by ArchaeoMaxEnt/runtimeBuilder.py, the same code the
user-facing scripts ArchaeoMaxEnt/runtime/buildRuntime.sh and .ps1 (and the
GitHub Actions workflow) use. Output: dist/<archive>.zip and .zip.sha256;
scratch files go to build/.
"""
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

from ArchaeoMaxEnt import runtimeBuilder  # noqa: E402  (standard library only)

if __name__ == "__main__":
    arguments = sys.argv[1:]
    if "--build-dir" not in arguments:
        arguments += ["--build-dir", str(REPO_DIR / "build")]
    if "--dist-dir" not in arguments:
        arguments += ["--dist-dir", str(REPO_DIR / "dist")]
    sys.exit(runtimeBuilder.main(arguments))
