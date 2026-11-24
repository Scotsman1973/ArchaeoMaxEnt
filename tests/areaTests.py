"""The geographical area can be a polygon layer or a GeoTIFF (run with the modelling packages installed).

    python -I tests/areaTests.py
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import numpy as np, rasterio, geopandas as gpd
from rasterio.transform import from_origin
from shapely.geometry import Point, box
from ArchaeoMaxEnt import geoProcessing, modelEngine, workerOperations
from ArchaeoMaxEnt.projectCore import Project

results = []
def check(name, ok, detail=""):
    results.append(ok); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))

tmp = Path(tempfile.mkdtemp()); N, X0, Y0, CELL = 60, 500000, 5000000, 30
grid = dict(driver="GTiff", width=N, height=N, count=1, crs="EPSG:32633", transform=from_origin(X0, Y0, CELL, CELL))
def write(name, values, **extra):
    path = tmp / name
    with rasterio.open(path, "w", **{**grid, "dtype": values.dtype, **extra}) as target:
        target.write(values, 1)
    return path
def cellArea(count):
    return count * CELL * CELL

yy, xx = np.mgrid[0:N, 0:N]
# 1. A clipped DEM: real values (including 0 and 1, as elevations can be) inside a disc, nodata outside.
disc = (xx - 30) ** 2 + (yy - 30) ** 2 < 20 ** 2
dem = np.where(disc, (xx + yy) % 7, -9999).astype("float32")
frame, rule = geoProcessing.rasterAreaPolygons(write("clippedDem.tif", dem, nodata=-9999))
check("clipped DEM: area = cells with a value", np.isclose(frame.geometry.iloc[0].area, cellArea(disc.sum())), rule)
check("clipped DEM: one row, raster CRS", len(frame) == 1 and frame.crs == grid["crs"])

# 2. A 0/1 mask: 1 inside, 0 outside, plus some nodata.
mask = (xx < 20).astype("uint8"); mask[:5] = 255
frame, rule = geoProcessing.rasterAreaPolygons(write("mask.tif", mask, nodata=255))
check("0/1 mask: only the 1 cells", np.isclose(frame.geometry.iloc[0].area, cellArea(((xx < 20) & (yy >= 5)).sum())), rule)

# 3. A mask with two separate patches and a hole.
patches = np.zeros((N, N), "uint8"); patches[5:25, 5:25] = 1; patches[10:15, 10:15] = 0; patches[40:50, 40:55] = 1
frame, _ = geoProcessing.rasterAreaPolygons(write("patches.tif", patches))
outline = frame.geometry.iloc[0]
check("mask with two patches and a hole", outline.geom_type == "MultiPolygon" and len(outline.geoms) == 2
      and np.isclose(outline.area, cellArea(patches.sum()))
      and not outline.contains(Point(X0 + 12.5 * CELL, Y0 - 12.5 * CELL)))

# 4. No nodata value and not a mask: the whole rectangle, with a note saying so.
frame, rule = geoProcessing.rasterAreaPolygons(write("noNodata.tif", (xx * 3.5).astype("float32")))
check("no nodata: whole rectangle, rule says so", np.isclose(frame.geometry.iloc[0].area, cellArea(N * N))
      and "whole raster rectangle" in rule)

# 5. Refusals with clear messages.
for name, values, extra, expected in (
        ("allNodata.tif", np.full((N, N), -9999, "float32"), {"nodata": -9999}, "no cells with a value"),
        ("allZeroMask.tif", np.zeros((N, N), "uint8"), {}, "no cells with the value 1"),
        ("noCrs.tif", dem, {"nodata": -9999, "crs": None}, "no CRS")):
    try:
        geoProcessing.rasterAreaPolygons(write(name, values, **extra)); check(f"refuses {name}", False)
    except ValueError as exc:
        check(f"refuses {name}", expected in str(exc), str(exc))

# 6. Through the worker operation, as the Project tab calls it, then used by the rest of the plugin.
project = Project.create(tmp, "proj"); project.closeLog()
result = workerOperations.importArea(None, str(project.projectDir), str(tmp / "clippedDem.tif"), "geographic")
areaPath = Path(result["path"])
check("importArea (GeoTIFF) -> rawInputs/geographic.gpkg", areaPath == project.folder("rawInputs") / "geographic.gpkg"
      and areaPath.is_file() and "clippedDem.tif" in result["note"], result["note"])
check("area GeoPackage passes the polygon check", len(geoProcessing.readPolygons(areaPath)) == 1)
check("no .tif copied into rawInputs (it would be offered as a predictor)",
      not list(project.folder("rawInputs").glob("*.tif")))
log = (project.folder("logs") / "processing.log").read_text()
check("import written once to processing.log", log.count("as the geographical area polygon") == 1)

gpd.GeoDataFrame({"name": ["area"]}, geometry=[box(X0, Y0 - N * CELL, X0 + N * CELL, Y0)], crs="EPSG:32633").to_file(tmp / "area.shp")
vectorResult = workerOperations.importArea(None, str(project.projectDir), str(tmp / "area.shp"), "geographicVector")
check("importArea (Shapefile) still copies the polygons", Path(vectorResult["path"]).is_file() and vectorResult["note"] == "")
try:
    workerOperations.importArea(None, str(project.projectDir), str(tmp / "clippedDem.tif"), "geographic")
    check("second import with the same name refused", False)
except FileExistsError:
    check("second import with the same name refused", True)

# Background points from a GeoTIFF area fall only on its cells.
predictorPath = write("predictor.tif", (xx * 2.0 + yy).astype("float32"), nodata=-9999)
points, available = modelEngine.sampleBackground([modelEngine.Predictor(predictorPath, "raw", "p")], areaPath, 400, 1)
rows, cols = rasterio.transform.rowcol(grid["transform"], points.geometry.x, points.geometry.y)
check("background points stay on the GeoTIFF area's cells", available == disc.sum() and bool(disc[rows, cols].all()),
      f"{available} cells")
sites = gpd.GeoDataFrame(geometry=[Point(X0 + 30.5 * CELL, Y0 - 30.5 * CELL)] * 3, crs="EPSG:32633")
shares = geoProcessing.rasterShares(predictorPath, areaPath, sites)
check("Explorer works with a GeoTIFF area", abs(sum(shares["series"]["Geographical area"]) - 100) < 1e-6)

print(f"\n{sum(results)}/{len(results)} passed")
