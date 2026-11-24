"""End-to-end tests of the runtime: offline install, every worker operation, failure handling."""
from pathlib import Path as _P
REPO = _P(__file__).resolve().parents[1]
import json, os, shutil, sys, tarfile, tempfile, threading, time, io, zipfile
from pathlib import Path
sys.path.insert(0, str(REPO))
from ArchaeoMaxEnt import runtimeEnv
from ArchaeoMaxEnt.runtimeClient import RuntimeClient, WorkerCancelled, WorkerError

PLUGIN = REPO / "ArchaeoMaxEnt"
ARCHIVE = next((REPO / "dist").glob("*.zip"))
tmp = Path(tempfile.mkdtemp())
results = []
def check(name, ok, detail=""):
    results.append(ok); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))

# ---- install (offline: local file only) ----
runtimeDir = tmp / "rt"
check("status before install", not runtimeEnv.runtimeStatus(runtimeDir, PLUGIN).ready,
      runtimeEnv.runtimeStatus(runtimeDir, PLUGIN).message)
t = time.time()
calls = []
status = runtimeEnv.installRuntime(ARCHIVE, runtimeDir, PLUGIN,
    verify=lambda exe, folder: calls.append(RuntimeClient(exe, PLUGIN, folder).call("ping")))
check("install from archive", status.ready, f"{status.message}; {time.time()-t:.0f}s")
check("verify hook ran in new runtime", bool(calls) and calls[0]["packages"]["elapid"] == "1.0.4")
check("no leftover temp folders", sorted(p.name for p in tmp.iterdir()) == ["rt"])
python = runtimeEnv.pythonPath(runtimeDir, runtimeEnv.platformKey())
check("zip kept Python executable", os.access(python, os.X_OK))
links = [p for p in runtimeDir.rglob("*") if p.is_symlink()]
check("zip kept symbolic links as links", os.name == "nt" or (links and all(p.resolve().exists() for p in links)), f"{len(links)} links")

# plugin with a changed lock -> runtime must be reported as not matching
otherPlugin = tmp / "pluginCopy" / "ArchaeoMaxEnt"
shutil.copytree(PLUGIN, otherPlugin, ignore=shutil.ignore_patterns("__pycache__"))
lock = runtimeEnv.lockPath(otherPlugin, runtimeEnv.platformKey())
lock.write_text(lock.read_text().replace("elapid==1.0.4", "elapid==1.0.3"))
st = runtimeEnv.runtimeStatus(runtimeDir, otherPlugin)
check("changed lock -> not ready", not st.ready, st.message)
try:
    runtimeEnv.installRuntime(ARCHIVE, tmp / "rt2", otherPlugin); check("changed lock -> install refused", False)
except runtimeEnv.RuntimeProblem as e:
    check("changed lock -> install refused before unpacking", not (tmp / "rt2").exists(), str(e)[:90])

# CRLF line endings (git autocrlf on Windows) must not change the hash
crlfPlugin = tmp / "crlf" / "ArchaeoMaxEnt"; shutil.copytree(PLUGIN, crlfPlugin, ignore=shutil.ignore_patterns("__pycache__"))
crlfLock = runtimeEnv.lockPath(crlfPlugin, runtimeEnv.platformKey())
crlfLock.write_bytes(crlfLock.read_bytes().replace(b"\n", b"\r\n"))
check("CRLF lock still matches", runtimeEnv.runtimeStatus(runtimeDir, crlfPlugin).ready)

def fakeZip(path, entries):
    """Write a small zip; entries are (name, bytes, unix mode or None)."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload, mode in entries:
            info = zipfile.ZipInfo(name); info.create_system = 3
            if mode is not None:
                info.external_attr = mode << 16
            zf.writestr(info, payload)
    return path

wrongZip = fakeZip(tmp / "fake.zip", [("manifest.json", json.dumps({"format": 1, "platform": "windows-x86_64", "lockHash": "x"}).encode(), None)])
try:
    runtimeEnv.installRuntime(wrongZip, tmp / "rtz", PLUGIN); check("wrong platform zip refused", False)
except runtimeEnv.RuntimeProblem as e:
    check("wrong platform zip refused", True, str(e))
goodManifest = (runtimeDir / "manifest.json").read_bytes()
for label, entry in (("path escape zip refused", ("../escapedZip.txt", b"x", None)),
                     ("absolute path zip refused", ("/tmp/absZip.txt", b"x", None)),
                     ("link pointing outside refused", ("python/evil", b"../../../etc", 0o120777))):
    bad = fakeZip(tmp / "bad.zip", [("manifest.json", goodManifest, None), entry])
    try:
        runtimeEnv.installRuntime(bad, tmp / "rtbad", PLUGIN); check(label, False)
    except runtimeEnv.RuntimeProblem as e:
        check(label, not (tmp / "escapedZip.txt").exists() and not (tmp / "rtbad").exists(), str(e)[:70])
try:
    runtimeEnv.installRuntime(fakeZip(tmp / "notRuntime.zip", [("readme.txt", b"hi", None)]), tmp / "rtn", PLUGIN)
    check("zip without manifest refused", False)
except runtimeEnv.RuntimeProblem as e:
    check("zip without manifest refused", "manifest" in str(e))
(tmp / "junk.zip").write_bytes(b"not an archive at all")
try:
    runtimeEnv.installRuntime(tmp / "junk.zip", tmp / "rtj", PLUGIN); check("junk file refused", False)
except runtimeEnv.RuntimeProblem as e:
    check("junk file refused with clear message", ".zip expected" in str(e), str(e))

# older .tar runtimes (like a renamed .tar.xz) still install
legacy = tmp / "legacy-renamed.zip"
with tarfile.open(legacy, "w:gz") as tf:
    tf.add(runtimeDir / "manifest.json", "manifest.json"); tf.add(runtimeDir / "python", "python")
st = runtimeEnv.installRuntime(legacy, tmp / "rtlegacy", PLUGIN)
check("older .tar runtime (renamed .zip) still installs", st.ready, st.message)
shutil.rmtree(tmp / "rtlegacy"); legacy.unlink()

# wrong-platform archive (manifest edited) -> refused
fake = tmp / "fake.tar.xz"
with tarfile.open(fake, "w:xz") as tf:
    data = json.dumps({"format": 1, "platform": "windows-x86_64", "lockHash": "x"}).encode()
    info = tarfile.TarInfo("manifest.json"); info.size = len(data); tf.addfile(info, io.BytesIO(data))
try:
    runtimeEnv.installRuntime(fake, tmp / "rt3", PLUGIN); check("wrong platform refused", False)
except runtimeEnv.RuntimeProblem as e:
    check("wrong platform refused", True, str(e))

# path-escape archive -> refused, nothing written outside
evil = tmp / "evil.tar.xz"
manifest = json.loads((runtimeDir / "manifest.json").read_text())
with tarfile.open(evil, "w:xz") as tf:
    for name, payload in (("manifest.json", json.dumps(manifest).encode()), ("../escaped.txt", b"x")):
        info = tarfile.TarInfo(name); info.size = len(payload); tf.addfile(info, io.BytesIO(payload))
try:
    runtimeEnv.installRuntime(evil, tmp / "rt4", PLUGIN); check("path escape refused", False)
except Exception as e:
    check("path escape refused", not (tmp / "escaped.txt").exists() and not (tmp / "rt4").exists(), type(e).__name__)

# published checksum mismatch -> refused
sumPlugin = tmp / "sum" / "ArchaeoMaxEnt"; shutil.copytree(PLUGIN, sumPlugin, ignore=shutil.ignore_patterns("__pycache__"))
(sumPlugin / "runtime" / "checksums.json").write_text(json.dumps({ARCHIVE.name: "0" * 64}))
try:
    runtimeEnv.installRuntime(ARCHIVE, tmp / "rt5", sumPlugin); check("checksum mismatch refused", False)
except runtimeEnv.RuntimeProblem as e:
    check("checksum mismatch refused", "not the one published" in str(e), str(e)[:70])
# A runtime built on this computer differs from the published file, and must still install.
st = runtimeEnv.installRuntime(ARCHIVE, tmp / "rt5", sumPlugin, requirePublished=False)
check("locally built runtime installs although checksums are published", st.ready, st.message)
shutil.rmtree(tmp / "rt5")
check("archiveIsPublished: not listed -> False", runtimeEnv.archiveIsPublished(ARCHIVE, sumPlugin) is False)
check("archiveIsPublished: no checksums -> None", runtimeEnv.archiveIsPublished(ARCHIVE, PLUGIN) is None)
(sumPlugin / "runtime" / "checksums.json").write_text(json.dumps({ARCHIVE.name: runtimeEnv.fileSha256(ARCHIVE)}))
check("archiveIsPublished: listed -> True", runtimeEnv.archiveIsPublished(ARCHIVE, sumPlugin) is True)
# A runtime for another kind of computer: say so (not "damaged"), even when checksums are published.
otherKey = "linux-aarch64" if runtimeEnv.platformKey() != "linux-aarch64" else "linux-x86_64"
other = tmp / f"archaeoMaxEnt-runtime-x-{otherKey}.zip"
with zipfile.ZipFile(ARCHIVE) as source, zipfile.ZipFile(other, "w") as target:
    manifest = json.loads(source.read("manifest.json")); manifest["platform"] = otherKey
    target.writestr("manifest.json", json.dumps(manifest))
try:
    runtimeEnv.installRuntime(other, tmp / "rt6", sumPlugin); check("other computer's runtime: clear message", False)
except runtimeEnv.RuntimeProblem as e:
    check("other computer's runtime: clear message", "but this computer is" in str(e)
          and runtimeEnv.expectedLock(PLUGIN)["bundleName"] in str(e), str(e))

# reinstall over an existing runtime replaces it atomically
(runtimeDir / "marker").write_text("old")
runtimeEnv.installRuntime(ARCHIVE, runtimeDir, PLUGIN)
check("reinstall replaces old runtime", not (runtimeDir / "marker").exists() and runtimeEnv.runtimeStatus(runtimeDir, PLUGIN).ready)

# ---- worker operations on synthetic data ----
import numpy as np, rasterio, geopandas as gpd
from rasterio.transform import from_origin
from shapely.geometry import Point, box
from ArchaeoMaxEnt.projectCore import Project
rng = np.random.default_rng(1); N = 100
data = tmp / "data"; data.mkdir()
prof = dict(driver="GTiff", width=N, height=N, count=1, crs="EPSG:32633", transform=from_origin(500000, 5000000, 30, 30), dtype="float32", nodata=-9999)
yy, xx = np.mgrid[0:N, 0:N]
with rasterio.open(data / "dem.tif", "w", **prof) as d: d.write((xx * 2 + yy + rng.normal(0, 2, (N, N))).astype("float32"), 1)
pts = [Point(500000 + c * 30 + 15, 5000000 - r * 30 - 15) for r, c in rng.integers(0, N, (60, 2))]
kinds = ["fort" if p.x > 500000 + 60 * 30 else "farm" for p in pts]
gpd.GeoDataFrame({"type": kinds, "period": rng.choice([1, 2, 3], 60).astype(float), "notes": [None] * 60}, geometry=pts, crs="EPSG:32633").to_file(data / "sites.shp")
gpd.GeoDataFrame({"name": ["area"]}, geometry=[box(500000, 5000000 - N * 30, 500000 + N * 30, 5000000)], crs="EPSG:32633").to_file(data / "area.gpkg")
soils = [box(500000 + i * 750, 5000000 - N * 30, 500000 + (i + 1) * 750, 5000000) for i in range(4)]
gpd.GeoDataFrame({"soil": ["Luvisol", "Cambisol", "Fluvisol", "Regosol"]}, geometry=soils, crs="EPSG:32633").to_file(data / "soils.gpkg")

os.environ.update({"GDAL_DATA": "/nonexistent/gdal", "PROJ_LIB": "/nonexistent/proj", "PROJ_DATA": "/nonexistent/proj",
                   "PYTHONPATH": "/nonexistent/qgis/python", "PYTHONHOME": "/nonexistent/home"})  # what QGIS may set
client = RuntimeClient(runtimeEnv.runtimeStatus(runtimeDir, PLUGIN).pythonPath, PLUGIN, runtimeDir)
ping = client.call("ping"); check("ping with polluted QGIS-style environment", ping["gdal"].startswith("3."), ping["gdal"])

summary = client.call("vectorSummary", {"path": str(data / "sites.shp")})
check("vectorSummary fields & counts", summary["counts"]["type"] == {"farm": kinds.count("farm"), "fort": kinds.count("fort")} and "period" in summary["counts"],
      f"period labels {list(summary['counts']['period'])}")

project = Project.create(tmp, "proj")
sitesGpkg = client.call("importVector", {"projectDir": str(project.projectDir), "source": str(data / "sites.shp"), "name": "sites"})
areaGpkg = client.call("importVector", {"projectDir": str(project.projectDir), "source": str(data / "area.gpkg"), "name": "geographic"})
soilGpkg = client.call("importVector", {"projectDir": str(project.projectDir), "source": str(data / "soils.gpkg"), "name": "soils"})
dem = project.importRaster(str(data / "dem.tif"), "elevation")
check("importVector x3", all(Path(p).is_file() for p in (sitesGpkg, areaGpkg, soilGpkg)))

rng_ = client.call("rasterRange", {"path": str(dem)})
edges = list(np.linspace(rng_["min"], rng_["max"] + 1e-8, 6))
client.call("writeRasterWeights", {"sourcePath": str(dem), "targetPath": str(project.folder("weightedInputs") / "elevation.tif"),
                                    "weights": [[i, w] for i, w in enumerate([1, 2, 4, 6, 8])], "edges": edges})
client.call("writeVectorWeights", {"vectorPath": soilGpkg, "fieldName": "soil", "referencePath": str(dem),
                                    "targetPath": str(project.folder("weightedInputs") / "soils_soil.tif"),
                                    "weights": [["Luvisol", 3], ["Cambisol", 5], ["Fluvisol", 7.5], ["Regosol", 2]]})
lookup = client.call("writeVectorCategories", {"vectorPath": soilGpkg, "fieldName": "soil", "referencePath": str(dem),
    "targetPath": str(project.folder("derivedInputs") / "soilCategories.tif"), "lookupPath": str(project.folder("derivedInputs") / "soilCategories.csv")})
check("weighted + categorical rasters", len(lookup) == 4 and (project.folder("weightedInputs") / "soils_soil.tif").is_file())

preds = client.call("discoverPredictors", {"projectDir": str(project.projectDir)})
types = {Path(p["path"]).name: p["suggestedType"] for p in preds}
check("discoverPredictors", types.get("soilCategories.tif") == "categorical" and all(not p["problem"] for p in preds), str(types))

chosen = [dict(p, predictorType=p["suggestedType"]) for p in preds if p["source"] in ("weighted", "derived")]
events = []
args = {"projectDir": str(project.projectDir), "sitesPath": sitesGpkg, "fieldName": "type", "categories": ["fort", "farm"],
        "predictors": chosen, "backgroundCount": 2000, "validationMethod": "spatialKFold", "foldCount": 4}
t = time.time()
res = client.call("runModels", args, onEvent=events.append)
progress = [e["value"] for e in events if e["type"] == "progress"]
check("runModels two categories", [r["category"] for r in res] == ["fort", "farm"] and all(0 <= r["meanAuc"] <= 1 for r in res),
      f"AUC {[round(r['meanAuc'], 3) for r in res]}, {time.time()-t:.0f}s")
check("progress events rise to 1.0", progress == sorted(progress) and abs(progress[-1] - 1) < 1e-9, f"{len(progress)} events")
check("log events forwarded", any("validation run" in e.get("message", "") for e in events if e["type"] == "log"))
runDir = project.folder("modellingOutputs") / res[0]["run"]
check("outputs written", all((runDir / f).is_file() for f in ("prediction.tif", "prediction.jpg", "evaluation.json", "backgroundPoints.gpkg")))
logText = (project.folder("logs") / "processing.log").read_text()
check("model run not written to processing.log twice", "validation run" not in logText,
      "the worker leaves writing the forwarded messages to QGIS")

with rasterio.open(data / "areaMask.tif", "w", **dict(prof, dtype="uint8", nodata=None)) as d: d.write((xx < 50).astype("uint8"), 1)
areaTif = client.call("importArea", {"projectDir": str(project.projectDir), "source": str(data / "areaMask.tif"), "name": "areaFromTif"})
check("importArea with a GeoTIFF mask", Path(areaTif["path"]).suffix == ".gpkg" and "value 1" in areaTif["note"], areaTif["note"][:80])

chart = client.call("explorerChart", {"dataPath": soilGpkg, "fieldName": "soil", "areaPath": areaGpkg, "sitesPath": sitesGpkg,
                                       "siteField": "type", "category": "fort", "outputPath": str(tmp / "chart.png")})
check("explorerChart PNG", (tmp / "chart.png").stat().st_size > 5000, f"labels {chart['labels']}")

# cancellation during leave-one-out
cancel = threading.Event(); threading.Timer(1, cancel.set).start()
t = time.time()
try:
    client.call("runModels", dict(args, validationMethod="leaveOneOut", categories=["farm"]), isCancelled=cancel.is_set)
    check("cancel stops worker", False)
except WorkerCancelled:
    check("cancel stops worker", time.time() - t < 6, f"stopped after {time.time()-t:.1f}s")
runs = sorted(p.name for p in project.folder("modellingOutputs").iterdir())
runs = [r for r in runs if not r.endswith(".partial")]  # a killed run may leave its .partial folder; the next run removes it
check("cancelled run left no finished-looking folder", runs == ["farmRun01", "fortRun01"], str(runs))
(project.folder("modellingOutputs") / "fortRun09.partial").mkdir()
client.call("runModels", dict(args, categories=["fort"]))
runs = sorted(p.name for p in project.folder("modellingOutputs").iterdir())
check("next run removes half-written folders", runs == ["farmRun01", "fortRun01", "fortRun02"], str(runs))

# errors come back as WorkerError with traceback
try:
    client.call("rasterRange", {"path": str(tmp / "missing.tif")}); check("error propagation", False)
except WorkerError as e:
    check("error propagation", "Traceback" in e.details, str(e)[:80])
try:
    client.call("noSuchOperation"); check("unknown operation", False)
except WorkerError as e:
    check("unknown operation rejected", "Unknown operation" in str(e))

print(f"\n{sum(results)}/{len(results)} passed")
