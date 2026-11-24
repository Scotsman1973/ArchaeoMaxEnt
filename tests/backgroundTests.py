"""Checks for issue 1: background points must come only from inside the geographical area."""
import json, subprocess, sys, tempfile, time, warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import numpy as np, rasterio, geopandas as gpd
from rasterio.transform import from_origin
from shapely.geometry import Point, box, Polygon
from ArchaeoMaxEnt import modelEngine
from ArchaeoMaxEnt.projectCore import Project

results = []
def check(name, ok, detail=""):
    results.append(ok); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))

tmp = Path(tempfile.mkdtemp()); N = 100; X0, Y0, CELL = 500000, 5000000, 30
rng = np.random.default_rng(1)
prof = dict(driver="GTiff", width=N, height=N, count=1, crs="EPSG:32633",
            transform=from_origin(X0, Y0, CELL, CELL), dtype="float32", nodata=-9999)
yy, xx = np.mgrid[0:N, 0:N]
dem = (xx * 2 + yy + rng.normal(0, 2, (N, N))).astype("float32")
slope = (np.abs(xx - 50) + rng.normal(0, 1, (N, N))).astype("float32")
slope[40:50, 10:20] = -9999           # a nodata hole inside the area
paths = {}
for name, arr in (("elevation", dem), ("slope", slope)):
    paths[name] = tmp / f"{name}.tif"
    with rasterio.open(paths[name], "w", **prof) as d: d.write(arr, 1)
preds = [modelEngine.Predictor(path=paths[n], source="raw", label=n) for n in ("elevation", "slope")]

# Irregular area: a triangle in the western part of the DEM (well under half of it)
tri = Polygon([(X0, Y0), (X0 + 60 * CELL, Y0), (X0, Y0 - 90 * CELL)])
area = tmp / "area.gpkg"; gpd.GeoDataFrame(geometry=[tri], crs="EPSG:32633").to_file(area)

# Reference: candidate cells counted the slow, obvious way
centres = [(X0 + (c + .5) * CELL, Y0 - (r + .5) * CELL) for r in range(N) for c in range(N)]
inside = np.array([tri.contains(Point(p)) for p in centres]).reshape(N, N)
valid = inside & (slope != -9999)
pts, available = modelEngine.sampleBackground(preds, area, 1000, seed=42)
check("candidate cell count matches brute force", available == valid.sum(), f"{available} vs {valid.sum()}")
check("requested number of points drawn", len(pts) == 1000)
check("every point inside the area polygon", bool(pts.within(tri).all()))
rows, cols = rasterio.transform.rowcol(prof["transform"], pts.geometry.x, pts.geometry.y)
rows, cols = np.asarray(rows), np.asarray(cols)
check("every point on a valid cell (none in the nodata hole)", bool(valid[rows, cols].all()))
check("no cell drawn twice", len(set(zip(rows, cols))) == len(pts))
check("points sit at cell centres", np.allclose((pts.geometry.x - X0) / CELL % 1, .5))
check("CRS kept", pts.crs == prof["crs"])
again, _ = modelEngine.sampleBackground(preds, area, 1000, seed=42)
other, _ = modelEngine.sampleBackground(preds, area, 1000, seed=7)
check("same seed gives same points", bool(again.geometry.geom_equals(pts.geometry).all()))
check("different seed gives different points", not bool(other.geometry.geom_equals(pts.geometry).all()))
allPts, avail2 = modelEngine.sampleBackground(preds, area, 10**6, seed=1)
check("more requested than available -> every cell once", len(allPts) == avail2 == valid.sum())

# Area stored in a different CRS (lat/lon)
area4326 = tmp / "area4326.gpkg"; gpd.read_file(area).to_crs(4326).to_file(area4326)
p2, a2 = modelEngine.sampleBackground(preds, area4326, 500, seed=3)
check("area in another CRS still works", abs(a2 - available) <= 3 and bool(p2.buffer(1).intersects(tri).all()), f"{a2} cells")

# Area that misses the DEM
far = tmp / "far.gpkg"; gpd.GeoDataFrame(geometry=[box(0, 0, 1000, 1000)], crs="EPSG:32633").to_file(far)
try:
    modelEngine.sampleBackground(preds, far, 100, 1); check("area outside DEM -> clear error", False)
except ValueError as e:
    check("area outside DEM -> clear error", "geographical area" in str(e), str(e)[:70])

# Multi-part area with a hole
outer = box(X0 + 10 * CELL, Y0 - 90 * CELL, X0 + 90 * CELL, Y0 - 10 * CELL)
holed = outer.difference(box(X0 + 30 * CELL, Y0 - 70 * CELL, X0 + 70 * CELL, Y0 - 30 * CELL))
multi = tmp / "multi.gpkg"
gpd.GeoDataFrame(geometry=[holed, box(X0, Y0 - 5 * CELL, X0 + 5 * CELL, Y0)], crs="EPSG:32633").to_file(multi)
p3, _ = modelEngine.sampleBackground(preds, multi, 2000, 5)
union = gpd.read_file(multi).geometry.union_all()
check("multi-polygon with a hole: all points inside, none in the hole", bool(p3.within(union).all()))

# Large raster: memory/time sanity (12000 x 12000 = 144 million cells)
big = tmp / "big.tif"; BN = 12000
with rasterio.open(big, "w", **dict(prof, width=BN, height=BN, compress="deflate", tiled=True)) as d:
    for r in range(0, BN, 1000): d.write(np.ones((1000, BN), "float32"), 1, window=rasterio.windows.Window(0, r, BN, 1000))
bigArea = tmp / "bigArea.gpkg"
gpd.GeoDataFrame(geometry=[Point(X0 + BN * CELL / 2, Y0 - BN * CELL / 2).buffer(BN * CELL / 3)], crs="EPSG:32633").to_file(bigArea)
t = time.time(); pb, ab = modelEngine.sampleBackground([modelEngine.Predictor(big, "raw", "big")], bigArea, 10000, 1)
check("144-million-cell raster sampled in blocks", len(pb) == 10000, f"{time.time()-t:.1f}s, {ab} candidate cells")

# ---- full run through the JSON worker, as QGIS calls it ----
pts60 = [Point(X0 + c * CELL + 15, Y0 - r * CELL - 15) for r, c in rng.integers(0, 50, (60, 2))]
pts60 += [Point(X0 + 95 * CELL, Y0 - 95 * CELL)]   # one site outside the triangle
kinds = ["fort" if i % 2 else "farm" for i in range(len(pts60))]
expectedOutside = sum(1 for k, q in zip(kinds, pts60) if k == "fort" and not tri.intersects(q))
project = Project.create(tmp, "proj")
raw = project.folder("rawInputs")
gpd.GeoDataFrame({"type": kinds}, geometry=pts60, crs="EPSG:32633").to_file(raw / "sites.gpkg")
gpd.read_file(area).to_file(raw / "geographic.gpkg")
import shutil
shutil.copy(paths["elevation"], raw / "elevation.tif"); shutil.copy(paths["slope"], project.folder("derivedInputs") / "slope.tif")
project.closeLog()
predArgs = [{"path": str(raw / "elevation.tif"), "source": "raw", "label": "rawElevation", "predictorType": "numeric"},
            {"path": str(project.folder("derivedInputs") / "slope.tif"), "source": "derived", "label": "derivedSlope", "predictorType": "numeric"}]

def worker(args):
    job = {"protocol": 1, "operation": "runModels", "args": args}
    out = subprocess.run([sys.executable, "-I", str(REPO / "ArchaeoMaxEnt" / "modelWorker.py")], input=json.dumps(job),
                         capture_output=True, text=True).stdout.splitlines()
    return [json.loads(line) for line in out]

base = {"projectDir": str(project.projectDir), "sitesPath": str(raw / "sites.gpkg"), "fieldName": "type",
        "categories": ["fort"], "predictors": predArgs, "backgroundCount": 800, "validationMethod": "spatialKFold", "foldCount": 4}
for label, extra in (("with areaPath from the dialog", {"areaPath": str(raw / "geographic.gpkg")}),
                     ("without areaPath (falls back to rawInputs/geographic.gpkg)", {})):
    events = worker(dict(base, **extra)); final = events[-1]
    ok = final["type"] == "result"
    check(f"worker run {label}", ok, final.get("message", ""))
    if ok:
        res = final["value"][0]; runDir = project.folder("modellingOutputs") / res["run"]
        bg = gpd.read_file(runDir / "backgroundPoints.gpkg")
        ev = json.loads((runDir / "evaluation.json").read_text())
        check("  backgroundPoints.gpkg saved, all inside area", len(bg) == 800 and bool(bg.within(tri).all()))
        check("  evaluation.json records area and counts", ev["backgroundArea"] == "geographic.gpkg"
              and ev["backgroundCellsAvailable"] == available and ev["backgroundUsed"] == 800 and ev["sitesOutsideArea"] == expectedOutside,
              f"{ev['sitesOutsideArea']} of {ev['siteCount']} fort sites outside")
        logs = [e["message"] for e in events if e["type"] == "log"]
        check("  log warns about the site outside the area", any("outside the geographical area" in m for m in logs))
events = worker(dict(base, validationMethod="leaveOneOut", backgroundCount=300))
check("worker run with leave-one-out", events[-1]["type"] == "result", events[-1].get("message", ""))
events = worker(dict(base, areaPath=str(far)))
check("worker reports clear error when area misses the DEM", events[-1]["type"] == "error" and "geographical area" in events[-1]["message"])
print(f"\n{sum(results)}/{len(results)} passed")
