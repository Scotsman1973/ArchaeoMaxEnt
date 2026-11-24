from PyQt5.QtCore import QObject, pyqtSignal

class QgsFeedback:
    def __init__(self): self.cancelled = False
    def cancel(self): self.cancelled = True

class QgsTask(QObject):
    class Flag: CanCancel = 1
    taskTerminated = pyqtSignal()
    def __init__(self, description, flags=0):
        super().__init__(); self.description = description; self._cancelled = False; self.progressValues = []
    def setProgress(self, value): self.progressValues.append(value)
    def isCanceled(self): return self._cancelled
    def cancel(self): self._cancelled = True

class _TaskManager:
    tasks = []
    def addTask(self, task):  # run synchronously for the test
        self.tasks.append(task); ok = task.run(); task.finished(ok)

class QgsApplication:
    _manager = _TaskManager()
    @staticmethod
    def taskManager(): return QgsApplication._manager

class QgsBlockingNetworkRequest(QObject):
    class ErrorCode: NoError = 0
    downloadProgress = pyqtSignal(int, int)
