"""Find, check and install the modelling runtime.

The runtime is a standalone Python with every modelling package already
installed at exactly the versions in runtime/locks/<platform>.lock. It is
shipped as one .zip archive per platform (older .tar.xz archives still
install), so installing it needs no internet,
no pip and no compiler: the archive is checked and unpacked, nothing more.

Standard library only: this module runs inside QGIS.

Platform differences are kept in the two tables below. Compatibility is a
single comparison: the runtime is usable when the lock-file hash recorded in
its manifest equals the hash of the lock file shipped with this plugin.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path

# (sys.platform, machine) -> platform key used for lock files and runtime archives.
PLATFORM_KEYS = {
    ("win32", "amd64"): "windows-x86_64",
    ("win32", "x86_64"): "windows-x86_64",
    ("darwin", "arm64"): "macos-arm64",
    ("darwin", "x86_64"): "macos-x86_64",
    ("linux", "x86_64"): "linux-x86_64",
    ("linux", "amd64"): "linux-x86_64",
    ("linux", "aarch64"): "linux-aarch64",
    ("linux", "arm64"): "linux-aarch64",
}
# Where the Python executable sits inside an unpacked runtime.
PYTHON_PATHS = {"windows-x86_64": ("python", "python.exe")}
DEFAULT_PYTHON_PATH = ("python", "bin", "python3")

RUNTIME_SPEC_FOLDER = "runtime"
LOCKS_FOLDER = "locks"
RUNTIME_SPEC_FILE = "runtime.json"
CHECKSUMS_FILE = "checksums.json"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = 1
LOCK_ID_LENGTH = 12
BUNDLE_NAME = "archaeoMaxEnt-runtime-{lockId}-{platform}.zip"
SMOKE_TEST_SECONDS = 120
# How each platform key is described to the researcher.
PLATFORM_LABELS = {
    "windows-x86_64": "Windows",
    "macos-arm64": "a Mac with an Apple chip (M1 or later)",
    "macos-x86_64": "a Mac with an Intel chip",
    "linux-x86_64": "Linux on an ordinary Intel or AMD computer (x86_64)",
    "linux-aarch64": "Linux on an Arm computer (aarch64, e.g. a Raspberry Pi)",
}
DOWNLOAD_OWNER_PLACEHOLDER = "OWNER"  # in runtime.json until the GitHub account is filled in
# Scripts that download everything and build the runtime .zip on the user's own computer.
WINDOWS_BUILD_SCRIPT = "buildRuntime.ps1"
UNIX_BUILD_SCRIPT = "buildRuntime.sh"
BUILD_WORK_PREFIX = "archaeoMaxEnt-build"  # the scripts' temporary folders start with this
BUILD_LINES_KEPT = 40
BUILD_POLL_SECONDS = 0.2
BUILD_STOP_SECONDS = 10
# Variables set by QGIS that would make the build's own Python load QGIS's libraries instead.
BUILD_DROPPED_ENV_PREFIXES = ("PYTHON", "GDAL_", "PROJ_", "CPL_", "OGR_", "OSGEO", "QGIS", "QT_",
                              "CONDA", "VIRTUAL_ENV", "LD_LIBRARY_PATH", "DYLD_")


class RuntimeProblem(Exception):
    """The runtime is missing, damaged, or does not match this plugin."""


@dataclass
class RuntimeStatus:
    """Whether the installed runtime can be used, and why not if it cannot."""

    ready: bool
    message: str
    pythonPath: Path | None = None
    manifest: dict | None = None


def platformKey() -> str:
    """Return this computer's platform key, e.g. 'windows-x86_64'."""
    system = "linux" if sys.platform.startswith("linux") else sys.platform
    machine = platform.machine().lower()
    key = PLATFORM_KEYS.get((system, machine))
    if key is None:
        raise RuntimeProblem(f"No modelling runtime is built for this computer ({system}, {machine})")
    return key


def pythonPath(runtimeDir: Path, key: str) -> Path:
    """Return the path of the runtime's Python executable."""
    return Path(runtimeDir).joinpath(*PYTHON_PATHS.get(key, DEFAULT_PYTHON_PATH))


def lockPath(pluginDir: Path, key: str) -> Path:
    """Return the lock file this plugin requires for a platform."""
    return Path(pluginDir) / RUNTIME_SPEC_FOLDER / LOCKS_FOLDER / f"{key}.lock"


def lockHash(path: Path) -> str:
    """Return the SHA-256 of a lock file's requirements.

    Only the pinned packages and their hashes count: comments, blank lines,
    indentation and Windows/Unix line endings are ignored, so regenerating a
    lock from another folder or checking it out on Windows keeps its identity.
    """
    lines = (line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines())
    content = "\n".join(line for line in lines if line and not line.startswith("#"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def lockSetId(pluginDir: Path) -> str:
    """Return a short id covering every platform's lock file (names a release holding all archives)."""
    digest = hashlib.sha256()
    for path in sorted((Path(pluginDir) / RUNTIME_SPEC_FOLDER / LOCKS_FOLDER).glob("*.lock")):
        digest.update(f"{path.stem}:{lockHash(path)}\n".encode("utf-8"))
    return digest.hexdigest()[:LOCK_ID_LENGTH]


def expectedLock(pluginDir: Path, key: str | None = None) -> dict:
    """Return the platform key, lock hash, short ids and archive name this plugin expects."""
    key = key or platformKey()
    path = lockPath(pluginDir, key)
    if not path.is_file():
        raise RuntimeProblem(f"This plugin has no runtime lock file for {key}")
    fullHash = lockHash(path)
    lockId = fullHash[:LOCK_ID_LENGTH]
    return {"platform": key, "lockHash": fullHash, "lockId": lockId, "setId": lockSetId(pluginDir),
            "bundleName": BUNDLE_NAME.format(lockId=lockId, platform=key)}


def runtimeSpec(pluginDir: Path) -> dict:
    """Return the runtime settings shipped with the plugin (Python version, download address)."""
    return json.loads((Path(pluginDir) / RUNTIME_SPEC_FOLDER / RUNTIME_SPEC_FILE).read_text(encoding="utf-8"))


def downloadConfigured(pluginDir: Path) -> bool:
    """True once runtime.json's download address names a real GitHub account (not the OWNER placeholder)."""
    return f"/{DOWNLOAD_OWNER_PLACEHOLDER}/" not in runtimeSpec(pluginDir)["downloadUrl"]


def downloadUrl(pluginDir: Path) -> str:
    """Return the address of this computer's runtime archive."""
    return runtimeSpec(pluginDir)["downloadUrl"].format(**expectedLock(pluginDir))


def readManifest(path: Path) -> dict:
    """Read a runtime manifest, raising RuntimeProblem if it is missing or unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeProblem("No modelling runtime installed") from None
    except (OSError, ValueError) as exc:
        raise RuntimeProblem(f"Runtime manifest cannot be read: {exc}") from exc


def checkManifest(manifest: dict, expected: dict):
    """Raise RuntimeProblem unless a manifest matches this computer and this plugin's lock file."""
    if manifest.get("format") != MANIFEST_FORMAT:
        raise RuntimeProblem(f"Unsupported runtime format {manifest.get('format')!r}")
    if manifest.get("platform") != expected["platform"]:
        theirs, ours = manifest.get("platform"), expected["platform"]
        raise RuntimeProblem(f"This runtime is for {PLATFORM_LABELS.get(theirs, theirs)}, but this computer is "
                             f"{PLATFORM_LABELS.get(ours, ours)}. Choose the file {expected['bundleName']} instead.")
    if manifest.get("lockHash") != expected["lockHash"]:
        raise RuntimeProblem(f"Runtime {str(manifest.get('lockHash'))[:LOCK_ID_LENGTH]} does not match this "
                             f"plugin version (needs {expected['lockId']}); install {expected['bundleName']}")


def runtimeStatus(runtimeDir: Path, pluginDir: Path) -> RuntimeStatus:
    """Report whether the installed runtime matches this computer and this plugin version."""
    try:
        expected = expectedLock(pluginDir)
        manifest = readManifest(Path(runtimeDir) / MANIFEST_NAME)
        checkManifest(manifest, expected)
        executable = pythonPath(runtimeDir, expected["platform"])
        if not executable.is_file():
            raise RuntimeProblem("Runtime is incomplete (Python executable missing); reinstall it")
    except RuntimeProblem as exc:
        return RuntimeStatus(False, str(exc))
    message = f"Runtime {expected['lockId']} ready (Python {manifest.get('python', '?')}, {expected['platform']})"
    return RuntimeStatus(True, message, executable, manifest)


def fileSha256(path: Path, blockSize: int = 1 << 20) -> str:
    """Return the SHA-256 of a file, read in blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(blockSize), b""):
            digest.update(block)
    return digest.hexdigest()


def publishedChecksums(pluginDir: Path) -> dict:
    """Return {archive name: SHA-256} published with this plugin version, if any."""
    path = Path(pluginDir) / RUNTIME_SPEC_FOLDER / CHECKSUMS_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def archiveIsPublished(archivePath: Path, pluginDir: Path):
    """True if the file is one of the runtimes published with this plugin version (its checksum is listed),
    False if it is not, None if this plugin version lists no checksums (e.g. a developer copy)."""
    published = publishedChecksums(pluginDir)
    if not published:
        return None
    return fileSha256(archivePath) in published.values()


def archiveManifest(archive) -> dict:
    """Read manifest.json from a runtime archive (zip or tar) without unpacking anything else."""
    try:
        if isinstance(archive, zipfile.ZipFile):
            data = archive.read(MANIFEST_NAME)
        else:
            stream = archive.extractfile(MANIFEST_NAME)
            data = stream.read() if stream is not None else None
    except KeyError:
        data = None
    if data is None:
        raise RuntimeProblem(notRuntimeMessage(archive))
    return json.loads(data.decode("utf-8"))


def notRuntimeMessage(archive) -> str:
    """Explain why an archive without manifest.json is not a runtime, naming what it is if it can tell."""
    names = archive.namelist() if isinstance(archive, zipfile.ZipFile) else archive.getnames()
    nextStep = ("To get the runtime, press 'Build on this computer…' on the Project tab, or choose a file "
                "named archaeoMaxEnt-runtime-….zip.")
    if any(name.endswith("tools/buildRuntime.py") for name in names):
        return f"This is the ArchaeoMaxEnt repository (source code), not a runtime. {nextStep}"
    if any(name.endswith("/metadata.txt") or name == "metadata.txt" for name in names):
        return ("This is the ArchaeoMaxEnt plugin, not a runtime: install it in QGIS with Plugins > "
                f"Manage and Install Plugins > Install from ZIP. {nextStep}")
    return f"Not an ArchaeoMaxEnt runtime archive (it has no manifest.json). {nextStep}"


def insideFolder(path: Path, root: Path) -> bool:
    """True when path (already resolved) is root or lies below it."""
    return path == root or root in path.parents


def extractZip(archive: zipfile.ZipFile, target: Path):
    """Unpack a runtime zip into target, refusing any entry that would land outside it.

    Python's zipfile does not restore Unix permissions or symbolic links, so
    this does both: the runtime's Python and its libraries must stay
    executable on macOS and Linux, and their few links must stay links.
    """
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    for info in archive.infolist():
        name = info.filename
        if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name or ".." in name.split("/"):
            raise RuntimeProblem(f"Unsafe entry in runtime archive: {name}")
        destination = (root / name).resolve()
        if not insideFolder(destination, root):
            raise RuntimeProblem(f"Unsafe entry in runtime archive: {name}")
        mode = info.external_attr >> 16 if info.create_system == 3 else 0
        if stat.S_ISLNK(mode):
            linkText = archive.read(info).decode("utf-8")
            if os.path.isabs(linkText) or not insideFolder((destination.parent / linkText).resolve(), root):
                raise RuntimeProblem(f"Unsafe link in runtime archive: {name} -> {linkText}")
            if sys.platform == "win32":
                raise RuntimeProblem(f"Runtime archive contains a link ({name}); it is not a Windows runtime")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(linkText, destination)
        elif info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(destination, "wb") as output:
                shutil.copyfileobj(source, output, 1 << 20)
            if mode and sys.platform != "win32":
                os.chmod(destination, stat.S_IMODE(mode) & 0o777)


def safeMembers(archive: tarfile.TarFile, target: Path):
    """Yield archive members, refusing any that would be written outside the target folder.

    Used only on Python versions without tarfile's built-in 'data' filter.
    """
    root = target.resolve()
    for member in archive.getmembers():
        destination = (root / member.name).resolve()
        linkTarget = (destination.parent / member.linkname).resolve() if member.issym() else destination
        insideRoot = all(path == root or root in path.parents for path in (destination, linkTarget))
        if member.isdev() or member.islnk() or not insideRoot:
            raise RuntimeProblem(f"Unsafe entry in runtime archive: {member.name}")
        yield member


def extractArchive(archive, target: Path):
    """Unpack a zip or tar archive into target, keeping file permissions and refusing unsafe paths."""
    if isinstance(archive, zipfile.ZipFile):
        extractZip(archive, target)
    elif hasattr(tarfile, "data_filter"):
        archive.extractall(target, filter="data")
    else:
        archive.extractall(target, members=safeMembers(archive, target))


def openArchive(archivePath: Path):
    """Open a runtime archive, recognised by its content rather than its name.

    New runtimes are .zip files; older ones are .tar.xz, and still install
    even if they were renamed.
    """
    if zipfile.is_zipfile(archivePath):
        return zipfile.ZipFile(archivePath)
    try:
        return tarfile.open(archivePath, "r:*")
    except tarfile.TarError as exc:
        raise RuntimeProblem(f"{Path(archivePath).name} is not a runtime archive (.zip expected)") from exc


def smokeTest(executable: Path):
    """Run the runtime's Python once to prove it starts on this computer."""
    completed = subprocess.run(
        [str(executable), "-I", "-c", "import sys; print(sys.version)"],
        capture_output=True, text=True, timeout=SMOKE_TEST_SECONDS,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeProblem(f"Runtime Python does not start: {completed.stderr.strip()[-500:]}")


def installRuntime(archivePath: Path, runtimeDir: Path, pluginDir: Path, verify=None,
                   requirePublished: bool = True) -> RuntimeStatus:
    """Check and install a runtime archive, replacing any previous runtime only on success.

    Works offline: the archive can come from a download, a USB stick or a
    shared drive. Checks, in order: that its manifest matches this computer and
    this plugin's lock file (so a file for another kind of computer gets a
    message saying so); if requirePublished, that it is the runtime published
    for this plugin version (its checksum, when this plugin version lists
    checksums); that it unpacks safely; and that its Python starts.
    requirePublished is False for a runtime built on this computer by the build
    script: its packages were checked against the lock file's checksums as they
    were downloaded, but its own checksum differs from the published file's,
    because no two builds produce byte-identical zips.
    verify(executable, runtimeFolder), if given, is called on the unpacked runtime
    before it replaces the old one, for further checks.
    """
    archivePath, runtimeDir = Path(archivePath), Path(runtimeDir)
    expected = expectedLock(pluginDir)
    with openArchive(archivePath) as archive:
        checkManifest(archiveManifest(archive), expected)
    publishedSha = publishedChecksums(pluginDir).get(expected["bundleName"]) if requirePublished else None
    if publishedSha and fileSha256(archivePath) != publishedSha:
        raise RuntimeProblem("This runtime file is not the one published for this plugin version: it was damaged "
                             "while downloading or copying, or it was altered. Download it again.")

    runtimeDir.parent.mkdir(parents=True, exist_ok=True)
    incoming = runtimeDir.with_name(f"{runtimeDir.name}.incoming-{os.getpid()}")
    previous = runtimeDir.with_name(f"{runtimeDir.name}.previous-{os.getpid()}")
    shutil.rmtree(incoming, ignore_errors=True)
    try:
        with openArchive(archivePath) as archive:
            checkManifest(archiveManifest(archive), expected)
            extractArchive(archive, incoming)
        executable = pythonPath(incoming, expected["platform"])
        smokeTest(executable)
        if verify:
            verify(executable, incoming)
        if runtimeDir.exists():
            runtimeDir.rename(previous)
        incoming.rename(runtimeDir)
    except Exception:
        shutil.rmtree(incoming, ignore_errors=True)
        if previous.exists() and not runtimeDir.exists():
            previous.rename(runtimeDir)
        raise
    shutil.rmtree(previous, ignore_errors=True)
    return runtimeStatus(runtimeDir, pluginDir)


# ----------------------------------------------------------------------
# Building the runtime on this computer (runtime/buildRuntime.sh or .ps1)
# ----------------------------------------------------------------------
def buildScriptCommand(pluginDir: Path, outputDir: Path, key: str | None = None) -> list:
    """Return the command that runs this computer's build script, writing the .zip to outputDir."""
    key = key or platformKey()
    scripts = Path(pluginDir) / RUNTIME_SPEC_FOLDER
    if key.startswith("windows"):
        systemRoot = os.environ.get("SystemRoot", r"C:\Windows")
        powershell = (shutil.which("powershell.exe")
                      or str(Path(systemRoot) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"))
        return [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(scripts / WINDOWS_BUILD_SCRIPT), "-OutputDir", str(outputDir), "-Platform", key]
    return [shutil.which("bash") or "/bin/bash", str(scripts / UNIX_BUILD_SCRIPT), str(outputDir), key]


def removeStaleBuildFolders():
    """Delete temporary folders left by an earlier build that was stopped part-way."""
    for folder in Path(tempfile.gettempdir()).glob(f"{BUILD_WORK_PREFIX}*"):
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)


def stopProcessTree(process: subprocess.Popen):
    """Stop a build script and everything it started (uv, Python)."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)  # the script cleans up its temporary folder
            process.wait(timeout=BUILD_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def runBuildScript(pluginDir: Path, outputDir: Path, onLine=None, isCancelled=None) -> Path:
    """Build this computer's runtime .zip in outputDir with the build script; return its path.

    Each line the script prints is passed to onLine(text). isCancelled() is
    polled; when it returns True the script is stopped and RuntimeProblem is
    raised. Needs an internet connection (PyPI and the Python download site).
    """
    expected = expectedLock(pluginDir)
    outputDir = Path(outputDir)
    outputDir.mkdir(parents=True, exist_ok=True)
    removeStaleBuildFolders()
    environment = {name: value for name, value in os.environ.items()
                   if not name.upper().startswith(BUILD_DROPPED_ENV_PREFIXES)}
    if sys.platform == "win32":
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        options = {"start_new_session": True}  # its own process group, so Cancel stops uv and Python too
    process = subprocess.Popen(
        buildScriptCommand(pluginDir, outputDir, expected["platform"]),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment,
        cwd=tempfile.gettempdir(), text=True, encoding="utf-8", errors="replace", bufsize=1, **options)
    lines = queue.Queue()
    tail = deque(maxlen=BUILD_LINES_KEPT)

    def readOutput():
        """Pass each output line to the loop below, then signal the end."""
        for line in process.stdout:
            lines.put(line.rstrip())
        lines.put(None)

    reader = threading.Thread(target=readOutput, daemon=True)
    reader.start()
    started = time.monotonic()
    try:
        while True:
            if isCancelled and isCancelled():
                raise RuntimeProblem("Runtime build cancelled")
            try:
                line = lines.get(timeout=BUILD_POLL_SECONDS)
            except queue.Empty:
                continue
            if line is None:
                break
            tail.append(line)
            if onLine and line:
                onLine(line)
    except BaseException:
        stopProcessTree(process)
        raise
    exitCode = process.wait()
    reader.join(timeout=1)
    archivePath = outputDir / expected["bundleName"]
    if exitCode != 0 or not archivePath.is_file():
        details = "\n".join(tail)
        raise RuntimeProblem(f"Building the runtime failed (exit code {exitCode}). Last messages:\n{details}")
    if onLine:
        onLine(f"Runtime built in {(time.monotonic() - started) / 60:.0f} min: {archivePath}")
    return archivePath
