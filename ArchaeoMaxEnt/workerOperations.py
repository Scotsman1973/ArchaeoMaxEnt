"""Operations the modelling runtime can perform, looked up by name.

Every operation takes emit (for 'log' and 'progress' events) plus
JSON-friendly keyword arguments, and returns a JSON-friendly result. To add
an operation, write a function and add it to OPERATIONS; nothing else
changes.
"""
from __future__ import annotations

import logging
import sys
from importlib import metadata
from pathlib import Path

from . import geoProcessing, modelEngine
from .projectCore import RAW_INPUTS_FOLDER, Project

# Where makeProject stores the imported geographical area (importVector with name 'geographic').
AREA_FILE_NAME = "geographic.gpkg"

REPORTED_PACKAGES = ("elapid", "numpy", "pandas", "geopandas", "rasterio", "pyproj", "shapely",
                     "scikit-learn", "scipy", "matplotlib")
CHART_DPI = 120
CHART_SIZE = (8, 5)
BAR_GROUP_WIDTH = 0.8
LABEL_ROTATION = 40


class EmitHandler(logging.Handler):
    """Forward project log messages to QGIS as 'log' events."""

    def __init__(self, emit):
        """Remember the event writer."""
        super().__init__(logging.INFO)
        self.emitEvent = emit

    def emit(self, record):
        """Send one log record."""
        self.emitEvent("log", message=record.getMessage())


def openProject(projectDir, emit=None) -> Project:
    """Open a project folder for logging.

    Without emit, messages go to the project's processing.log. With emit, they
    are sent to QGIS instead, which shows them on the Logs tab and writes them
    to processing.log itself (writing them here too would log every line twice).
    """
    project = Project(Path(projectDir), logToFile=emit is None)
    if emit:
        project.log.addHandler(EmitHandler(emit))
    return project


def ping(emit) -> dict:
    """Report the runtime's Python, package versions and GDAL version."""
    import rasterio

    versions = {}
    for name in REPORTED_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {"python": sys.version.split()[0], "packages": versions, "gdal": rasterio.__gdal_version__}


def vectorSummary(emit, path, maxDistinct=500) -> dict:
    """Return a vector layer's fields and value counts."""
    return geoProcessing.vectorSummary(path, maxDistinct)


def rasterRange(emit, path) -> dict:
    """Return a raster's minimum and maximum valid value."""
    return geoProcessing.rasterRange(path)


def importVector(emit, projectDir, source, name, layerName=None) -> str:
    """Copy a vector layer into the project's rawInputs/ as a GeoPackage."""
    project = openProject(projectDir)
    try:
        return str(project.importVector(source, name, layerName))
    finally:
        project.closeLog()


def importArea(emit, projectDir, source, name, layerName=None) -> dict:
    """Import the geographical area (polygon layer or GeoTIFF) as a GeoPackage; return its path and a note."""
    project = openProject(projectDir)
    try:
        target, note = project.importArea(source, name, layerName)
        return {"path": str(target), "note": note}
    finally:
        project.closeLog()


def writeRasterWeights(emit, sourcePath, targetPath, weights, edges=None, isAspect=False) -> str:
    """Write a weighted raster; weights is a list of [class index, weight] pairs."""
    weightMap = {int(classIndex): float(weight) for classIndex, weight in weights}
    geoProcessing.writeRasterWeights(Path(sourcePath), Path(targetPath), weightMap, edges, isAspect)
    return str(targetPath)


def writeVectorWeights(emit, vectorPath, fieldName, referencePath, targetPath, weights) -> str:
    """Rasterise a weighted vector field; weights is a list of [class name, weight] pairs."""
    weightMap = {str(name): float(weight) for name, weight in weights}
    geoProcessing.writeVectorWeights(Path(vectorPath), fieldName, Path(referencePath), Path(targetPath), weightMap)
    return str(targetPath)


def writeVectorCategories(emit, vectorPath, fieldName, referencePath, targetPath, lookupPath) -> dict:
    """Rasterise a vector field as class codes; return {code: class name}."""
    lookup = geoProcessing.writeVectorCategories(Path(vectorPath), fieldName, Path(referencePath),
                                                 Path(targetPath), Path(lookupPath))
    return {str(code): name for code, name in lookup.items()}


def predictorRecord(predictor) -> dict:
    """Convert a Predictor to a JSON-friendly dict."""
    return {"path": str(predictor.path), "source": predictor.source, "label": predictor.label,
            "suggestedType": predictor.suggestedType, "problem": predictor.problem,
            "displayName": predictor.displayName}


def discoverPredictors(emit, projectDir) -> list:
    """List every GeoTIFF in the project's input folders with its suggested type and any problem."""
    project = openProject(projectDir)
    try:
        return [predictorRecord(predictor) for predictor in modelEngine.discoverPredictors(project)]
    finally:
        project.closeLog()


def runModels(emit, projectDir, sitesPath, fieldName, categories, predictors, backgroundCount, seed=42,
              foldCount=4, betaMultiplier=2.0, validationMethod=modelEngine.VALIDATION_LEAVE_ONE_OUT,
              areaPath=None) -> list:
    """Fit, validate and map one MaxEnt model per category, reporting progress as it goes.

    predictors is a list of dicts with 'path', 'source', 'label' and 'predictorType'.
    Background points are drawn inside areaPath; if it is not given, the
    project's imported geographical area (rawInputs/geographic.gpkg) is used.
    """
    chosen = [modelEngine.Predictor(path=Path(item["path"]), source=item["source"], label=item["label"],
                                    predictorType=item["predictorType"]) for item in predictors]
    project = openProject(projectDir, emit)
    if not areaPath:
        areaPath = project.folder(RAW_INPUTS_FOLDER) / AREA_FILE_NAME
    try:
        if not Path(areaPath).is_file():
            raise FileNotFoundError(f"Geographical area not found: {areaPath}")
        return modelEngine.runModels(
            project, sitesPath, areaPath, fieldName, categories, chosen, backgroundCount, seed, foldCount,
            betaMultiplier, validationMethod,
            progress=lambda fraction, message: emit("progress", value=fraction, message=message),
        )
    finally:
        project.closeLog()


def explorerChart(emit, dataPath, areaPath, sitesPath, siteField, outputPath, category=None,
                  fieldName=None, subAreaPath=None) -> dict:
    """Compare a layer's classes across the area, an optional sub-area and the sites; save a PNG chart."""
    import geopandas as gpd
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    import numpy as np

    sites = gpd.read_file(sitesPath)
    if category is not None:
        sites = sites.loc[sites[siteField].astype(str) == str(category)]
    if not sites.geom_type.eq("Point").all():
        raise ValueError("Explorer requires archaeological point locations")
    isRaster = Path(dataPath).suffix.lower() in (".tif", ".tiff")
    shares = (geoProcessing.rasterShares(dataPath, areaPath, sites, subAreaPath) if isRaster
              else geoProcessing.vectorShares(dataPath, fieldName, areaPath, sites, subAreaPath))

    figure = Figure(figsize=CHART_SIZE)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)
    labels, series = shares["labels"], shares["series"]
    positions = np.arange(len(labels))
    barWidth = BAR_GROUP_WIDTH / len(series)
    for seriesIndex, (name, values) in enumerate(series.items()):
        offset = (seriesIndex - (len(series) - 1) / 2) * barWidth
        axes.bar(positions + offset, values, width=barWidth, label=name)
    axes.set_xticks(positions)
    axes.set_xticklabels(labels, rotation=LABEL_ROTATION, ha="right")
    axes.set_xlabel("Raster values" if isRaster else fieldName)
    axes.set_ylabel("Share (%)")
    axes.set_title(f"Sites: {category or 'all categories'} ({len(sites)})")
    axes.legend()
    figure.tight_layout()
    figure.savefig(outputPath, dpi=CHART_DPI)
    return {"labels": labels, "series": {name: [float(v) for v in values] for name, values in series.items()},
            "image": str(outputPath)}


OPERATIONS = {
    "ping": ping,
    "vectorSummary": vectorSummary,
    "rasterRange": rasterRange,
    "importVector": importVector,
    "importArea": importArea,
    "writeRasterWeights": writeRasterWeights,
    "writeVectorWeights": writeVectorWeights,
    "writeVectorCategories": writeVectorCategories,
    "discoverPredictors": discoverPredictors,
    "runModels": runModels,
    "explorerChart": explorerChart,
}
