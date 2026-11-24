"""Run one job in the modelling runtime as a separate process.

The job goes to modelWorker.py as JSON on standard input; the worker answers
with one JSON object per line on standard output: 'log' and 'progress'
events while it works, then a single 'result' or 'error'.

Running in a separate process keeps the runtime's own GDAL, PROJ and numpy
apart from QGIS's, and lets a long model run be cancelled cleanly.

Standard library only: this module runs inside QGIS.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path

PROTOCOL_VERSION = 1
WORKER_SCRIPT = "modelWorker.py"
POLL_SECONDS = 0.2
STDERR_LINES_KEPT = 60
# Environment variables set by QGIS (or the system) that would point the
# runtime at QGIS's own Python, GDAL or PROJ instead of its bundled copies.
DROPPED_ENV_PREFIXES = ("PYTHON", "GDAL_", "PROJ_", "CPL_", "OGR_", "OSGEO", "QGIS",
                        "QT_", "CONDA", "VIRTUAL_ENV", "LD_LIBRARY_PATH", "DYLD_")


class WorkerError(Exception):
    """The runtime reported an error, or stopped without answering."""

    def __init__(self, message, details=""):
        super().__init__(message)
        self.details = details


class WorkerCancelled(Exception):
    """The job was cancelled by the user."""


def childEnvironment(runtimeDir: Path) -> dict:
    """Return a clean environment for the runtime process."""
    environment = {name: value for name, value in os.environ.items()
                   if not name.upper().startswith(DROPPED_ENV_PREFIXES)}
    cacheDir = Path(runtimeDir) / "cache"
    environment.update({
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(cacheDir / "matplotlib"),
        "PYTHONIOENCODING": "utf-8",
    })
    return environment


class RuntimeClient:
    """Sends jobs to the runtime's Python and collects their events and results."""

    def __init__(self, pythonPath: Path, pluginDir: Path, runtimeDir: Path):
        """Remember which Python, worker script and runtime folder to use."""
        self.pythonPath = Path(pythonPath)
        self.workerScript = Path(pluginDir) / WORKER_SCRIPT
        self.runtimeDir = Path(runtimeDir)

    def command(self) -> list:
        """Return the command line that starts the worker (isolated from user site-packages)."""
        return [str(self.pythonPath), "-I", "-X", "utf8", str(self.workerScript)]

    def call(self, operation: str, args: dict | None = None, onEvent=None, isCancelled=None):
        """Run one operation and return its result.

        onEvent(event) receives each 'log' and 'progress' event as it arrives.
        isCancelled() is polled; when it returns True the worker is stopped
        and WorkerCancelled is raised.
        """
        process = subprocess.Popen(
            self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=childEnvironment(self.runtimeDir), cwd=str(self.runtimeDir),
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        events = queue.Queue()
        stderrTail = deque(maxlen=STDERR_LINES_KEPT)

        def readStdout():
            """Pass each output line to the main loop, then signal the end."""
            for line in process.stdout:
                events.put(line)
            events.put(None)

        def readStderr():
            """Keep the last lines of the worker's diagnostic output."""
            for line in process.stderr:
                stderrTail.append(line.rstrip())

        threads = [threading.Thread(target=reader, daemon=True) for reader in (readStdout, readStderr)]
        for thread in threads:
            thread.start()
        job = {"protocol": PROTOCOL_VERSION, "operation": operation, "args": args or {}}
        try:
            process.stdin.write(json.dumps(job))
            process.stdin.close()
        except OSError:
            pass  # The worker exited early; its error is reported below.

        outcome = {}
        handlers = {
            "result": lambda event: outcome.update(result=event.get("value")),
            "error": lambda event: outcome.update(error=event),
        }
        try:
            while True:
                if isCancelled and isCancelled():
                    raise WorkerCancelled(f"{operation} cancelled")
                try:
                    line = events.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    continue
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    stderrTail.append(line.rstrip())
                    continue
                handler = handlers.get(event.get("type"))
                if handler:
                    handler(event)
                elif onEvent:
                    onEvent(event)
        except BaseException:
            process.kill()
            process.wait()
            raise
        exitCode = process.wait()
        for thread in threads:
            thread.join(timeout=1)

        if "error" in outcome:
            error = outcome["error"]
            raise WorkerError(error.get("message", "Unknown runtime error"), error.get("traceback", ""))
        if "result" not in outcome:
            raise WorkerError(f"Modelling runtime stopped unexpectedly (exit code {exitCode})",
                              "\n".join(stderrTail))
        return outcome["result"]
