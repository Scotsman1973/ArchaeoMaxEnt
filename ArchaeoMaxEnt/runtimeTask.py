"""Background work in QGIS's task manager: model runs, runtime installs and downloads.

Keeps QGIS responsive and gives the user a progress bar and a cancel button.
Works with QGIS 3 (Qt5) and QGIS 4 (Qt6).
"""
from __future__ import annotations

from pathlib import Path

from qgis.core import QgsBlockingNetworkRequest, QgsFeedback, QgsTask
from qgis.PyQt.QtCore import QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest

# Enum holders differ between QGIS 3 (unscoped) and QGIS 4 (scoped); resolve once here.
TASK_FLAGS = getattr(QgsTask, "Flag", QgsTask)
NETWORK_ERRORS = getattr(QgsBlockingNetworkRequest, "ErrorCode", QgsBlockingNetworkRequest)
PROGRESS_SCALE = 100


class RuntimeTask(QgsTask):
    """Run work(task) in the background and call onDone(result, error, cancelled) afterwards.

    work receives the task, so it can call task.report(event) with 'progress'
    and 'log' events and check task.isCanceled(). onDone runs in the main
    thread, where it is safe to update the dialog.
    """

    logged = pyqtSignal(str)

    def __init__(self, description, work, onDone):
        """Prepare the task; QGIS starts it when it is added to the task manager."""
        super().__init__(description, TASK_FLAGS.CanCancel)
        self.work = work
        self.onDone = onDone
        self.result = None
        self.error = None
        self.feedback = QgsFeedback()

    def cancel(self):
        """Cancel the task, including any download in progress."""
        self.feedback.cancel()
        super().cancel()

    def run(self):
        """Do the work in a background thread; never touch the dialog here."""
        try:
            self.result = self.work(self)
            return True
        except Exception as exc:
            self.error = exc
            return False

    def report(self, event: dict):
        """Turn a runtime event into a progress update and/or a log line."""
        if "value" in event and event.get("type") == "progress":
            self.setProgress(float(event["value"]) * PROGRESS_SCALE)
        if event.get("message"):
            self.logged.emit(str(event["message"]))

    def finished(self, succeeded):
        """Hand the outcome to the dialog (main thread)."""
        self.onDone(self.result, self.error, self.isCanceled())


def downloadFile(task: RuntimeTask, url: str, target: Path) -> Path:
    """Download url to target using QGIS's network settings (proxy, authentication).

    Writes to a temporary name first, so a failed download never looks complete.
    """
    request = QgsBlockingNetworkRequest()
    request.downloadProgress.connect(
        lambda received, total: task.setProgress(received / total * PROGRESS_SCALE if total > 0 else 0))
    task.logged.emit(f"Downloading {url}")
    error = request.get(QNetworkRequest(QUrl(url)), True, task.feedback)
    if task.isCanceled():
        raise RuntimeError("Download cancelled")
    if error != NETWORK_ERRORS.NoError:
        raise RuntimeError(f"Download failed: {request.errorMessage()}")
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(bytes(request.reply().content()))
    partial.replace(target)
    return target
