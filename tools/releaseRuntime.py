"""Check a full set of runtime archives and write the plugin's checksums.json.

Run after the GitHub Actions workflow has built every platform's archive
into one folder (default dist/):

    python tools/releaseRuntime.py dist

Fails unless there is exactly one archive per platform in runtime.json, each
built from this plugin's current lock file. Writes
ArchaeoMaxEnt/runtime/checksums.json (ship it with the plugin, so installs
are checked against it) and prints the release tag the archives belong under.
"""
import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_DIR / "ArchaeoMaxEnt"
sys.path.insert(0, str(REPO_DIR))

from ArchaeoMaxEnt import runtimeEnv  # noqa: E402


def main() -> int:
    """Verify the archive set and write checksums.json."""
    distDir = Path(sys.argv[1] if len(sys.argv) > 1 else REPO_DIR / "dist")
    platforms = runtimeEnv.runtimeSpec(PLUGIN_DIR)["platforms"]
    checksums, missing = {}, []
    for key in platforms:
        expected = runtimeEnv.expectedLock(PLUGIN_DIR, key)
        archive = distDir / expected["bundleName"]
        if not archive.is_file():
            missing.append(expected["bundleName"])
            continue
        checksums[archive.name] = runtimeEnv.fileSha256(archive)
    if missing:
        print("Missing (or built from a different lock file):\n  " + "\n  ".join(missing))
        return 1
    target = PLUGIN_DIR / runtimeEnv.RUNTIME_SPEC_FOLDER / runtimeEnv.CHECKSUMS_FILE
    target.write_text(json.dumps(checksums, indent=2) + "\n", encoding="utf-8")
    tag = f"runtime-{runtimeEnv.lockSetId(PLUGIN_DIR)}"
    print(f"Wrote {target.relative_to(REPO_DIR)}\nRelease tag: {tag}")
    githubOutput = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if githubOutput:
        with open(githubOutput, "a", encoding="utf-8") as stream:
            stream.write(f"tag={tag}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
