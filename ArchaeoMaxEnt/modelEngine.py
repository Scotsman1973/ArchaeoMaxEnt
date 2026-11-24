"""MaxEnt model execution: predictor discovery, leave-one-out or spatial k-fold
validation, and the prediction map."""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import elapid
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import xy as cellCentres
from rasterio.windows import Window
from rasterio.windows import transform as windowTransform
from sklearn.metrics import roc_auc_score

from .geoProcessing import CATEGORIES_TAG, NODATA_VALUE, WEIGHTS_TAG, readPolygons, validateStack
from .projectCore import (
    DERIVED_INPUTS_FOLDER, MODELLING_OUTPUTS_FOLDER, RAW_INPUTS_FOLDER, WEIGHTED_INPUTS_FOLDER, safeName,
)
from .shared import (
    PREDICTOR_CATEGORICAL, PREDICTOR_CIRCULAR, PREDICTOR_NUMERIC, PREDICTOR_TYPES, VALIDATION_LEAVE_ONE_OUT,
    VALIDATION_SPATIAL_K_FOLD, WEIGHTED_SOURCE,
)

PARTIAL_SUFFIX = "_partial.tif"
OUTPUT_TRANSFORM = "cloglog"
PREDICTION_FILE_NAME = "prediction.tif"
HEATMAP_FILE_NAME = "prediction.jpg"
HEATMAP_COLOUR_MAP = "GnBu"
HEATMAP_MAX_PIXELS = 2000
HEATMAP_DPI = 150
HEATMAP_QUALITY = 90
EVALUATION_FILE_NAME = "evaluation.json"
BACKGROUND_FILE_NAME = "backgroundPoints.gpkg"
MIN_BACKGROUND_POINTS = 10
# A run is written into "<name>.partial" and renamed when complete, so a run that is
# cancelled or crashes part-way never looks like a finished one.
PARTIAL_RUN_SUFFIX = ".partial"
RENAME_ATTEMPTS = 10
RENAME_WAIT_SECONDS = 0.5
REFERENCE_RASTER_NAME = "elevation.tif"
VALIDATION_DESCRIPTIONS = {
    VALIDATION_LEAVE_ONE_OUT: "Leave-one-out (each site held out once); fixed random 25% of background held out for testing",
    VALIDATION_SPATIAL_K_FOLD: "GeographicKFold (presence); random background folds",
}
VALIDATION_SHORT_NAMES = {
    VALIDATION_LEAVE_ONE_OUT: "leave-one-out",
    VALIDATION_SPATIAL_K_FOLD: "spatial k-fold",
}
MIN_SITES_LEAVE_ONE_OUT = 5
MIN_SITES_K_FOLD = 8
TEST_BACKGROUND_SHARE = 0.25
# Folders searched for predictors, with the source name shown to the researcher.
PREDICTOR_SOURCES = (
    (WEIGHTED_INPUTS_FOLDER, WEIGHTED_SOURCE),
    (DERIVED_INPUTS_FOLDER, "derived"),
    (RAW_INPUTS_FOLDER, "raw"),
)
MAX_CATEGORY_COUNT = 50
PREDICTION_BLOCK_ROWS = 256


@dataclass
class Predictor:
    """One candidate predictor raster found in the project, and how it is to be used."""

    path: Path
    source: str
    label: str
    suggestedType: str = PREDICTOR_NUMERIC
    problem: str = ""
    predictorType: str = PREDICTOR_NUMERIC

    @property
    def valid(self) -> bool:
        """True when the raster can be used for modelling."""
        return not self.problem

    @property
    def displayName(self) -> str:
        """Folder and file name, as shown in the predictor table and evaluation.json."""
        return f"{self.path.parent.name}/{self.path.name}"


def predictorLabel(source: str, path: Path) -> str:
    """Return a unique camelCase column name for a predictor, e.g. 'weightedElevation'."""
    words = re.findall(r"[A-Za-z0-9]+", path.stem)
    return source + "".join(word[0].upper() + word[1:] for word in words)


def gridOf(source) -> tuple:
    """Return the CRS, transform and size that define an open raster's grid."""
    return source.crs, source.transform, source.width, source.height


def inspectPredictor(path: Path, source: str, referenceGrid) -> tuple:
    """Check one raster and suggest how it should be used.

    Weighted rasters hold 0-9 weights, so they are always suggested as
    numeric; other rasters named 'aspect' are suggested as circular, and
    integer rasters with few distinct values as categorical.
    Returns (problem, suggestedType); problem is '' when the raster is usable.
    """
    if path.name.endswith(PARTIAL_SUFFIX):
        return "incomplete file", PREDICTOR_NUMERIC
    isWeighted = source == WEIGHTED_SOURCE
    isAspect = not isWeighted and "aspect" in path.stem.lower()
    suggestedType = PREDICTOR_CIRCULAR if isAspect else PREDICTOR_NUMERIC
    try:
        with rasterio.open(path) as raster:
            if raster.count != 1:
                return f"has {raster.count} bands; one is required", suggestedType
            if not raster.crs:
                return "has no CRS", suggestedType
            if referenceGrid is not None and gridOf(raster) != referenceGrid:
                return "grid differs from the elevation DEM (CRS, extent or pixel size)", suggestedType
            values = raster.read(1, masked=True).compressed()
            if not len(values) or not np.isfinite(values).all():
                return "no usable values, or contains infinities", suggestedType
            if np.ptp(values) == 0:
                return f"has the same value ({values[0]:g}) everywhere, so it cannot inform the model", suggestedType
            isInteger = np.issubdtype(raster.dtypes[0], np.integer)
            if (suggestedType == PREDICTOR_NUMERIC and not isWeighted and isInteger
                    and len(np.unique(values)) <= MAX_CATEGORY_COUNT):
                suggestedType = PREDICTOR_CATEGORICAL
    except Exception as exc:
        return f"cannot be read: {exc}", suggestedType
    return "", suggestedType


def discoverPredictors(project) -> list:
    """List every GeoTIFF in the project's input folders as a candidate predictor.

    Rasters are checked against the grid of the imported elevation DEM (or the
    first raster found if there is no DEM). Each predictor starts with its
    suggested type: aspect as circular, integer rasters with few distinct
    values as categorical, everything else as numeric.
    """
    candidates = []
    for folderName, source in PREDICTOR_SOURCES:
        folder = project.folder(folderName)
        for path in sorted([*folder.glob("*.tif"), *folder.glob("*.tiff")]):
            candidates.append((path, source))
    if not candidates:
        return []

    referencePath = project.folder(RAW_INPUTS_FOLDER) / REFERENCE_RASTER_NAME
    if not referencePath.exists():
        referencePath = candidates[0][0]
    try:
        with rasterio.open(referencePath) as reference:
            referenceGrid = gridOf(reference)
    except Exception:
        referenceGrid = None

    predictors = []
    for path, source in candidates:
        problem, suggestedType = inspectPredictor(path, source, referenceGrid)
        predictors.append(Predictor(path=path, source=source, label=predictorLabel(source, path),
                                    suggestedType=suggestedType, problem=problem,
                                    predictorType=suggestedType))
    return predictors


def readJsonTag(path: Path, tagName: str):
    """Return a JSON record stored in a raster's metadata tag, or None if it has none."""
    with rasterio.open(path) as source:
        text = source.tags().get(tagName)
    return json.loads(text) if text else None


def annotateWithPredictors(points, predictors):
    """Attach raw predictor values to points, dropping points that fall on nodata.

    Only the point locations are passed to elapid: its drop_na removes a row
    if any column is empty, so an empty attribute field (such as a blank
    notes column) would otherwise discard sites that have valid raster values.
    """
    if isinstance(points, gpd.GeoDataFrame):
        points = points[["geometry"]]
    return elapid.annotate(points, [str(predictor.path) for predictor in predictors],
                           labels=[predictor.label for predictor in predictors],
                           drop_na=True, quiet=True)


def buildFeatures(values: pd.DataFrame, predictors) -> tuple:
    """Turn raw predictor values into the model's feature table.

    Numeric predictors are used as they are; circular predictors (aspect, in
    degrees) become a northness (cosine) and an eastness (sine) column;
    categorical predictors become whole-number class codes. Returns the
    feature table and the positions of its categorical columns.
    """
    columns = {}
    categoricalNames = []
    for predictor in predictors:
        column = values[predictor.label].astype(float)
        if predictor.predictorType == PREDICTOR_CIRCULAR:
            radians = np.deg2rad(column)
            columns[f"{predictor.label}North"] = np.cos(radians)
            columns[f"{predictor.label}East"] = np.sin(radians)
        elif predictor.predictorType == PREDICTOR_CATEGORICAL:
            columns[predictor.label] = column.round().astype(int)
            categoricalNames.append(predictor.label)
        else:
            columns[predictor.label] = column
    features = pd.DataFrame(columns).reset_index(drop=True)
    categoricalIndexes = [features.columns.get_loc(name) for name in categoricalNames]
    return features, categoricalIndexes


def stackFeatures(presenceFeatures, backgroundFeatures):
    """Combine presence and background feature tables and return (features, labels: 1 = site)."""
    features = pd.concat([presenceFeatures, backgroundFeatures], ignore_index=True)
    labels = np.concatenate([np.ones(len(presenceFeatures)), np.zeros(len(backgroundFeatures))])
    return features, labels


def fitMaxent(features, labels, betaMultiplier, categoricalIndexes):
    """Fit and return one MaxEnt model, telling it which columns are categorical."""
    model = elapid.MaxentModel(beta_multiplier=float(betaMultiplier), transform=OUTPUT_TRANSFORM)
    model.fit(features, labels, categorical=categoricalIndexes or None)
    return model


def leaveOneOutAucs(presenceFeatures, backgroundFeatures, betaMultiplier, seed, categoricalIndexes,
                    onFit=None) -> list:
    """Hold out each site in turn and score it against held-out background points.

    A fixed random share of the background is kept out of every training run.
    Each model is trained on all other sites plus the training background,
    and the result for the held-out site is the share of test background
    points it outscores (its AUC, with ties counted as half).
    onFit(done, total), if given, is called after each model is fitted.
    """
    order = np.random.default_rng(seed).permutation(len(backgroundFeatures))
    testCount = max(1, int(len(backgroundFeatures) * TEST_BACKGROUND_SHARE))
    backgroundTest = backgroundFeatures.iloc[order[:testCount]]
    backgroundTrain = backgroundFeatures.iloc[order[testCount:]]
    aucs = []
    for siteIndex in range(len(presenceFeatures)):
        trainPresence = presenceFeatures.drop(presenceFeatures.index[siteIndex])
        model = fitMaxent(*stackFeatures(trainPresence, backgroundTrain), betaMultiplier, categoricalIndexes)
        siteScore = model.predict(presenceFeatures.iloc[[siteIndex]])[0]
        backgroundScores = model.predict(backgroundTest)
        auc = (np.sum(backgroundScores < siteScore) + 0.5 * np.sum(backgroundScores == siteScore)) / len(backgroundScores)
        aucs.append(float(auc))
        if onFit:
            onFit(siteIndex + 1, len(presenceFeatures))
    return aucs


def spatialKFoldAucs(presence, presenceFeatures, backgroundFeatures, foldCount, betaMultiplier, seed,
                     categoricalIndexes, onFit=None) -> list:
    """Split sites into spatial clusters and background into random folds; return one AUC per fold."""
    folds = elapid.GeographicKFold(n_splits=foldCount, random_state=seed)
    order = np.random.default_rng(seed).permutation(len(backgroundFeatures))
    shuffledBackground = backgroundFeatures.iloc[order].reset_index(drop=True)
    aucs = []
    for foldIndex, (trainIndex, testIndex) in enumerate(folds.split(presence)):
        backgroundTest = shuffledBackground.iloc[foldIndex::foldCount]
        backgroundTrain = shuffledBackground.drop(shuffledBackground.index[foldIndex::foldCount])
        model = fitMaxent(*stackFeatures(presenceFeatures.iloc[trainIndex], backgroundTrain),
                          betaMultiplier, categoricalIndexes)
        testFeatures, testLabels = stackFeatures(presenceFeatures.iloc[testIndex], backgroundTest)
        aucs.append(float(roc_auc_score(testLabels, model.predict(testFeatures))))
        if onFit:
            onFit(foldIndex + 1, foldCount)
    return aucs


def backgroundBlocks(sources, areaGeometries):
    """Yield (first row, cell mask) for each block of rows of the predictor grid.

    A cell is True when every predictor has a finite value there and the
    cell's centre lies inside the geographical area polygons.
    """
    height, width = sources[0].height, sources[0].width
    for rowStart in range(0, height, PREDICTION_BLOCK_ROWS):
        window = Window(0, rowStart, width, min(PREDICTION_BLOCK_ROWS, height - rowStart))
        mask = geometry_mask(areaGeometries, out_shape=(int(window.height), width),
                             transform=windowTransform(window, sources[0].transform), invert=True)
        for source in sources:
            block = source.read(1, window=window, masked=True)
            mask &= ~np.ma.getmaskarray(block) & np.isfinite(block.data.astype("float64"))
        yield rowStart, mask


def sampleBackground(predictors, areaPath, count: int, seed: int) -> tuple:
    """Draw background points from the geographical area only.

    Candidate cells are those inside the area polygons (cell centre inside)
    where every predictor has a value, so each point drawn is usable. Cells
    are drawn without repeats; if the area has fewer candidate cells than
    requested, every one of them is used. Each point sits at its cell's
    centre. The rasters are read in blocks, twice (count, then pick), so
    large DEMs never have to fit in memory at once.
    Returns (points GeoDataFrame in the predictors' CRS, number of candidate cells).
    """
    sources = [rasterio.open(predictor.path) for predictor in predictors]
    try:
        crs, gridTransform = sources[0].crs, sources[0].transform
        areaGeometries = [geometry for geometry in readPolygons(areaPath).to_crs(crs).geometry
                          if geometry is not None and not geometry.is_empty]
        if not areaGeometries:
            raise ValueError("The geographical area layer has no usable polygon geometry")
        blockCounts = [int(mask.sum()) for _, mask in backgroundBlocks(sources, areaGeometries)]
        available = sum(blockCounts)
        if available == 0:
            raise ValueError("No cell inside the geographical area has a value in every predictor: check that "
                             "the area polygon overlaps the DEM and the predictor rasters")
        rng = np.random.default_rng(seed)
        chosen = np.sort(rng.choice(available, size=min(count, available), replace=False))
        rows, cols = [], []
        offset = 0
        for (rowStart, mask), blockCount in zip(backgroundBlocks(sources, areaGeometries), blockCounts):
            picked = chosen[(chosen >= offset) & (chosen < offset + blockCount)] - offset
            if len(picked):
                blockRows, blockCols = np.nonzero(mask)
                rows.append(blockRows[picked] + rowStart)
                cols.append(blockCols[picked])
            offset += blockCount
    finally:
        for source in sources:
            source.close()
    xs, ys = cellCentres(gridTransform, np.concatenate(rows), np.concatenate(cols))
    points = gpd.GeoDataFrame(geometry=gpd.points_from_xy(np.asarray(xs), np.asarray(ys)), crs=crs)
    return points, available


def writePredictionMap(model, predictors, targetPath: Path):
    """Apply the model to every cell where all predictors have values, block by block.

    Uses the same feature conversion as training, so circular and categorical
    predictors are handled consistently. Cells missing any predictor are nodata.
    """
    sources = [rasterio.open(predictor.path) for predictor in predictors]
    try:
        profile = sources[0].profile.copy()
        profile.update(driver="GTiff", count=1, dtype="float32", nodata=NODATA_VALUE, compress="deflate")
        height, width = sources[0].height, sources[0].width
        with rasterio.open(targetPath, "w", **profile) as target:
            for rowStart in range(0, height, PREDICTION_BLOCK_ROWS):
                window = Window(0, rowStart, width, min(PREDICTION_BLOCK_ROWS, height - rowStart))
                blocks = [source.read(1, window=window, masked=True) for source in sources]
                validCells = np.ones(blocks[0].shape, dtype=bool)
                for block in blocks:
                    validCells &= ~np.ma.getmaskarray(block) & np.isfinite(block.data.astype("float64"))
                output = np.full(blocks[0].shape, NODATA_VALUE, dtype="float32")
                if validCells.any():
                    values = pd.DataFrame({predictor.label: block.data[validCells]
                                           for predictor, block in zip(predictors, blocks)})
                    features, _ = buildFeatures(values, predictors)
                    output[validCells] = model.predict(features).astype("float32")
                target.write(output, 1, window=window)
    finally:
        for source in sources:
            source.close()


def writeHeatmap(predictionPath: Path, sitePoints, heatmapPath: Path, title: str):
    """Save the prediction map as a JPEG heatmap with the sites marked on it.

    Suitability is drawn on a fixed 0-1 scale (GnBu, as in archaeo_modelling)
    so heatmaps from different runs can be compared by eye. Large rasters are
    resampled for the picture only; prediction.tif keeps full resolution.
    """
    with rasterio.open(predictionPath) as source:
        scale = max(1, int(np.ceil(max(source.width, source.height) / HEATMAP_MAX_PIXELS)))
        values = source.read(1, masked=True, out_shape=(source.height // scale or 1, source.width // scale or 1),
                             resampling=Resampling.average)
        bounds, crs = source.bounds, source.crs
    width = bounds.right - bounds.left
    height = bounds.top - bounds.bottom
    figure = Figure(figsize=(10, max(4, min(14, 10 * height / width + 1))))
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)
    image = axes.imshow(values, cmap=HEATMAP_COLOUR_MAP, vmin=0, vmax=1,
                        extent=(bounds.left, bounds.right, bounds.bottom, bounds.top))
    points = sitePoints.to_crs(crs)
    axes.scatter(points.geometry.x, points.geometry.y, s=36, c="#d7301f", edgecolors="white",
                 linewidths=0.8, label=f"Sites ({len(points)})", zorder=3)
    axes.set_title(title, fontsize=12)
    axes.set_xlabel("Easting")
    axes.set_ylabel("Northing")
    axes.ticklabel_format(style="plain", useOffset=False)
    axes.legend(loc="lower right")
    figure.colorbar(image, ax=axes, shrink=0.8, label="Relative suitability (0–1)")
    figure.tight_layout()
    figure.savefig(heatmapPath, dpi=HEATMAP_DPI, format="jpeg", bbox_inches="tight",
                   pil_kwargs={"quality": HEATMAP_QUALITY})


def nextRunDir(outputDir: Path, category) -> Path:
    """Return the next free numbered output folder name for a category, e.g. hillfortRun03 (not created)."""
    baseName = safeName(str(category))
    runNumber = 1
    while (outputDir / f"{baseName}Run{runNumber:02d}").exists():
        runNumber += 1
    return outputDir / f"{baseName}Run{runNumber:02d}"


def removePartialRuns(outputDir: Path):
    """Delete run folders left half-written by a cancelled or failed earlier run."""
    for folder in outputDir.glob(f"*{PARTIAL_RUN_SUFFIX}"):
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)


def finishRunDir(workDir: Path, runDir: Path):
    """Rename a completed run's folder to its final name.

    Retries for a few seconds, because on Windows a virus scanner or OneDrive
    can briefly hold a newly written file open, which blocks the rename.
    """
    for attempt in range(RENAME_ATTEMPTS):
        try:
            workDir.rename(runDir)
            return
        except PermissionError:
            if attempt == RENAME_ATTEMPTS - 1:
                raise
            time.sleep(RENAME_WAIT_SECONDS)


def runModels(project, sitesPath, areaPath, fieldName, categories, predictors, backgroundCount, seed=42,
              foldCount=4, betaMultiplier=2.0, validationMethod=VALIDATION_LEAVE_ONE_OUT, progress=None) -> list:
    """Fit and validate one MaxEnt model per archaeological category.

    Background points are drawn only from inside the geographical area
    polygons at areaPath (see sampleBackground).

    predictors are the Predictor objects the researcher included, each with
    its predictorType set. validationMethod is VALIDATION_LEAVE_ONE_OUT (the
    default) or VALIDATION_SPATIAL_K_FOLD, which uses foldCount spatial folds.
    A final model trained on all sites and background is mapped and saved,
    with an evaluation summary, in a new numbered folder for each run.
    progress(fraction, message), if given, is called as the work advances (fraction 0-1).
    Returns one result dict per category.
    """
    if validationMethod not in VALIDATION_DESCRIPTIONS:
        raise ValueError(f"Unknown validation method: {validationMethod}")
    if not predictors:
        raise ValueError("Include at least one predictor")
    for predictor in predictors:
        if predictor.predictorType not in PREDICTOR_TYPES:
            raise ValueError(f"Unknown predictor type for {predictor.displayName}: {predictor.predictorType}")
        project.log.info("Predictor: %s as %s", predictor.displayName, predictor.predictorType)
    validateStack([predictor.path for predictor in predictors])

    sites = gpd.read_file(sitesPath)
    if fieldName not in sites or sites.crs is None:
        raise ValueError("Archaeological field or CRS missing")
    if not categories:
        raise ValueError("Select at least one archaeological category")

    # Background points come only from inside the geographical area; the seed makes runs repeatable.
    backgroundCount = int(backgroundCount)
    backgroundPoints, availableCells = sampleBackground(predictors, areaPath, backgroundCount, seed)
    project.log.info("Geographical area: %d cells have a value in every predictor", availableCells)
    if availableCells < backgroundCount:
        project.log.warning("Only %d cells are available inside the geographical area, fewer than the %d "
                            "background samples requested: every available cell is used", availableCells,
                            backgroundCount)
    background = annotateWithPredictors(backgroundPoints, predictors)
    backgroundFeatures, categoricalIndexes = buildFeatures(background, predictors)
    project.log.info("Background points requested: %d, drawn inside the geographical area: %d, usable: %d",
                     backgroundCount, len(backgroundPoints), len(background))
    if len(background) < MIN_BACKGROUND_POINTS:
        raise ValueError(f"Only {len(background)} background points could be drawn inside the geographical "
                         f"area (at least {MIN_BACKGROUND_POINTS} needed)")
    areaOutline = readPolygons(areaPath).to_crs(backgroundPoints.crs).geometry.union_all()

    predictorRecords = [
        {
            "file": predictor.displayName,
            "source": predictor.source,
            "type": predictor.predictorType,
            "weights": readJsonTag(predictor.path, WEIGHTS_TAG) if predictor.source == WEIGHTED_SOURCE else None,
            "categories": (readJsonTag(predictor.path, CATEGORIES_TAG)
                           if predictor.predictorType == PREDICTOR_CATEGORICAL else None),
        }
        for predictor in predictors
    ]

    outputDir = project.folder(MODELLING_OUTPUTS_FOLDER)
    removePartialRuns(outputDir)
    results = []
    def report(categoryIndex, done, total, message):
        """Pass overall progress across all categories to the progress callback."""
        if progress:
            progress((categoryIndex + done / max(total, 1)) / len(categories), message)

    for categoryIndex, category in enumerate(categories):
        presence = sites.loc[sites[fieldName].astype(str) == str(category)].copy()
        if presence.geometry.isna().any() or not presence.geom_type.eq("Point").all():
            raise ValueError("Site observations must have valid point geometry")
        siteCount = len(presence)
        sitePoints = presence.to_crs(backgroundPoints.crs)
        sitesOutsideArea = int((~sitePoints.geometry.intersects(areaOutline)).sum())
        if sitesOutsideArea:
            project.log.warning("Category %s: %d of %d sites lie outside the geographical area; they are still "
                                "used as sites, but the background (the available environment) is drawn only "
                                "from inside the area", category, sitesOutsideArea, siteCount)
        presence = annotateWithPredictors(sitePoints, predictors)
        presence = presence.reset_index(drop=True)
        presenceFeatures, _ = buildFeatures(presence, predictors)
        project.log.info("Category %s: %d sites, %d with values in every predictor",
                         category, siteCount, len(presence))

        if validationMethod == VALIDATION_LEAVE_ONE_OUT:
            minimumSites = MIN_SITES_LEAVE_ONE_OUT
        else:
            minimumSites = max(MIN_SITES_K_FOLD, foldCount * 2)
        if len(presence) < minimumSites:
            raise ValueError(f"Not enough usable sites for {category}: {len(presence)} "
                             f"(at least {minimumSites} needed)")

        # Validation fits are most of the work; the final model and map are the last step.
        def onFit(done, total, categoryIndex=categoryIndex, category=category):
            """Report one finished validation fit."""
            report(categoryIndex, done, total + 1, f"{category}: validation model {done} of {total}")

        if validationMethod == VALIDATION_LEAVE_ONE_OUT:
            validationAucs = leaveOneOutAucs(presenceFeatures, backgroundFeatures, betaMultiplier,
                                             seed, categoricalIndexes, onFit=onFit)
        else:
            validationAucs = spatialKFoldAucs(presence, presenceFeatures, backgroundFeatures, foldCount,
                                              betaMultiplier, seed, categoricalIndexes, onFit=onFit)
        for index, score in enumerate(validationAucs, start=1):
            project.log.info("Category %s validation run %d AUC %.4f", category, index, score)
        meanAuc = float(np.mean(validationAucs))

        finalModel = fitMaxent(*stackFeatures(presenceFeatures, backgroundFeatures), betaMultiplier,
                               categoricalIndexes)
        runDir = nextRunDir(outputDir, category)
        workDir = runDir.with_name(runDir.name + PARTIAL_RUN_SUFFIX)
        workDir.mkdir()
        result = {
            "category": str(category),
            "run": runDir.name,
            "siteCount": siteCount,
            "sitesUsed": len(presence),
            "sitesOutsideArea": sitesOutsideArea,
            "predictors": predictorRecords,
            "validationMethod": validationMethod,
            "validation": VALIDATION_DESCRIPTIONS[validationMethod],
            "validationAucs": validationAucs,
            "meanAuc": meanAuc,
            "seed": seed,
            "folds": foldCount if validationMethod == VALIDATION_SPATIAL_K_FOLD else None,
            "backgroundCount": backgroundCount,
            "backgroundUsed": len(background),
            "backgroundArea": Path(areaPath).name,
            "backgroundCellsAvailable": availableCells,
            "betaMultiplier": betaMultiplier,
        }
        try:
            writePredictionMap(finalModel, predictors, workDir / PREDICTION_FILE_NAME)
            backgroundPoints.to_file(workDir / BACKGROUND_FILE_NAME, driver="GPKG", layer="backgroundPoints")
            writeHeatmap(workDir / PREDICTION_FILE_NAME, sitePoints, workDir / HEATMAP_FILE_NAME,
                         f"Predictive model for {category} ({runDir.name})\n"
                         f"validation mean AUC {meanAuc:.3f}, {VALIDATION_SHORT_NAMES[validationMethod]}")
            (workDir / EVALUATION_FILE_NAME).write_text(json.dumps(result, indent=2), encoding="utf-8")
            finishRunDir(workDir, runDir)
        except Exception:
            # Remove the half-written run so the folder numbering only lists complete runs.
            shutil.rmtree(workDir, ignore_errors=True)
            raise
        results.append(result)
        report(categoryIndex, 1, 1, f"{category}: finished ({runDir.name})")
        project.log.info("Finished model category %s in %s", category, runDir.name)
    return results
