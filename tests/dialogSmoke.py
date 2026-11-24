"""Drive the real dialog offscreen through the whole workflow, using the installed runtime."""
from pathlib import Path as _P
REPO = _P(__file__).resolve().parents[1]
import os, sys, tempfile, json
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path[:0] = [str(REPO / "tests" / "stubs"), str(REPO)]
import numpy as np, rasterio, geopandas as gpd
from rasterio.transform import from_origin
from shapely.geometry import Point, box
from PyQt5.QtWidgets import QApplication, QFileDialog, QMessageBox
from PyQt5.QtCore import Qt
from ArchaeoMaxEnt import runtimeEnv, archaeoMaxEntDialog as dialogModule
from ArchaeoMaxEnt.explorer import Explorer

app = QApplication([])
errors = []
QMessageBox.critical = staticmethod(lambda parent, title, text: errors.append(text))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

tmp = Path(tempfile.mkdtemp())
runtimeDir = tmp / "runtime"
runtimeEnv.installRuntime(next((REPO / "dist").glob("*.zip")), runtimeDir, REPO / "ArchaeoMaxEnt")
dialogModule.defaultRuntimeDir = lambda: runtimeDir

def fakeTerrain(demPath, derivedDir):  # stands in for QGIS Processing gdal:slope / gdal:aspect
    with rasterio.open(demPath) as s:
        z = s.read(1); prof = s.profile
    gy, gx = np.gradient(z.astype(float), 30)
    for name, arr in (("slope", np.degrees(np.arctan(np.hypot(gx, gy)))), ("aspect", (np.degrees(np.arctan2(-gx, gy)) + 360) % 360)):
        with rasterio.open(derivedDir / f"{name}.tif", "w", **prof) as d: d.write(arr.astype("float32"), 1)
    return {"slope": derivedDir / "slope.tif", "aspect": derivedDir / "aspect.tif"}
dialogModule.createTerrainRasters = fakeTerrain

# synthetic inputs
rng = np.random.default_rng(3); N = 90
prof = dict(driver="GTiff", width=N, height=N, count=1, crs="EPSG:32633", transform=from_origin(500000, 5000000, 30, 30), dtype="float32", nodata=-9999)
yy, xx = np.mgrid[0:N, 0:N]
with rasterio.open(tmp / "dem.tif", "w", **prof) as d: d.write((xx * 3 + yy + rng.normal(0, 2, (N, N))).astype("float32"), 1)
pts = [Point(500000 + c * 30 + 15, 5000000 - r * 30 - 15) for r, c in rng.integers(0, N, (40, 2))]
gpd.GeoDataFrame({"type": ["hillfort" if p.x > 501300 else "villa" for p in pts]}, geometry=pts, crs="EPSG:32633").to_file(tmp / "sites.gpkg")
gpd.GeoDataFrame({"n": [1]}, geometry=[box(500000, 5000000 - N * 30, 500000 + N * 30, 5000000)], crs="EPSG:32633").to_file(tmp / "area.shp")
gpd.GeoDataFrame({"soil": ["A", "B", "C"]}, geometry=[box(500000 + i * 900, 5000000 - N * 30, 500000 + (i + 1) * 900, 5000000) for i in range(3)], crs="EPSG:32633").to_file(tmp / "soils.gpkg")

dlg = dialogModule.ArchaeoMaxEntDialog()
print("runtime label:", dlg.runtimeLabel.text()[:80])
dlg.checkRuntime()
dlg.nameEdit.setText("smoke test"); dlg.purposeEdit.setPlainText("Testing")
dlg.parentDirEdit.setText(str(tmp)); dlg.inputEdits["geographic"].setText(str(tmp / "area.shp"))
dlg.inputEdits["sites"].setText(str(tmp / "sites.gpkg"))
dlg.siteFieldCombo.setCurrentText("type")
items = [dlg.siteCategoryList.item(i) for i in range(dlg.siteCategoryList.count())]
print("categories listed:", [i.text() for i in items])
for i in items: i.setSelected(True)
dlg.guard(dlg.makeProject)
dlg.demEdit.setText(str(tmp / "dem.tif")); dlg.guard(dlg.importDem)
dlg.vectorEdit.setText(str(tmp / "soils.gpkg")); dlg.guard(dlg.importEnvironment)

for label, weights in (("soils::soil", ["2", "7.5", "4"]), ("elevation", ["1", "3", "5", "7", "9"]), ("aspect", ["5"] * 8)):
    dlg.predictorCombo.setCurrentText(label); dlg.guard(dlg.prepareWeights)
    for row, w in enumerate(weights): dlg.weightTable.item(row, 1).setText(w)
    dlg.guard(dlg.approveWeights)

dlg.guard(dlg.discoverPredictorTable)
print("predictor table:", [(dlg.predictorTable.item(r, 1).text(), dlg.predictorTable.item(r, 2).text(),
       dlg.predictorTable.item(r, 0).checkState() == Qt.CheckState.Checked) for r in range(dlg.predictorTable.rowCount())])
dlg.guard(dlg.runModel)
task = dialogModule.QgsApplication.taskManager().tasks[-1]
print("task progress samples:", [round(v) for v in task.progressValues][:3], "...", round(task.progressValues[-1]))

ex = Explorer(dlg.callRuntime, ["hillfort", "villa"], str(dlg.importedPaths["sites"]), dlg.sitesFieldName,
              str(dlg.importedPaths["geographic"]), dlg.project.projectDir, dlg)
ex.dataPath = str(dlg.project.folder("weightedInputs") / "elevation.tif"); ex.setFieldChoices(["Raster values"]); ex.refresh()
print("explorer pixmap:", not ex.chartLabel.pixmap().isNull())

# a deliberate mistake: weights out of range must show an error, not crash
dlg.predictorCombo.setCurrentText("slope"); dlg.guard(dlg.prepareWeights)
dlg.weightTable.item(0, 1).setText("12"); dlg.guard(dlg.approveWeights)

log = dlg.logBox.toPlainText()
print("--- log excerpt ---")
for line in log.splitlines():
    if any(k in line for k in ("Runtime OK", "Project created", "Imported", "Warning", "Discovered", "AUC", "ERROR", "started")):
        print("  ", line[:150])
print("errors shown to user:", errors)

# ---- checks added with the .zip runtime, GeoTIFF area and build button ----
results = []
def check(name, ok, detail=""):
    results.append(ok); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))

processingLog = (dlg.project.folder("logs") / "processing.log").read_text()
aucLines = [line for line in processingLog.splitlines() if "hillfort validation run 1 AUC" in line]
check("processing.log has each model message once", len(aucLines) == 1, f"{len(aucLines)} copies")
check("file choosers offer only what each input accepts",
      ".tif" in dialogModule.AREA_FILE_FILTER and ".tif" not in dialogModule.VECTOR_FILE_FILTER
      and ".shp" not in dialogModule.RASTER_FILE_FILTER and ".zip" in dialogModule.RUNTIME_FILE_FILTER)

# A second project whose geographical area is a GeoTIFF (a DEM clipped to a disc, nodata outside).
with rasterio.open(tmp / "dem.tif") as s: z = s.read(1); demProfile = s.profile
disc = (xx - 45) ** 2 + (yy - 45) ** 2 < 40 ** 2
with rasterio.open(tmp / "areaDem.tif", "w", **demProfile) as d: d.write(np.where(disc, z, -9999).astype("float32"), 1)
dlg2 = dialogModule.ArchaeoMaxEntDialog()
dlg2.nameEdit.setText("tif area"); dlg2.purposeEdit.setPlainText("Testing a GeoTIFF area")
dlg2.parentDirEdit.setText(str(tmp)); dlg2.inputEdits["geographic"].setText(str(tmp / "areaDem.tif"))
dlg2.inputEdits["sites"].setText(str(tmp / "sites.gpkg")); dlg2.siteFieldCombo.setCurrentText("type")
for i in range(dlg2.siteCategoryList.count()): dlg2.siteCategoryList.item(i).setSelected(True)
errors.clear(); dlg2.guard(dlg2.makeProject)
areaPath = dlg2.importedPaths.get("geographic")
check("project with a GeoTIFF area is created", not errors and areaPath is not None and areaPath.suffix == ".gpkg", str(errors))
check("Logs tab explains how the GeoTIFF became the area", "nodata cells are outside" in dlg2.logBox.toPlainText())
dlg2.demEdit.setText(str(tmp / "dem.tif")); dlg2.guard(dlg2.importDem)
dlg2.guard(dlg2.discoverPredictorTable)
for row in range(dlg2.predictorTable.rowCount()):  # no weighted rasters here, so tick the raw/derived ones
    dlg2.predictorTable.item(row, 0).setCheckState(Qt.CheckState.Checked)
errors.clear(); dlg2.guard(dlg2.runModel)
runDir = next(dlg2.project.folder("modellingOutputs").glob("hillfortRun01"), None)
background = gpd.read_file(runDir / "backgroundPoints.gpkg") if runDir else None
inside = background is not None and bool(background.within(gpd.read_file(areaPath).geometry.iloc[0]).all())
check("model runs with the GeoTIFF area; background points inside it", not errors and inside, str(errors))

# The "Build on this computer" button: real download and build, then install, as a user would do it.
if os.environ.get("SKIP_BUILD_BUTTON") != "1":
    import shutil
    shutil.rmtree(runtimeDir)
    dlg.refreshRuntimeStatus()
    errors.clear(); dlg.guard(dlg.buildRuntimeHere)
    built = list((tmp / dialogModule.BUILT_RUNTIMES_FOLDER_NAME).glob("*.zip"))
    status = runtimeEnv.runtimeStatus(runtimeDir, REPO / "ArchaeoMaxEnt")
    check("Build on this computer: zip built, kept and installed", not errors and len(built) == 1 and status.ready,
          f"{status.message}; {errors}")
    check("build messages shown on the Logs tab", "uv checksum OK" in dlg.logBox.toPlainText())

# ---- the automatic install: one question when the window first opens, download first, build if needed ----
if os.environ.get("SKIP_BUILD_BUTTON") != "1":
    import shutil
    builtZip = built[0]  # a real runtime .zip from the build-button check above
    questions = []
    QMessageBox.question = staticmethod(lambda *a, **k: questions.append(a[1]) or QMessageBox.StandardButton.Yes)

    def freshDialog():
        """A new window (as in a new QGIS session) with the runtime uninstalled."""
        shutil.rmtree(runtimeDir, ignore_errors=True)
        return dialogModule.ArchaeoMaxEntDialog()

    # Runtime already installed: no question at all.
    dlg3 = dialogModule.ArchaeoMaxEntDialog(); questions.clear(); dlg3.offerRuntimeInstall()
    check("no question when the runtime is installed", questions == [])

    # Download works: the downloaded file is installed and then deleted.
    runtimeEnv.downloadConfigured = lambda pluginDir: True
    downloads = []
    def fakeDownload(task, url, target):
        downloads.append(url); shutil.copy(builtZip, target); return target
    dialogModule.downloadFile = fakeDownload
    dlg4 = freshDialog(); questions.clear(); errors.clear(); dlg4.offerRuntimeInstall()
    status = runtimeEnv.runtimeStatus(runtimeDir, REPO / "ArchaeoMaxEnt")
    check("first open asks once, downloads and installs", questions == ["Install the modelling tools"]
          and len(downloads) == 1 and status.ready and not errors, f"{questions}, {status.message}, {errors}")
    check("downloaded file removed after installing", not (Path(tempfile.gettempdir()) / builtZip.name).exists())
    questions.clear(); dlg4.offerRuntimeInstall()
    check("not asked again in the same session", questions == [])

    # Download fails (e.g. GitHub blocked): it builds on this computer instead, for real.
    def failingDownload(task, url, target):
        raise RuntimeError("Download failed: Error transferring - server replied: Not Found")
    dialogModule.downloadFile = failingDownload
    checksumsFile = REPO / "ArchaeoMaxEnt" / "runtime" / "checksums.json"
    checksumsFile.write_text(json.dumps({builtZip.name: "0" * 64}))  # as in a released plugin
    dlg5 = freshDialog(); errors.clear()
    try:
        dlg5.offerRuntimeInstall()
    finally:
        checksumsFile.unlink()
    status = runtimeEnv.runtimeStatus(runtimeDir, REPO / "ArchaeoMaxEnt")
    log5 = dlg5.logBox.toPlainText()
    check("failed download falls back to building, then installs (with published checksums present)", status.ready and not errors
          and "Building it on this computer instead" in log5 and "uv checksum OK" in log5, f"{status.message}, {errors}")

    # No download address yet (OWNER placeholder): straight to building, no download attempted.
    runtimeEnv.downloadConfigured = lambda pluginDir: False
    downloads.clear(); dialogModule.downloadFile = fakeDownload
    builds = []
    realBuild = runtimeEnv.runBuildScript
    def fakeBuild(pluginDir, outputDir, onLine=None, isCancelled=None):
        builds.append(outputDir); return builtZip  # the real build was proven just above
    runtimeEnv.runBuildScript = fakeBuild
    dlg6 = freshDialog(); errors.clear(); dlg6.offerRuntimeInstall()
    status = runtimeEnv.runtimeStatus(runtimeDir, REPO / "ArchaeoMaxEnt")
    check("no download address: builds without trying to download", not downloads and len(builds) == 1
          and status.ready and "No download address is set" in dlg6.logBox.toPlainText())
    check("a built runtime .zip is kept for other computers", builtZip.is_file())
    runtimeEnv.runBuildScript = realBuild

    # Install from file with a runtime not in the published checksums: asked first, then installed.
    checksumsFile.write_text(json.dumps({builtZip.name: "0" * 64}))
    try:
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(builtZip), ""))
        dlg8 = freshDialog(); questions.clear(); errors.clear(); dlg8.installRuntimeFromFile()
        status = runtimeEnv.runtimeStatus(runtimeDir, REPO / "ArchaeoMaxEnt")
        check("Install from file: unpublished runtime asks, then installs", questions == ["Runtime file not published"]
              and status.ready and not errors, f"{questions}, {status.message}, {errors}")
    finally:
        checksumsFile.unlink()

    # Declining: nothing happens, and the Project tab still shows what is needed.
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    dlg7 = freshDialog(); dlg7.offerRuntimeInstall()
    check("declining installs nothing", not runtimeEnv.runtimeStatus(runtimeDir, REPO / "ArchaeoMaxEnt").ready
          and dlg7.activeTask is None and "Needed file" in dlg7.runtimeLabel.text())

print(f"\n{sum(results)}/{len(results)} passed")
