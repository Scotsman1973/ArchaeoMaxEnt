"""Four-tab Qt interface for ArchaeoMaxEnt.

The dialog gathers inputs and orchestrates each step. It imports only the
QGIS API and the standard library: all work with geopandas, rasterio and
elapid happens in the modelling runtime (see runtimeEnv.py and
modelWorker.py), so the plugin loads on any QGIS without extra packages.
Works with QGIS 3 (Qt5) and QGIS 4 (Qt6).
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QStandardPaths, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget,
)

from . import runtimeEnv
from .explorer import Explorer
from .projectCore import (
    DERIVED_INPUTS_FOLDER, LOGS_FOLDER, RENAMED_FID_FIELD, RESERVED_FID_FIELD,
    WEIGHTED_INPUTS_FOLDER, Project, safeName,
)
from .runtimeClient import RuntimeClient, WorkerCancelled
from .runtimeTask import RuntimeTask, downloadFile
from .shared import (
    ASPECT_SECTOR_COUNT, PREDICTOR_CATEGORICAL, PREDICTOR_CIRCULAR, PREDICTOR_NUMERIC, VALIDATION_LEAVE_ONE_OUT,
    VALIDATION_SPATIAL_K_FOLD, aspectSectorCaption, validateWeights, writeWeightsCsv,
)
from .terrain import createTerrainRasters

WINDOW_TITLE = "ArchaeoMaxEnt"
WINDOW_SIZE = (900, 760)
# What each file chooser accepts.
AREA_FILE_FILTER = "Polygon layer or GeoTIFF (*.gpkg *.shp *.tif *.tiff)"
VECTOR_FILE_FILTER = "Shapefile or GeoPackage (*.gpkg *.shp)"
RASTER_FILE_FILTER = "GeoTIFF (*.tif *.tiff)"
AREA_TOOLTIP = ("A polygon layer (Shapefile or GeoPackage), or a GeoTIFF. For a GeoTIFF, cells with a value "
                "are inside the area and nodata cells are outside; if it holds only 0 and 1 (a mask), "
                "cells with 1 are inside.")
RUNTIME_FILE_FILTER = "ArchaeoMaxEnt runtime (*.zip *.tar.xz)"
RUNTIME_FOLDER_NAME = "archaeoMaxEnt-runtime"
# Runtime .zip files built on this computer are kept here, to copy to other computers of the same kind.
BUILT_RUNTIMES_FOLDER_NAME = "archaeoMaxEnt-runtime-archives"
RUNTIME_DOWNLOAD_SIZE = "250 MB"
# (key, label, file filter) of the two layers every project starts with.
PROJECT_INPUTS = (("geographic", "Geographical area", AREA_FILE_FILTER),
                  ("sites", "Archaeological sites", VECTOR_FILE_FILTER))
TERRAIN_PREDICTORS = ("elevation", "slope", "aspect")
VECTOR_PREDICTOR_SEPARATOR = "::"
DEFAULT_WEIGHT = 5.0
DEFAULT_CLASS_COUNT = 5
EDGE_PADDING = 1e-8
DEFAULT_BACKGROUND_COUNT = 10000
BACKGROUND_RANGE = (100, 1000000)
DEFAULT_SEED = 42
SEED_RANGE = (0, 99999999)
DEFAULT_FOLD_COUNT = 4
FOLD_RANGE = (2, 10)
DEFAULT_BETA_MULTIPLIER = 2.0
BETA_RANGE = (0.1, 20.0)
VALIDATION_CHOICES = (
    ("Leave-one-out (default; best for small numbers of sites)", VALIDATION_LEAVE_ONE_OUT),
    ("Spatial k-fold (GeographicKFold)", VALIDATION_SPATIAL_K_FOLD),
)
PREDICTOR_TYPE_COLUMNS = (
    (PREDICTOR_NUMERIC, "Numeric"),
    (PREDICTOR_CATEGORICAL, "Categorical"),
    (PREDICTOR_CIRCULAR, "Circular"),
)
PREDICTOR_TABLE_HEADERS = ("Include", "Predictor", "Source", *[title for _, title in PREDICTOR_TYPE_COLUMNS], "Note")
FIRST_TYPE_COLUMN = 3
NOTE_COLUMN = FIRST_TYPE_COLUMN + len(PREDICTOR_TYPE_COLUMNS)
DEFAULT_INCLUDED_SOURCE = "weighted"
CATEGORIES_SUFFIX = "Categories"
LOG_FILE_PREFIX = "log"
LOG_TIME_FORMAT = "%Y%m%d-%H%M%S"
PROJECT_TAB_INDEX = 0
INPUTS_TAB_INDEX = 1
LOG_TAB_INDEX = 3
TOO_MANY_VALUES = "(too many distinct values to list)"


def defaultRuntimeDir() -> Path:
    """Return the runtime folder: shared by all QGIS profiles of this user."""
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)) / RUNTIME_FOLDER_NAME


class ArchaeoMaxEntDialog(QDialog):
    """Main plugin window: Project, Geospatial Inputs, Modelling and Logs tabs."""

    def __init__(self, parent=None):
        """Build all four tabs and initialise the workflow state."""
        super().__init__(parent)
        self.pluginDir = Path(__file__).resolve().parent
        self.runtimeDir = defaultRuntimeDir()
        self.project = None
        self.inputEdits = {}
        self.importedPaths = {}
        self.vectorSources = {}
        self.vectorSummaries = {}
        self.sitesSummary = None
        self.demPath = None
        self.currentLabel = None
        self.classLabels = []
        self.currentEdges = None
        self.sitesFieldName = None
        self.chosenCategories = []
        self.predictorRows = []
        self.activeTask = None
        self.runtimeOffered = False

        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_SIZE)
        rootLayout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        rootLayout.addWidget(self.tabs)
        self.createProjectTab()
        self.createEnvironmentTab()
        self.createModelTab()
        self.createLogTab()
        self.tabs.setCurrentIndex(PROJECT_TAB_INDEX)
        self.refreshRuntimeStatus()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def addTab(self, title):
        """Add an empty tab with a vertical layout and return that layout."""
        page = QWidget()
        layout = QVBoxLayout(page)
        self.tabs.addTab(page, title)
        return layout

    def addPathSelector(self, layout, label, isDirectory=False, fileFilter=VECTOR_FILE_FILTER):
        """Add a labelled path field with a Browse button (offering fileFilter's types) and return the field."""
        row = QHBoxLayout()
        edit = QLineEdit()
        button = QPushButton("Browse")

        def browse():
            """Open a file or folder chooser and copy the chosen path into the field."""
            if isDirectory:
                value = QFileDialog.getExistingDirectory(self, label)
            else:
                value, _ = QFileDialog.getOpenFileName(self, label, "", fileFilter)
            if value:
                edit.setText(value)

        button.clicked.connect(browse)
        row.addWidget(QLabel(label))
        row.addWidget(edit)
        row.addWidget(button)
        layout.addLayout(row)
        return edit

    def addButton(self, layout, text, handler):
        """Add a push button whose handler runs inside guard() and return the button."""
        button = QPushButton(text)
        button.clicked.connect(lambda: self.guard(handler))
        layout.addWidget(button)
        return button

    def guard(self, func):
        """Run func, logging and showing any exception instead of letting it escape to QGIS."""
        try:
            return func()
        except Exception as exc:
            self.showError(exc)
            return None

    def showError(self, exc):
        """Log an error (with runtime details, if any) and show it in a message box."""
        self.log(f"ERROR: {exc}")
        details = getattr(exc, "details", "")
        if details:
            self.log(details)
        QMessageBox.critical(self, "ArchaeoMaxEnt error", str(exc))

    def log(self, text):
        """Append text to the Logs tab and, once a project exists, to its log file."""
        self.logBox.append(str(text))
        if self.project:
            self.project.log.info(str(text))

    def requireProject(self):
        """Raise a clear error if no project has been created yet."""
        if not self.project:
            raise ValueError("Create a project first")

    def requireDem(self):
        """Raise a clear error if the elevation DEM has not been imported yet."""
        self.requireProject()
        if not self.demPath:
            raise ValueError("Import the elevation GeoTIFF first")

    # ------------------------------------------------------------------
    # Modelling runtime
    # ------------------------------------------------------------------
    def createRuntimeBox(self, layout):
        """Add the runtime status line and its install buttons."""
        box = QGroupBox("Modelling runtime")
        boxLayout = QVBoxLayout(box)
        self.runtimeLabel = QLabel()
        self.runtimeLabel.setWordWrap(True)
        boxLayout.addWidget(self.runtimeLabel)
        buttonRow = QHBoxLayout()
        boxLayout.addLayout(buttonRow)
        self.addButton(buttonRow, "Install from file (offline)…", self.installRuntimeFromFile)
        self.addButton(buttonRow, "Download and install", self.downloadRuntime)
        self.addButton(buttonRow, "Build on this computer…", self.buildRuntimeHere)
        self.addButton(buttonRow, "Check runtime", self.checkRuntime)
        layout.addWidget(box)

    def refreshRuntimeStatus(self):
        """Show whether the runtime is installed and matches this plugin version."""
        status = runtimeEnv.runtimeStatus(self.runtimeDir, self.pluginDir)
        try:
            expected = runtimeEnv.expectedLock(self.pluginDir)
            needed = f"<br>Needed file: <code>{expected['bundleName']}</code>"
        except runtimeEnv.RuntimeProblem as exc:
            needed = f"<br>{exc}"
        colour = "green" if status.ready else "#b35900"
        extra = "" if status.ready else needed
        self.runtimeLabel.setText(f"<span style='color:{colour}'>{status.message}</span>{extra}")
        return status

    def runtimeClient(self) -> RuntimeClient:
        """Return a client for the installed runtime, or explain how to install it."""
        status = runtimeEnv.runtimeStatus(self.runtimeDir, self.pluginDir)
        if not status.ready:
            raise ValueError(f"{status.message}. Install the modelling runtime on the Project tab first.")
        return RuntimeClient(status.pythonPath, self.pluginDir, self.runtimeDir)

    def callRuntime(self, operation, **args):
        """Run a short runtime operation, showing a busy cursor; return its result."""
        client = self.runtimeClient()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return client.call(operation, args)
        finally:
            QApplication.restoreOverrideCursor()

    def startTask(self, description, work, onSuccess):
        """Run long work in QGIS's task manager; onSuccess(result) runs when it succeeds."""
        if self.activeTask is not None:
            raise ValueError("Another ArchaeoMaxEnt task is still running; wait for it or cancel it")

        def onDone(result, error, cancelled):
            """Report the outcome in the dialog (main thread)."""
            self.activeTask = None
            if cancelled or isinstance(error, WorkerCancelled):
                self.log(f"{description}: cancelled")
            elif error is not None:
                self.showError(error)
            else:
                self.guard(lambda: onSuccess(result))

        task = RuntimeTask(description, work, onDone)
        task.logged.connect(self.log)
        self.activeTask = task  # Keep a reference: QGIS must not lose the task while it runs.
        QgsApplication.taskManager().addTask(task)
        self.log(f"{description}: started (progress and Cancel in the QGIS task bar)")

    def installRuntimeFromFile(self):
        """Install a runtime archive from disk (download, USB stick or shared drive)."""
        path, _ = QFileDialog.getOpenFileName(self, "Choose runtime archive", "", RUNTIME_FILE_FILTER)
        if not path:
            return
        archivePath = Path(path)

        def decide(isPublished):
            """Install a published runtime straight away; ask first about any other runtime file."""
            if isPublished is False:
                answer = QMessageBox.question(
                    self, "Runtime file not published",
                    f"{archivePath.name} is not one of the runtimes published for this version of "
                    "ArchaeoMaxEnt.\n\nThat is expected if it was made with 'Build on this computer' on "
                    "another computer. It can also mean the file was damaged while copying, or altered.\n\n"
                    "Install it only if you know where it came from. Install it?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
            # Already checked (or accepted) above, so the install does not check the checksum again.
            self.installRuntimeArchive(archivePath, keepArchive=True, requirePublished=False)

        self.startTask("Check runtime file",
                       lambda task: runtimeEnv.archiveIsPublished(archivePath, self.pluginDir), decide)

    def installArchive(self, task, archivePath: Path, keepArchive: bool, requirePublished: bool = True):
        """Check and unpack a runtime archive, then confirm every package imports (runs in a background task).

        requirePublished=False for a runtime built on this computer, or one the
        researcher chose to trust: see runtimeEnv.installRuntime.
        """
        def verify(executable, runtimeFolder):
            """Start the worker in the new runtime before it replaces the old one."""
            RuntimeClient(executable, self.pluginDir, runtimeFolder).call("ping")
        task.logged.emit(f"Installing runtime from {archivePath.name}")
        try:
            return runtimeEnv.installRuntime(archivePath, self.runtimeDir, self.pluginDir, verify=verify,
                                             requirePublished=requirePublished)
        finally:
            if not keepArchive:
                archivePath.unlink(missing_ok=True)

    def installRuntimeArchive(self, archivePath: Path, keepArchive: bool, requirePublished: bool = True):
        """Check and unpack a runtime archive in the background."""
        self.startTask("Install modelling runtime",
                       lambda task: self.installArchive(task, archivePath, keepArchive, requirePublished),
                       self.runtimeInstalled)

    def runtimeInstalled(self, status):
        """After any successful install: update the status line and run a full check."""
        self.refreshRuntimeStatus()
        self.log(status.message)
        self.checkRuntime()

    def offerRuntimeInstall(self):
        """When the window first opens in a QGIS session, offer to install a missing or outdated runtime.

        One question is all the researcher sees: the plugin then downloads the
        ready-made runtime, or builds it if it cannot be downloaded (see
        installRuntimeAutomatically). If they decline, the Project tab buttons
        remain, and the question comes back next session.
        """
        if self.runtimeOffered or self.activeTask is not None:
            return
        self.runtimeOffered = True
        if self.refreshRuntimeStatus().ready:
            return
        try:
            runtimeEnv.expectedLock(self.pluginDir)
        except runtimeEnv.RuntimeProblem:
            return  # No runtime exists for this kind of computer; the status line says so.
        outdated = (self.runtimeDir / runtimeEnv.MANIFEST_NAME).is_file()
        reason = ("This version of ArchaeoMaxEnt needs updated modelling tools." if outdated else
                  "ArchaeoMaxEnt needs its modelling tools before it can be used: Python and the "
                  "modelling packages, at the exact versions the plugin was tested with.")
        answer = QMessageBox.question(
            self, "Install the modelling tools",
            f"{reason}\n\nInstall them now? This downloads about {RUNTIME_DOWNLOAD_SIZE}, once. It runs in "
            "the background, so you can keep using QGIS; progress is shown on the Logs tab.\n\n"
            "If you choose No, you can install them later on the Project tab.")
        if answer == QMessageBox.StandardButton.Yes:
            self.installRuntimeAutomatically()

    def installRuntimeAutomatically(self):
        """Get the runtime the easiest way that works, then install it, all in one background task.

        1. Download the ready-made runtime from the plugin's GitHub release
           (using QGIS's network settings) and install it.
        2. If that is not possible (no release yet, GitHub blocked, no
           internet to GitHub, a damaged download), build the runtime on this
           computer from PyPI with the build script instead, and install that.
        Cancelling stops whichever step is running.
        """
        url = runtimeEnv.downloadUrl(self.pluginDir) if runtimeEnv.downloadConfigured(self.pluginDir) else None
        bundleName = runtimeEnv.expectedLock(self.pluginDir)["bundleName"]
        outputDir = self.runtimeDir.parent / BUILT_RUNTIMES_FOLDER_NAME

        def work(task):
            """Download and install, or build and install."""
            if url:
                target = Path(tempfile.gettempdir()) / bundleName
                try:
                    downloadFile(task, url, target)
                    return self.installArchive(task, target, keepArchive=False)
                except Exception as exc:
                    if task.isCanceled():
                        raise
                    task.logged.emit(f"The ready-made runtime could not be used ({exc}). Building it on this "
                                     "computer instead; this takes 5–15 minutes.")
            else:
                task.logged.emit("No download address is set for ready-made runtimes yet. Building the "
                                 "runtime on this computer; this takes 5–15 minutes.")
            archivePath = runtimeEnv.runBuildScript(self.pluginDir, outputDir, onLine=task.logged.emit,
                                                    isCancelled=task.isCanceled)
            task.logged.emit(f"Runtime file kept for other computers of this kind: {archivePath}")
            return self.installArchive(task, archivePath, keepArchive=True, requirePublished=False)

        self.tabs.setCurrentIndex(LOG_TAB_INDEX)
        self.startTask("Install modelling runtime", work, self.runtimeInstalled)

    def downloadRuntime(self):
        """The Download and install button: the same automatic install, after a confirmation."""
        if runtimeEnv.downloadConfigured(self.pluginDir):
            how = (f"Download about {RUNTIME_DOWNLOAD_SIZE} from:\n{runtimeEnv.downloadUrl(self.pluginDir)}\n\n"
                   "If it cannot be downloaded, it is built on this computer from PyPI instead (5–15 minutes).")
        else:
            how = ("No download address is set yet, so the runtime will be built on this computer from PyPI "
                   "(about 300 MB, 5–15 minutes).")
        answer = QMessageBox.question(
            self, "Download modelling runtime",
            f"{how}\n\nIt contains Python and the modelling packages, at the exact versions this plugin was "
            "tested with. Nothing else on your computer is changed.")
        if answer == QMessageBox.StandardButton.Yes:
            self.installRuntimeAutomatically()

    def buildRuntimeHere(self):
        """Download Python and the locked packages and build the runtime on this computer, then install it.

        Runs runtime/buildRuntime.ps1 (Windows) or runtime/buildRuntime.sh
        (macOS, Linux) in the background; for when the ready-made runtime
        cannot be downloaded. The finished .zip is kept, so it can also be
        installed on other computers of the same kind.
        """
        outputDir = self.runtimeDir.parent / BUILT_RUNTIMES_FOLDER_NAME
        answer = QMessageBox.question(
            self, "Build the modelling runtime on this computer",
            "This downloads Python and the modelling packages (about 300 MB, from pypi.org, "
            "files.pythonhosted.org and uv's Python download site) at the exact versions this plugin "
            "was tested with, and builds the runtime from them. It takes 5–15 minutes and needs about "
            "2 GB of free disk space while it runs.\n\n"
            "Nothing is installed elsewhere on your computer. You can keep working in QGIS; progress "
            "is shown on the Logs tab and the build can be cancelled from the QGIS task bar.\n\n"
            f"The finished file is kept in:\n{outputDir}\n\nStart the build?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.tabs.setCurrentIndex(LOG_TAB_INDEX)

        def work(task):
            """Run the build script, passing its messages to the log, then install the result."""
            archivePath = runtimeEnv.runBuildScript(self.pluginDir, outputDir, onLine=task.logged.emit,
                                                    isCancelled=task.isCanceled)
            task.logged.emit(f"Runtime file kept for other computers of this kind: {archivePath}")
            return self.installArchive(task, archivePath, keepArchive=True, requirePublished=False)

        self.startTask("Build and install modelling runtime", work, self.runtimeInstalled)

    def checkRuntime(self):
        """Start the runtime and list its package versions in the log."""
        report = self.callRuntime("ping")
        packages = ", ".join(f"{name} {version}" for name, version in report["packages"].items())
        self.log(f"Runtime OK: Python {report['python']}, GDAL {report['gdal']}; {packages}")
        self.refreshRuntimeStatus()

    # ------------------------------------------------------------------
    # Tab 1: Project
    # ------------------------------------------------------------------
    def createProjectTab(self):
        """Build the Project tab: runtime, name, purpose, folder, input layers and site categories."""
        layout = self.addTab("1. Project")
        self.createRuntimeBox(layout)
        self.nameEdit = QLineEdit()
        self.purposeEdit = QTextEdit()
        self.purposeEdit.setMaximumHeight(70)
        layout.addWidget(QLabel("Project name"))
        layout.addWidget(self.nameEdit)
        layout.addWidget(QLabel("Research purpose"))
        layout.addWidget(self.purposeEdit)
        self.parentDirEdit = self.addPathSelector(layout, "Main directory", isDirectory=True)
        for key, label, fileFilter in PROJECT_INPUTS:
            self.inputEdits[key] = self.addPathSelector(layout, label, fileFilter=fileFilter)
        self.inputEdits["geographic"].setToolTip(AREA_TOOLTIP)

        self.siteFieldCombo = QComboBox()
        self.siteFieldCombo.currentTextChanged.connect(lambda *_: self.guard(self.populateCategories))
        fieldRow = QHBoxLayout()
        fieldRow.addWidget(QLabel("Archaeological category field"))
        fieldRow.addWidget(self.siteFieldCombo, 1)
        layout.addLayout(fieldRow)
        self.inputEdits["sites"].textChanged.connect(lambda *_: self.guard(self.populateFields))
        self.siteCategoryList = QListWidget()
        self.siteCategoryList.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        layout.addWidget(QLabel("Select one or more site categories"))
        layout.addWidget(self.siteCategoryList)
        self.addButton(layout, "Create Project", self.makeProject)

    def populateFields(self):
        """Read the sites layer's fields and category counts once its path points to an existing file."""
        sitesPath = self.inputEdits["sites"].text().strip()
        self.siteFieldCombo.blockSignals(True)
        self.siteFieldCombo.clear()
        self.siteFieldCombo.blockSignals(False)
        self.siteCategoryList.clear()
        self.sitesSummary = None
        if not sitesPath or not Path(sitesPath).is_file():
            return
        self.sitesSummary = self.callRuntime("vectorSummary", path=sitesPath)
        self.siteFieldCombo.blockSignals(True)
        self.siteFieldCombo.addItems(self.sitesSummary["fields"])
        self.siteFieldCombo.blockSignals(False)
        self.populateCategories()

    def populateCategories(self):
        """List each value of the chosen field with its site count, for multi-selection."""
        self.siteCategoryList.clear()
        fieldName = self.siteFieldCombo.currentText()
        if not fieldName or not self.sitesSummary:
            return
        counts = self.sitesSummary["counts"].get(fieldName)
        if counts is None:
            self.siteCategoryList.addItem(TOO_MANY_VALUES)
            return
        for category, count in counts.items():
            item = QListWidgetItem(f"{category} ({count} sites)")
            item.setData(Qt.ItemDataRole.UserRole, category)
            self.siteCategoryList.addItem(item)

    def makeProject(self):
        """Create the project folder and import the geographical area and sites.

        Nothing is kept unless every import succeeds; a failed project folder is deleted.
        """
        if self.project is not None:
            raise ValueError("A project has already been created")
        self.runtimeClient()
        categories = [item.data(Qt.ItemDataRole.UserRole) for item in self.siteCategoryList.selectedItems()
                      if item.data(Qt.ItemDataRole.UserRole) is not None]
        if not categories:
            raise ValueError("Select at least one archaeological category")
        purpose = self.purposeEdit.toPlainText().strip()
        if not purpose:
            raise ValueError("Enter the research purpose")
        fieldName = self.siteFieldCombo.currentText()
        if not fieldName:
            raise ValueError("Select an archaeological category field")

        sources = {key: self.inputEdits[key].text() for key, _, _ in PROJECT_INPUTS}
        for key, source in sources.items():
            if not Path(source).is_file():
                raise FileNotFoundError(f"Missing {key} file: {source}")

        project = Project.create(self.parentDirEdit.text(), self.nameEdit.text())
        try:
            area = self.callRuntime("importArea", projectDir=str(project.projectDir),
                                    source=sources["geographic"], name="geographic")
            if area["note"]:
                self.log(area["note"])
            imported = {
                "geographic": Path(area["path"]),
                "sites": Path(self.callRuntime("importVector", projectDir=str(project.projectDir),
                                               source=sources["sites"], name="sites")),
            }
            if fieldName.lower() == RESERVED_FID_FIELD:
                fieldName = RENAMED_FID_FIELD  # importVector renames a Shapefile 'fid' attribute.
            project.log.info("Purpose: %s", purpose)
            project.log.info("Site field: %s", fieldName)
            project.log.info("Categories: %s", categories)
        except Exception:
            project.discard()
            raise

        self.project = project
        self.importedPaths = imported
        self.sitesFieldName = fieldName
        self.chosenCategories = categories
        self.log("Project created; all inputs imported successfully.")
        self.tabs.setCurrentIndex(INPUTS_TAB_INDEX)

    # ------------------------------------------------------------------
    # Tab 2: Geospatial inputs and class weights
    # ------------------------------------------------------------------
    def createEnvironmentTab(self):
        """Build the Geospatial Inputs tab: DEM, optional vectors, class weights and Explorer."""
        layout = self.addTab("2. Geospatial Inputs")
        self.demEdit = self.addPathSelector(layout, "Elevation GeoTIFF", fileFilter=RASTER_FILE_FILTER)
        self.addButton(layout, "Import elevation and generate slope/aspect", self.importDem)

        self.vectorEdit = self.addPathSelector(layout, "Environmental vector (optional)")
        self.vectorLayerEdit = QLineEdit()
        self.vectorLayerEdit.setPlaceholderText("Optional GeoPackage layer name")
        layout.addWidget(self.vectorLayerEdit)
        self.addButton(layout, "Import vector / read fields", self.importEnvironment)

        layout.addWidget(QLabel("Predictor: elevation, slope, aspect, or imported vector field"))
        self.predictorCombo = QComboBox()
        self.predictorCombo.addItems(TERRAIN_PREDICTORS)
        layout.addWidget(self.predictorCombo)
        self.addButton(layout, "Prepare class weights", self.prepareWeights)

        self.weightTableLabel = QLabel("Weights for: (prepare a predictor first)")
        layout.addWidget(self.weightTableLabel)
        self.weightTable = QTableWidget(0, 2)
        self.weightTable.setHorizontalHeaderLabels(["Class", "Weight (0.0–9.0)"])
        layout.addWidget(self.weightTable)
        self.addButton(layout, "Approve weights and create raster", self.approveWeights)
        layout.addWidget(QLabel(
            "Or keep a vector field's classes without weights, for use as a Categorical predictor:"))
        self.addButton(layout, "Create categorical raster from selected vector field", self.createCategories)
        self.addButton(layout, "Environmental Explorer (read-only)", self.openExplorer)

    def importDem(self):
        """Copy the elevation GeoTIFF into the project and derive slope and aspect from it.

        If slope or aspect cannot be created, the copied DEM is removed again so
        the import can simply be retried.
        """
        self.requireProject()
        demPath = self.project.importRaster(self.demEdit.text(), "elevation")
        try:
            derived = createTerrainRasters(demPath, self.project.folder(DERIVED_INPUTS_FOLDER))
        except Exception:
            demPath.unlink(missing_ok=True)
            for label in ("slope", "aspect"):
                (self.project.folder(DERIVED_INPUTS_FOLDER) / f"{label}.tif").unlink(missing_ok=True)
            raise
        self.demPath = demPath
        self.log(f"Imported elevation and generated {list(derived)}")

    def importEnvironment(self):
        """Import an environmental vector layer and offer each of its fields as a predictor."""
        self.requireProject()
        source = self.vectorEdit.text()
        name = safeName(Path(source).stem)
        path = Path(self.callRuntime("importVector", projectDir=str(self.project.projectDir), source=source,
                                     name=name, layerName=self.vectorLayerEdit.text().strip() or None))
        self.vectorSources[name] = path
        self.vectorSummaries[name] = self.callRuntime("vectorSummary", path=str(path))
        for fieldName in self.vectorSummaries[name]["fields"]:
            self.predictorCombo.addItem(f"{name}{VECTOR_PREDICTOR_SEPARATOR}{fieldName}")
        self.log(f"Imported vector: {path.name}")

    def sourceRaster(self, label):
        """Return the raster behind the selected terrain predictor (DEM, slope or aspect)."""
        if label == "elevation":
            return self.demPath
        return self.project.folder(DERIVED_INPUTS_FOLDER) / f"{label}.tif"

    def classesFor(self, label):
        """Return (class labels, class edges) for a predictor.

        Vector fields use their distinct values, aspect its eight compass
        sectors, elevation and slope equal-width intervals from minimum to maximum.
        """
        if VECTOR_PREDICTOR_SEPARATOR in label:
            name, fieldName = label.split(VECTOR_PREDICTOR_SEPARATOR, 1)
            counts = self.vectorSummaries[name]["counts"].get(fieldName)
            if counts is None:
                raise ValueError(f"{fieldName} has too many distinct values to weight class by class")
            return list(counts), None
        if label == "aspect":
            return list(range(ASPECT_SECTOR_COUNT)), None
        valueRange = self.callRuntime("rasterRange", path=str(self.sourceRaster(label)))
        low, high = valueRange["min"], valueRange["max"] + EDGE_PADDING
        step = (high - low) / DEFAULT_CLASS_COUNT
        edges = [low + step * index for index in range(DEFAULT_CLASS_COUNT)] + [high]
        return list(range(DEFAULT_CLASS_COUNT)), edges

    def prepareWeights(self):
        """Show the selected predictor's classes, each starting at the neutral weight of 5.0."""
        self.requireDem()
        label = self.predictorCombo.currentText()
        classLabels, edges = self.classesFor(label)
        self.currentLabel = label
        self.classLabels = classLabels
        self.currentEdges = edges
        self.weightTable.setRowCount(len(classLabels))
        for row, classLabel in enumerate(classLabels):
            caption = str(classLabel)
            if edges:
                caption = f"{edges[row]:.2f} to {edges[row + 1]:.2f}"
            elif label == "aspect":
                caption = aspectSectorCaption(classLabel)
            item = QTableWidgetItem(caption)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.weightTable.setItem(row, 0, item)
            self.weightTable.setItem(row, 1, QTableWidgetItem(f"{DEFAULT_WEIGHT:.1f}"))
        self.weightTableLabel.setText(f"Weights for: {label}")
        self.log(f"Prepared {label} classes for researcher approval")

    def approveWeights(self):
        """Read the researcher's weights from the table and write the weighted raster."""
        if self.currentLabel is None:
            raise ValueError("Prepare class weights first")
        weights = {}
        for row, classLabel in enumerate(self.classLabels):
            text = self.weightTable.item(row, 1).text()
            try:
                weights[classLabel] = float(text)
            except ValueError:
                raise ValueError(f"Weight '{text}' in row {row + 1} is not a number") from None
        validateWeights(weights)
        if len(set(weights.values())) == 1:
            self.log(f"Warning: every {self.currentLabel} class has weight {next(iter(weights.values()))}; "
                     "this raster has one value everywhere and cannot help the model")

        target = self.project.folder(WEIGHTED_INPUTS_FOLDER) / f"{safeName(self.currentLabel)}.tif"
        pairs = [[classLabel, weight] for classLabel, weight in weights.items()]
        if VECTOR_PREDICTOR_SEPARATOR in self.currentLabel:
            name, fieldName = self.currentLabel.split(VECTOR_PREDICTOR_SEPARATOR, 1)
            self.callRuntime("writeVectorWeights", vectorPath=str(self.vectorSources[name]), fieldName=fieldName,
                             referencePath=str(self.demPath), targetPath=str(target), weights=pairs)
        else:
            self.callRuntime("writeRasterWeights", sourcePath=str(self.sourceRaster(self.currentLabel)),
                             targetPath=str(target), weights=pairs, edges=self.currentEdges,
                             isAspect=self.currentLabel == "aspect")
        descriptions = [self.weightTable.item(row, 0).text() for row in range(len(self.classLabels))]
        csvPath = target.with_suffix(".csv")
        writeWeightsCsv(csvPath, [(classLabel, description, weights[classLabel])
                                  for classLabel, description in zip(self.classLabels, descriptions)])
        self.log(f"Approved {self.currentLabel} weights {weights}; output {target.name} and {csvPath.name}")

    def createCategories(self):
        """Rasterise the selected vector field as class codes in derivedInputs/, with a CSV lookup."""
        self.requireDem()
        label = self.predictorCombo.currentText()
        if VECTOR_PREDICTOR_SEPARATOR not in label:
            raise ValueError("Select an imported vector field (name::field) in the predictor list first")
        name, fieldName = label.split(VECTOR_PREDICTOR_SEPARATOR, 1)
        baseName = f"{safeName(label)}{CATEGORIES_SUFFIX}"
        derivedDir = self.project.folder(DERIVED_INPUTS_FOLDER)
        targetPath = derivedDir / f"{baseName}.tif"
        lookupPath = derivedDir / f"{baseName}.csv"
        if targetPath.exists() or lookupPath.exists():
            raise FileExistsError(f"{targetPath.name} already exists in derivedInputs/; "
                                  "delete it and its .csv to create it again")
        lookup = self.callRuntime("writeVectorCategories", vectorPath=str(self.vectorSources[name]),
                                  fieldName=fieldName, referencePath=str(self.demPath),
                                  targetPath=str(targetPath), lookupPath=str(lookupPath))
        self.log(f"Created categorical raster {targetPath.name} with {len(lookup)} classes; "
                 f"lookup in {lookupPath.name}. On the Modelling tab, press Discover predictors "
                 "and check it is set to Categorical.")

    def openExplorer(self):
        """Open the read-only Environmental Explorer for the project's sites and area."""
        self.requireProject()
        counts = (self.sitesSummary or {}).get("counts", {}).get(self.sitesFieldName) or {}
        dialog = Explorer(self.callRuntime, list(counts), str(self.importedPaths["sites"]), self.sitesFieldName,
                          str(self.importedPaths["geographic"]), self.project.projectDir, self)
        dialog.exec()

    # ------------------------------------------------------------------
    # Tab 3: Modelling
    # ------------------------------------------------------------------
    def createModelTab(self):
        """Build the Modelling tab: predictor table, sampling settings and the run button."""
        layout = self.addTab("3. Modelling")
        layout.addWidget(QLabel(
            "Discover lists every GeoTIFF in weightedInputs/, derivedInputs/ and rawInputs/. "
            "Tick the predictors to include and choose how each is treated: Numeric (values), "
            "Categorical (class codes such as soil types) or Circular (aspect in degrees)."))
        self.addButton(layout, "Discover predictors", self.discoverPredictorTable)
        self.predictorTable = QTableWidget(0, len(PREDICTOR_TABLE_HEADERS))
        self.predictorTable.setHorizontalHeaderLabels(PREDICTOR_TABLE_HEADERS)
        self.predictorTable.verticalHeader().setVisible(False)
        self.predictorTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.predictorTable.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.predictorTable)

        self.backgroundSpin = QSpinBox()
        self.backgroundSpin.setRange(*BACKGROUND_RANGE)
        self.backgroundSpin.setValue(DEFAULT_BACKGROUND_COUNT)
        self.seedSpin = QSpinBox()
        self.seedSpin.setRange(*SEED_RANGE)
        self.seedSpin.setValue(DEFAULT_SEED)
        self.foldSpin = QSpinBox()
        self.foldSpin.setRange(*FOLD_RANGE)
        self.foldSpin.setValue(DEFAULT_FOLD_COUNT)
        self.betaSpin = QDoubleSpinBox()
        self.betaSpin.setRange(*BETA_RANGE)
        self.betaSpin.setValue(DEFAULT_BETA_MULTIPLIER)

        layout.addWidget(QLabel(
            "Background samples: number of random locations inside the geographical area, "
            "representing the available environment"))
        layout.addWidget(self.backgroundSpin)
        layout.addWidget(QLabel("Random seed: allows repeatable random sampling"))
        layout.addWidget(self.seedSpin)

        self.validationCombo = QComboBox()
        for text, method in VALIDATION_CHOICES:
            self.validationCombo.addItem(text, method)
        self.validationCombo.currentIndexChanged.connect(self.updateFoldSpin)
        self.updateFoldSpin()

        self.advancedPanel = QWidget()
        advancedLayout = QFormLayout(self.advancedPanel)
        advancedLayout.addRow("Validation method", self.validationCombo)
        advancedLayout.addRow("Number of spatial folds (k-fold only)", self.foldSpin)
        advancedLayout.addRow("Regularisation multiplier (complexity penalty)", self.betaSpin)
        self.advancedPanel.setVisible(False)
        advancedButton = QPushButton("Advanced Settings")
        advancedButton.clicked.connect(lambda: self.advancedPanel.setVisible(not self.advancedPanel.isVisible()))
        layout.addWidget(advancedButton)
        layout.addWidget(self.advancedPanel)
        self.runButton = self.addButton(layout, "Validate and run MaxEnt", self.runModel)

    def updateFoldSpin(self, *_):
        """Enable the fold count only when spatial k-fold validation is selected."""
        self.foldSpin.setEnabled(self.validationCombo.currentData() == VALIDATION_SPATIAL_K_FOLD)

    def centredWidget(self, widget):
        """Wrap a widget so it sits centred in a table cell."""
        holder = QWidget()
        holderLayout = QHBoxLayout(holder)
        holderLayout.setContentsMargins(0, 0, 0, 0)
        holderLayout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        holderLayout.addWidget(widget)
        return holder

    def discoverPredictorTable(self):
        """Fill the predictor table with every raster in the project's input folders.

        Choices already made for a raster are kept when Discover is pressed
        again; new rasters start with the suggested type, and weighted rasters
        start included. Unusable rasters are shown greyed out with the reason.
        """
        self.requireProject()
        previous = {
            predictor["path"]: (includeItem.checkState() == Qt.CheckState.Checked, predictor["predictorType"])
            for predictor, includeItem, _ in self.predictorRows
        }
        predictors = self.callRuntime("discoverPredictors", projectDir=str(self.project.projectDir))
        self.predictorRows = []
        self.predictorTable.setRowCount(len(predictors))
        enabled, disabled = Qt.ItemFlag.ItemIsEnabled, Qt.ItemFlag.NoItemFlags
        for row, predictor in enumerate(predictors):
            valid = not predictor["problem"]
            included, predictorType = previous.get(
                predictor["path"], (predictor["source"] == DEFAULT_INCLUDED_SOURCE, predictor["suggestedType"]))
            predictor["predictorType"] = predictorType

            includeItem = QTableWidgetItem()
            includeItem.setFlags(Qt.ItemFlag.ItemIsUserCheckable | (enabled if valid else disabled))
            includeItem.setCheckState(Qt.CheckState.Checked if included and valid else Qt.CheckState.Unchecked)
            self.predictorTable.setItem(row, 0, includeItem)
            for column, text in ((1, Path(predictor["path"]).name), (2, predictor["source"]),
                                 (NOTE_COLUMN, predictor["problem"])):
                item = QTableWidgetItem(text)
                item.setFlags(enabled if valid else disabled)
                self.predictorTable.setItem(row, column, item)

            typeGroup = QButtonGroup(self.predictorTable)
            for offset, (typeName, _) in enumerate(PREDICTOR_TYPE_COLUMNS):
                button = QRadioButton()
                button.setChecked(typeName == predictorType)
                button.setEnabled(valid)
                button.toggled.connect(
                    lambda checked, target=predictor, name=typeName: checked and target.update(predictorType=name))
                typeGroup.addButton(button)
                self.predictorTable.setCellWidget(row, FIRST_TYPE_COLUMN + offset, self.centredWidget(button))
            self.predictorRows.append((predictor, includeItem, typeGroup))
        usable = sum(not predictor["problem"] for predictor in predictors)
        self.log(f"Discovered {len(predictors)} rasters; {usable} usable as predictors")

    def includedPredictors(self):
        """Return the predictors ticked in the table, discovering them first if the table is empty."""
        if not self.predictorRows:
            self.discoverPredictorTable()
        included = [predictor for predictor, includeItem, _ in self.predictorRows
                    if not predictor["problem"] and includeItem.checkState() == Qt.CheckState.Checked]
        if not included:
            raise ValueError("Tick at least one predictor in the table")
        return included

    def runModel(self):
        """Run MaxEnt in the background for every chosen category with the ticked predictors."""
        self.requireProject()
        client = self.runtimeClient()
        predictors = self.includedPredictors()
        self.log("Predictors: " + ", ".join(
            f"{predictor['displayName']} ({predictor['predictorType']})" for predictor in predictors))
        args = {
            "projectDir": str(self.project.projectDir),
            "sitesPath": str(self.importedPaths["sites"]),
            "areaPath": str(self.importedPaths["geographic"]),
            "fieldName": self.sitesFieldName,
            "categories": self.chosenCategories,
            "predictors": predictors,
            "backgroundCount": self.backgroundSpin.value(),
            "seed": self.seedSpin.value(),
            "foldCount": self.foldSpin.value(),
            "betaMultiplier": self.betaSpin.value(),
            "validationMethod": self.validationCombo.currentData(),
        }
        self.log(f"Starting MaxEnt with {args['backgroundCount']} background samples "
                 f"(drawn inside the geographical area) and "
                 f"{self.validationCombo.currentText()} validation")

        def work(task):
            """Run all categories in the runtime, passing its events to the task."""
            return client.call("runModels", args, onEvent=task.report, isCancelled=task.isCanceled)

        def onSuccess(results):
            """Summarise each category's validation result."""
            for result in results:
                self.log(f"{result['category']}: validation mean AUC = {result['meanAuc']:.3f} "
                         f"(saved in {result['run']})")

        self.tabs.setCurrentIndex(LOG_TAB_INDEX)
        self.startTask("ArchaeoMaxEnt modelling", work, onSuccess)

    # ------------------------------------------------------------------
    # Tab 4: Logs
    # ------------------------------------------------------------------
    def createLogTab(self):
        """Build the Logs tab: the read-only log with Clear log and Save log buttons."""
        layout = self.addTab("4. Logs")
        self.logBox = QTextEdit()
        self.logBox.setReadOnly(True)
        layout.addWidget(self.logBox)
        buttonRow = QHBoxLayout()
        for text, handler in (("Clear log", self.clearLog), ("Save log", self.saveLog)):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, target=handler: self.guard(target))
            buttonRow.addWidget(button)
        layout.addLayout(buttonRow)

    def clearLog(self):
        """Empty the Logs tab; the project's processing.log file is not changed."""
        self.logBox.clear()

    def saveLog(self):
        """Save the Logs tab as a time-stamped .txt file in the project's logs/ folder."""
        self.requireProject()
        logPath = self.project.folder(LOGS_FOLDER) / f"{LOG_FILE_PREFIX}{datetime.now():{LOG_TIME_FORMAT}}.txt"
        logPath.write_text(self.logBox.toPlainText() + "\n", encoding="utf-8")
        self.log(f"Saved log to {LOGS_FOLDER}/{logPath.name}")
