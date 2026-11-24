"""Build the modelling runtime .zip for the computer this runs on.

Used in three ways, always with the same steps:
  - by runtime/buildRuntime.sh (macOS, Linux) and runtime/buildRuntime.ps1
    (Windows), which a user runs, or the plugin runs from the Project tab;
  - by tools/buildRuntime.py on a developer's computer;
  - by the GitHub Actions workflow, through the same two scripts.

Steps: fetch the standalone Python named in runtime.json with uv, copy it,
install this platform's lock file into the copy (wheels only, hashes
enforced), start the worker in it to check every package imports, then
write manifest.json and pack everything as <dist>/<archive>.zip with a
.sha256 file. Finally the zip is installed into a scratch folder exactly as
the plugin would install it, as a last check.

Run with any Python 3.9 or newer (standard library only), e.g.
    uv run --no-project --python 3.12 python -I runtimeBuilder.py --uv uv --dist-dir dist

Nothing outside --build-dir and --dist-dir is changed: uv's downloads,
cache and Python installs all stay in --build-dir, and uv is told not to
add Python to PATH or to the Windows registry.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
if __package__:
    from . import runtimeEnv
    from .runtimeClient import RuntimeClient
else:  # Run as a script: import the plugin package by its folder name, as modelWorker.py does.
    sys.path.insert(0, str(PLUGIN_DIR.parent))
    runtimeEnv = importlib.import_module(f"{PLUGIN_DIR.name}.runtimeEnv")
    RuntimeClient = importlib.import_module(f"{PLUGIN_DIR.name}.runtimeClient").RuntimeClient

LOCKED_PACKAGE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", re.MULTILINE)
# Removed from the runtime to save space: test suites and tools never used by the
# worker. Python recreates bytecode caches on first use, so none are shipped.
PRUNED_FOLDER_NAMES = {"tests", "test", "__pycache__", "idlelib", "tkinter", "turtledemo", "ensurepip"}
PRUNED_FILE_SUFFIXES = (".pyc", ".a")
ZIP_COMPRESSION_LEVEL = 6
ZIP_EARLIEST_TIME = time.mktime((1980, 1, 2, 0, 0, 0, 0, 0, -1))
UNIX_SYSTEM = 3  # ZipInfo.create_system value meaning "permissions are Unix mode bits".
RESULT_PREFIX = "RUNTIME_ZIP="  # The plugin reads the finished zip's path from this output line.


def run(command, **kwargs):
    """Run a command, echoing it, and stop on failure."""
    print("+", " ".join(str(part) for part in command), flush=True)
    return subprocess.run([str(part) for part in command], check=True, **kwargs)


def uvEnvironment(buildDir: Path) -> dict:
    """Return an environment that keeps everything uv does inside buildDir."""
    return {
        **os.environ,
        "UV_PYTHON_INSTALL_DIR": str(buildDir / "pythons"),
        "UV_CACHE_DIR": str(buildDir / "uv-cache"),
        "UV_PYTHON_INSTALL_BIN": "0",       # do not put python3.12 on the user's PATH
        "UV_PYTHON_INSTALL_REGISTRY": "0",  # do not register Python in the Windows registry
        "UV_NO_CONFIG": "1",                # ignore any uv.toml the user may have
        "UV_PYTHON_DOWNLOADS": "automatic",
    }


def standalonePython(uv: str, version: str, buildDir: Path) -> Path:
    """Install a relocatable standalone CPython with uv and return its installation folder."""
    environment = uvEnvironment(buildDir)
    run([uv, "python", "install", version], env=environment)
    found = run([uv, "python", "find", "--managed-python", version], env=environment,
                capture_output=True, text=True).stdout.strip()
    key = runtimeEnv.platformKey()
    relativeParts = runtimeEnv.PYTHON_PATHS.get(key, runtimeEnv.DEFAULT_PYTHON_PATH)[1:]
    root = Path(found)
    for _ in relativeParts:
        root = root.parent
    return root


def normalise(name: str) -> str:
    """Normalise a package name for comparison (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def checkVersions(lockFile: Path, installed: dict):
    """Fail unless the reported key packages have exactly the locked versions."""
    locked = {normalise(name): version for name, version in LOCKED_PACKAGE.findall(lockFile.read_text())}
    wrong = {name: (version, locked.get(normalise(name))) for name, version in installed.items()
             if version and locked.get(normalise(name)) not in (None, version)}
    if wrong:
        raise SystemExit(f"Installed versions differ from the lock file: {wrong}")


def pruneRuntime(pythonRoot: Path):
    """Delete test suites, caches and unused tools from the runtime copy."""
    sitePackages = [path for path in pythonRoot.rglob("site-packages") if path.is_dir()]
    standardLibrary = [path.parent for path in sitePackages]
    removed = 0
    for top in standardLibrary:
        for folder in sorted(top.rglob("*"), key=lambda path: len(path.parts)):
            if folder.is_dir() and folder.name in PRUNED_FOLDER_NAMES and folder.exists():
                removed += sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())
                shutil.rmtree(folder)
    for item in pythonRoot.rglob("*"):
        if item.is_file() and not item.is_symlink() and item.suffix in PRUNED_FILE_SUFFIXES:
            removed += item.stat().st_size
            item.unlink()
    print(f"Pruned {removed / 1e6:.0f} MB")


def zipEntry(path: Path, name: str) -> zipfile.ZipInfo:
    """Describe one file, folder or symbolic link for the zip, keeping its Unix permissions.

    Owner names are never stored, so archives do not reveal the build computer's user.
    """
    details = path.lstat()
    info = zipfile.ZipInfo(name, time.localtime(max(details.st_mtime, ZIP_EARLIEST_TIME))[:6])
    info.create_system = UNIX_SYSTEM
    info.external_attr = (details.st_mode & 0xFFFF) << 16
    info.compress_type = zipfile.ZIP_DEFLATED  # a ZipInfo's own setting wins over the archive's
    if stat.S_ISDIR(details.st_mode):
        info.external_attr |= 0x10  # MS-DOS folder flag, so Windows tools see a folder too
    return info


def writeZip(archivePath: Path, items):
    """Pack (path, name in archive) pairs, and everything below any folder, into a zip.

    Symbolic links (macOS and Linux Pythons have a few) are stored as links,
    as Unix zip tools do; the plugin's installer recreates them.
    """
    partial = archivePath.with_name(archivePath.name + ".partial")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=ZIP_COMPRESSION_LEVEL) as archive:
        for top, topName in items:
            paths = [top] + (sorted(top.rglob("*")) if top.is_dir() and not top.is_symlink() else [])
            for path in paths:
                name = topName if path == top else f"{topName}/{path.relative_to(top).as_posix()}"
                if path.is_symlink():
                    archive.writestr(zipEntry(path, name), os.readlink(path), zipfile.ZIP_STORED)
                elif path.is_dir():
                    archive.writestr(zipEntry(path, name + "/"), b"")
                else:
                    with open(path, "rb") as source, archive.open(zipEntry(path, name), "w") as target:
                        shutil.copyfileobj(source, target, 1 << 20)
    partial.replace(archivePath)


def build(uv: str, buildDir: Path, distDir: Path, wantedPlatform: str | None = None) -> Path:
    """Build, check and pack this computer's runtime; return the zip's path."""
    spec = runtimeEnv.runtimeSpec(PLUGIN_DIR)
    expected = runtimeEnv.expectedLock(PLUGIN_DIR)
    key = expected["platform"]
    if wantedPlatform and wantedPlatform != key:
        raise SystemExit(f"Asked to build for {wantedPlatform}, but this Python runs as {key}")
    print(f"Building runtime {expected['lockId']} for {key} (Python {spec['python']})", flush=True)
    staging = buildDir / key
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    sourceRoot = standalonePython(uv, spec["python"], buildDir)
    shutil.copytree(sourceRoot, staging / "python", symlinks=True)
    for marker in (staging / "python").rglob("EXTERNALLY-MANAGED"):
        marker.unlink()  # The copy is ours to install into.
    executable = runtimeEnv.pythonPath(staging, key)

    lockFile = runtimeEnv.lockPath(PLUGIN_DIR, key)
    environment = {**uvEnvironment(buildDir), "MACOSX_DEPLOYMENT_TARGET": spec["macosMinimum"]}
    run([uv, "pip", "sync", "--python", executable, "--require-hashes", "--only-binary", ":all:",
         "--link-mode", "copy", lockFile], env=environment)
    pruneRuntime(staging / "python")

    report = RuntimeClient(executable, PLUGIN_DIR, staging).call("ping")
    checkVersions(lockFile, report["packages"])
    print(json.dumps(report, indent=2), flush=True)
    shutil.rmtree(staging / "cache", ignore_errors=True)

    manifest = {
        "format": runtimeEnv.MANIFEST_FORMAT,
        "platform": key,
        "lockHash": expected["lockHash"],
        "lockId": expected["lockId"],
        "python": report["python"],
        "gdal": report["gdal"],
        "packages": report["packages"],
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "builtOn": platform.platform(),
    }
    (staging / runtimeEnv.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    distDir.mkdir(parents=True, exist_ok=True)
    archivePath = distDir / expected["bundleName"]
    print(f"Packing {archivePath.name} (this takes a few minutes)", flush=True)
    writeZip(archivePath, [(staging / runtimeEnv.MANIFEST_NAME, runtimeEnv.MANIFEST_NAME),
                           (staging / "python", "python")])
    digest = runtimeEnv.fileSha256(archivePath)
    archivePath.with_name(archivePath.name + ".sha256").write_text(f"{digest}  {archivePath.name}\n")
    print(f"Built {archivePath} ({archivePath.stat().st_size / 1e6:.0f} MB) sha256 {digest}", flush=True)

    # Install the finished archive exactly as a user would, and start the worker in it.
    testDir = buildDir / f"{key}-install-test"
    shutil.rmtree(testDir, ignore_errors=True)
    status = runtimeEnv.installRuntime(
        archivePath, testDir / "runtime", PLUGIN_DIR, requirePublished=False,
        verify=lambda executable, folder: RuntimeClient(executable, PLUGIN_DIR, folder).call("ping"))
    print(f"Install test: {status.message}", flush=True)
    shutil.rmtree(testDir, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    if not status.ready:
        raise SystemExit(f"Install test failed: {status.message}")
    return archivePath


def main(argv=None) -> int:
    """Parse the command line and build the runtime."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uv", default="uv", help="uv executable to use (default: uv on PATH)")
    parser.add_argument("--build-dir", type=Path, required=True, help="scratch folder (can be deleted afterwards)")
    parser.add_argument("--dist-dir", type=Path, required=True, help="folder for the finished .zip")
    parser.add_argument("--platform", help="stop unless building for this platform key, e.g. macos-arm64")
    options = parser.parse_args(argv)
    try:
        archivePath = build(options.uv, options.build_dir.resolve(), options.dist_dir.resolve(), options.platform)
    except runtimeEnv.RuntimeProblem as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)  # a plain message, not a Python traceback
        return 1
    print("Finished. Install this file from the plugin's Project tab (Install from file):")
    print(f"{RESULT_PREFIX}{archivePath}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
