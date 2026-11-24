"""GIS processing: classification, weighting and area comparisons.

Runs inside the modelling runtime (see modelWorker.py), never inside QGIS:
it needs geopandas, rasterio and numpy. Terrain derivation, which uses QGIS
Processing, lives in terrain.py.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import MergeAlg
from rasterio.features import geometry_mask, rasterize, shapes
from rasterio.windows import Window
from shapely.geometry import shape
from shapely.ops import unary_union

from .shared import ASPECT_SECTOR_DEGREES, validateWeights

NODATA_VALUE = -9999.0
POLYGON_TYPES = ("Polygon", "MultiPolygon")
DEFAULT_BIN_COUNT = 10
MAX_DISCRETE_VALUES = 20
UNCLASSIFIED_LABEL = "Unclassified"
# GeoTIFF metadata tag holding the researcher's class weights, read back for evaluation.json.
WEIGHTS_TAG = "archaeoMaxEntWeights"
# GeoTIFF metadata tag holding a categorical raster's code -> class name lookup.
CATEGORIES_TAG = "archaeoMaxEntCategories"
CATEGORY_NODATA = -1
AREA_BLOCK_ROWS = 512


def weightsTag(weights: dict, edges=None) -> str:
    """Return the class weights (and class edges, if any) as JSON for a GeoTIFF tag."""
    record = {"weights": {str(classLabel): weight for classLabel, weight in weights.items()}}
    if edges is not None:
        record["classEdges"] = [float(edge) for edge in edges]
    return json.dumps(record)


def validateStack(paths):
    """Check that predictor rasters are single-band, share one grid and hold finite data.

    Collects every problem before raising, so the user sees all of them at once.
    """
    if not paths:
        raise ValueError("No GeoTIFF predictors found in weightedInputs/")
    problems = []
    reference = None
    for path in paths:
        try:
            with rasterio.open(path) as source:
                if source.count != 1 or not source.crs or source.width < 1 or source.height < 1:
                    raise ValueError("Expected one-band raster with valid CRS and dimensions")
                grid = (source.crs, source.transform, source.width, source.height)
                if reference is None:
                    reference = grid
                elif grid != reference:
                    raise ValueError("CRS, resolution, bounds or grid alignment differs")
                values = source.read(1, masked=True)
                if values.count() == 0 or not np.isfinite(values.compressed()).all():
                    raise ValueError("Raster contains no usable finite data, or contains infinities")
        except Exception as exc:
            problems.append(f"{Path(path).name}: {exc}")
    if problems:
        raise ValueError("Predictor validation failed:\n" + "\n".join(problems))


def rasterRange(path) -> dict:
    """Return the minimum and maximum of a raster's valid cells."""
    with rasterio.open(path) as source:
        values = source.read(1, masked=True).compressed()
    if not len(values):
        raise ValueError("Raster contains no usable cells")
    return {"min": float(values.min()), "max": float(values.max())}


def vectorSummary(path, maxDistinct=500) -> dict:
    """List a vector layer's fields and, for each field with few distinct values, the value counts.

    Values are converted to text exactly as the weighting and modelling code
    converts them, so class names shown to the researcher always match.
    """
    frame = gpd.read_file(path)
    fields = [column for column in frame.columns if column != frame.geometry.name]
    counts = {}
    for field in fields:
        values = frame[field].dropna().astype(str)
        if values.nunique() <= maxDistinct:
            counts[field] = {str(name): int(count) for name, count in sorted(values.value_counts().items())}
    return {"fields": fields, "counts": counts, "geometryTypes": sorted(frame.geom_type.dropna().unique().tolist())}


def classIndices(values, edges=None, isAspect=False):
    """Convert raster values to integer class indices.

    Aspect uses eight compass sectors, with north spanning 337.5–360 and
    0–22.5 degrees. Other rasters are binned by the supplied class edges;
    values outside the edges fall into the first or last class.
    """
    if isAspect:
        halfSector = ASPECT_SECTOR_DEGREES / 2
        return (((values + halfSector) % 360) // ASPECT_SECTOR_DEGREES).astype("int16")
    if edges is None or len(edges) < 2:
        raise ValueError("At least two class boundaries required")
    return np.clip(np.digitize(values, edges[1:-1]), 0, len(edges) - 2)


def writeRasterWeights(sourcePath: Path, targetPath: Path, weights: dict, edges=None, isAspect=False):
    """Write a float32 raster in which every cell holds its class's researcher-approved weight.

    Cells that were nodata in the source stay nodata; a weight of zero is kept as valid.
    """
    if targetPath.exists():
        raise FileExistsError(targetPath)
    validateWeights(weights)
    with rasterio.open(sourcePath) as source:
        values = source.read(1, masked=True)
        classes = classIndices(values.filled(0), edges, isAspect)
        validCells = ~np.ma.getmaskarray(values)
        missing = set(np.unique(classes[validCells]).tolist()) - set(weights)
        if missing:
            raise ValueError(f"Missing weights for classes: {missing}")
        output = np.full(values.shape, NODATA_VALUE, dtype="float32")
        for classIndex, weight in weights.items():
            output[validCells & (classes == classIndex)] = float(weight)
        profile = source.profile.copy()
        profile.update(dtype="float32", nodata=NODATA_VALUE, compress="deflate")
        with rasterio.open(targetPath, "w", **profile) as target:
            target.write(output, 1)
            target.update_tags(**{WEIGHTS_TAG: weightsTag(weights, edges)})
    return targetPath


def writeVectorWeights(vectorPath: Path, fieldName: str, referencePath: Path, targetPath: Path, weights: dict):
    """Rasterise one categorical vector attribute onto the DEM grid using approved weights.

    Features with an empty value in the field are left as nodata. Polygons
    must not overlap, otherwise a cell would have two weights.
    """
    if targetPath.exists():
        raise FileExistsError(targetPath)
    validateWeights(weights)
    frame = gpd.read_file(vectorPath)
    if fieldName not in frame or frame.crs is None or frame.empty:
        raise ValueError("Vector field, CRS or geometry missing")
    frame = frame.loc[frame[fieldName].notna()]
    with rasterio.open(referencePath) as reference:
        frame = frame.to_crs(reference.crs)
        categories = frame[fieldName].astype(str)
        unweighted = set(categories) - set(weights)
        if unweighted:
            raise ValueError(f"Unweighted classes: {unweighted}")
        shapes = [
            (geometry, float(weights[category]))
            for geometry, category in zip(frame.geometry, categories)
            if geometry is not None and not geometry.is_empty
        ]
        coverage = rasterize(
            ((geometry, 1) for geometry, _ in shapes), out_shape=reference.shape,
            transform=reference.transform, fill=0, merge_alg=MergeAlg.add,
        )
        if np.any(coverage > 1):
            raise ValueError("Overlapping vector polygons: resolve overlaps before rasterising")
        output = rasterize(
            shapes, out_shape=reference.shape, transform=reference.transform,
            fill=NODATA_VALUE, dtype="float32",
        )
        profile = reference.profile.copy()
        profile.update(dtype="float32", nodata=NODATA_VALUE, compress="deflate")
        with rasterio.open(targetPath, "w", **profile) as target:
            target.write(output, 1)
            target.update_tags(**{WEIGHTS_TAG: weightsTag(weights)})
    return targetPath


def writeVectorCategories(vectorPath: Path, fieldName: str, referencePath: Path, targetPath: Path,
                          lookupPath: Path) -> dict:
    """Rasterise a text (or any) vector field as class codes on the DEM grid.

    Every distinct value gets one whole-number code, in alphabetical order
    from 0, so separate polygons of the same class share a code. Features
    with an empty value are left as nodata (-1). The code -> name lookup is
    written to lookupPath as CSV and stored in the raster's metadata.
    Returns the lookup as {code: name}.
    """
    if targetPath.exists():
        raise FileExistsError(targetPath)
    if lookupPath.exists():
        raise FileExistsError(lookupPath)
    frame = gpd.read_file(vectorPath)
    if fieldName not in frame or frame.crs is None or frame.empty:
        raise ValueError("Vector field, CRS or geometry missing")
    frame = frame.loc[frame[fieldName].notna()]
    names = sorted(frame[fieldName].astype(str).unique())
    if not names:
        raise ValueError(f"Field {fieldName} has no values")
    codes = {name: index for index, name in enumerate(names)}
    with rasterio.open(referencePath) as reference:
        frame = frame.to_crs(reference.crs)
        shapes = [
            (geometry, codes[str(name)])
            for geometry, name in zip(frame.geometry, frame[fieldName])
            if geometry is not None and not geometry.is_empty
        ]
        coverage = rasterize(
            ((geometry, 1) for geometry, _ in shapes), out_shape=reference.shape,
            transform=reference.transform, fill=0, merge_alg=MergeAlg.add,
        )
        if np.any(coverage > 1):
            raise ValueError("Overlapping vector polygons: resolve overlaps before rasterising")
        output = rasterize(shapes, out_shape=reference.shape, transform=reference.transform,
                           fill=CATEGORY_NODATA, dtype="int16")
        profile = reference.profile.copy()
    profile.update(driver="GTiff", count=1, dtype="int16", nodata=CATEGORY_NODATA, compress="deflate")
    lookup = {code: name for name, code in codes.items()}
    with rasterio.open(targetPath, "w", **profile) as target:
        target.write(output, 1)
        target.update_tags(**{CATEGORIES_TAG: json.dumps({str(code): name for code, name in lookup.items()})})
    with open(lookupPath, "w", newline="", encoding="utf-8") as table:
        writer = csv.writer(table)
        writer.writerow(["code", fieldName])
        writer.writerows(lookup.items())
    return lookup


def readPolygons(path) -> gpd.GeoDataFrame:
    """Read a polygon layer and check that it has a CRS and only polygon geometry."""
    frame = gpd.read_file(path)
    if frame.crs is None:
        raise ValueError(f"{Path(path).name}: polygon layer requires a CRS")
    if frame.empty:
        raise ValueError(f"{Path(path).name}: polygon layer is empty")
    if not frame.geom_type.isin(POLYGON_TYPES).all():
        raise ValueError(f"{Path(path).name}: layer must contain only polygons")
    return frame


def rasterAreaPolygons(path) -> tuple:
    """Turn a GeoTIFF into the geographical-area polygon: the outline of its cells that count as inside.

    A cell is inside when it has a value (is not nodata). If the raster holds
    only the values 0 and 1, as a mask raster does, cells with 0 are outside
    too. The outline follows the cell edges exactly, in the raster's CRS.
    Returns (one-row GeoDataFrame, a sentence describing the rule used).
    The raster is read in blocks, so large rasters do not need much memory.
    """
    with rasterio.open(path) as source:
        if source.count != 1:
            raise ValueError(f"{Path(path).name}: the area raster must have one band, not {source.count}")
        if source.crs is None:
            raise ValueError(f"{Path(path).name}: the area raster has no CRS")
        windows = [Window(0, rowStart, source.width, min(AREA_BLOCK_ROWS, source.height - rowStart))
                   for rowStart in range(0, source.height, AREA_BLOCK_ROWS)]

        def validBlocks():
            """Yield (window, values, cells with a finite value) block by block."""
            for window in windows:
                block = source.read(1, window=window, masked=True)
                data = block.data.astype("float64")
                yield window, data, ~np.ma.getmaskarray(block) & np.isfinite(data)

        isMask = True
        anyValid = False
        for _, data, valid in validBlocks():
            anyValid = anyValid or bool(valid.any())
            if isMask and not np.isin(data[valid], (0, 1)).all():
                isMask = False
        if not anyValid:
            raise ValueError(f"{Path(path).name}: the area raster has no cells with a value")
        inside = np.zeros((source.height, source.width), dtype="uint8")
        for window, data, valid in validBlocks():
            rows = slice(int(window.row_off), int(window.row_off + window.height))
            inside[rows] = (valid & (data == 1)) if isMask else valid
        if not inside.any():
            raise ValueError(f"{Path(path).name}: the area mask has no cells with the value 1")
        pieces = [shape(geometry) for geometry, value in shapes(inside, mask=inside.astype(bool),
                                                                  transform=source.transform) if value == 1]
        crs, hasNodata = source.crs, source.nodata is not None
    outline = unary_union(pieces)
    if isMask:
        rule = "cells with the value 1 are inside the area; cells with 0 or no value are outside"
    elif hasNodata:
        rule = "cells with a value are inside the area; nodata cells are outside"
    else:
        rule = ("the raster has no nodata value, so all of its cells count as inside the area "
                "(the whole raster rectangle)")
    frame = gpd.GeoDataFrame({"source": [Path(path).name], "rule": [rule]}, geometry=[outline], crs=crs)
    return frame, rule


def percentShares(counts) -> np.ndarray:
    """Turn an array of counts into percentages of their total (all zeros if the total is zero)."""
    counts = np.asarray(counts, dtype=float)
    total = counts.sum()
    return counts / total * 100 if total else np.zeros_like(counts)


def rasterShares(rasterPath, areaPath, sitePoints, subAreaPath=None, binCount=DEFAULT_BIN_COUNT) -> dict:
    """Compare how raster values are distributed across the area, an optional sub-area and the sites.

    Rasters with few distinct values (such as weighted inputs) are compared
    value by value; continuous rasters are split into equal-width bins over
    the geographical area. Returns {'labels': [...], 'series': {name: percents}}.
    """
    with rasterio.open(rasterPath) as source:
        values = source.read(1, masked=True)
        validCells = ~np.ma.getmaskarray(values)
        area = readPolygons(areaPath).to_crs(source.crs)
        areaMask = geometry_mask(area.geometry, out_shape=values.shape,
                                 transform=source.transform, invert=True)
        areaValues = values.data[validCells & areaMask]
        if not len(areaValues):
            raise ValueError("The raster has no valid cells inside the geographical area")
        subAreaValues = None
        if subAreaPath:
            subArea = readPolygons(subAreaPath).to_crs(source.crs)
            subAreaMask = geometry_mask(subArea.geometry, out_shape=values.shape,
                                        transform=source.transform, invert=True)
            subAreaValues = values.data[validCells & subAreaMask]
            if not len(subAreaValues):
                raise ValueError("The raster has no valid cells inside the sub-area")
        points = sitePoints.to_crs(source.crs).geometry
        sampled = np.array([
            np.nan if np.ma.is_masked(sample[0]) else float(sample[0])
            for sample in source.sample([(point.x, point.y) for point in points], masked=True)
        ], dtype=float)
        siteValues = sampled[np.isfinite(sampled)]

    distinct = np.unique(areaValues)
    if len(distinct) <= MAX_DISCRETE_VALUES:
        labels = [f"{value:g}" for value in distinct]

        def countValues(sample):
            """Count how many entries of sample equal each distinct area value."""
            return [np.count_nonzero(sample == value) for value in distinct]
    else:
        edges = np.linspace(areaValues.min(), areaValues.max(), binCount + 1)
        labels = [f"{low:.4g}–{high:.4g}" for low, high in zip(edges[:-1], edges[1:])]

        def countValues(sample):
            """Count how many entries of sample fall into each equal-width bin."""
            return np.histogram(np.clip(sample, edges[0], edges[-1]), bins=edges)[0]

    series = {"Geographical area": percentShares(countValues(areaValues))}
    if subAreaValues is not None:
        series["Sub-area"] = percentShares(countValues(subAreaValues))
    series["Sites"] = percentShares(countValues(siteValues))
    return {"labels": labels, "series": series}


def vectorShares(vectorPath, fieldName, areaPath, sitePoints, subAreaPath=None) -> dict:
    """Compare a categorical vector field across the area, an optional sub-area and the sites.

    Area and sub-area shares are percentages of polygon area; site shares are
    percentages of sites falling in each class. Returns the same structure
    as rasterShares.
    """
    polygons = readPolygons(vectorPath)
    if fieldName not in polygons:
        raise ValueError(f"Field {fieldName} not found")
    if polygons.crs.is_geographic:
        polygons = polygons.to_crs(polygons.estimate_utm_crs())
    polygons = polygons[[fieldName, "geometry"]].copy()
    polygons[fieldName] = polygons[fieldName].fillna(UNCLASSIFIED_LABEL).astype(str)

    def areaByClass(boundaryPath):
        """Sum polygon area per class inside the boundary polygons."""
        boundary = readPolygons(boundaryPath).to_crs(polygons.crs)
        clipped = gpd.clip(polygons, boundary)
        return clipped.assign(areaSize=clipped.geometry.area).groupby(fieldName)["areaSize"].sum()

    areaTotals = areaByClass(areaPath)

    # Join geometry only, so a column shared by sites and polygons is not renamed with _left/_right.
    sites = sitePoints[["geometry"]].to_crs(polygons.crs)
    joined = gpd.sjoin(sites, polygons, predicate="within", how="left")

    if joined.index.duplicated().any():
        raise ValueError("Overlapping polygons cause ambiguous site counts")
    siteCounts = joined[fieldName].fillna(UNCLASSIFIED_LABEL).value_counts()
    subAreaTotals = areaByClass(subAreaPath) if subAreaPath else None

    labels = sorted(set(areaTotals.index) | set(siteCounts.index))
    series = {"Geographical area": percentShares(areaTotals.reindex(labels, fill_value=0))}
    if subAreaTotals is not None:
        series["Sub-area"] = percentShares(subAreaTotals.reindex(labels, fill_value=0))
    series["Sites"] = percentShares(siteCounts.reindex(labels, fill_value=0))
    return {"labels": labels, "series": series}
