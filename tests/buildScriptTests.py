"""The runtime build script, run the way the plugin's "Build on this computer" button runs it.

    python -I tests/buildScriptTests.py          (macOS or Linux; needs internet)

Checks that Cancel stops the script and everything it started, and that its
temporary folder is removed. (A full successful build is covered by
tests/dialogSmoke.py and by the GitHub Actions workflow.)
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from ArchaeoMaxEnt import runtimeEnv

results = []
def check(name, ok, detail=""):
    results.append(ok); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))

def buildFolders():
    return sorted(Path(tempfile.gettempdir()).glob(f"{runtimeEnv.BUILD_WORK_PREFIX}*"))

def buildProcesses():
    listing = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return [line for line in listing.splitlines() if "runtimeBuilder.py" in line or "buildRuntime.sh" in line]

lines = []
started = time.monotonic()
# Cancel once Python has started installing packages (well into the build).
cancelWhen = lambda: any("uv pip sync" in line for line in lines) or time.monotonic() - started > 120
output = Path(tempfile.mkdtemp())
try:
    runtimeEnv.runBuildScript(REPO / "ArchaeoMaxEnt", output, onLine=lines.append, isCancelled=cancelWhen)
    check("cancel stops the build", False, "the build finished instead")
except runtimeEnv.RuntimeProblem as exc:
    check("cancel stops the build", "cancelled" in str(exc), f"after {time.monotonic() - started:.0f}s")
time.sleep(1)
check("no build processes left running", not buildProcesses(), str(buildProcesses()))
check("temporary build folder removed", not buildFolders(), str(buildFolders()))
check("no half-finished zip left", not list(output.glob("*.zip")))
check("script messages were passed on", any("uv checksum OK" in line for line in lines))
print(f"\n{sum(results)}/{len(results)} passed")
