"""Slope and aspect from the elevation DEM, using QGIS Processing (GDAL provider).

Runs inside QGIS. Uses only the QGIS API, so it needs no extra packages.
"""
from __future__ import annotations

from pathlib import Path

# Each terrain raster with its algorithm and the parameters that algorithm accepts.
TERRAIN_ALGORITHMS = {
    "slope": ("gdal:slope", {"BAND": 1, "SCALE": 1, "AS_PERCENT": False,
                             "COMPUTE_EDGES": False, "ZEVENBERGEN": False}),
    "aspect": ("gdal:aspect", {"BAND": 1, "TRIG_ANGLE": False, "ZERO_FLAT": False,
                               "COMPUTE_EDGES": False, "ZEVENBERGEN": False}),
}


def checkDem(demPath: Path):
    """Raise ValueError unless the DEM is a valid one-band raster in a projected CRS."""
    from qgis.core import QgsRasterLayer

    layer = QgsRasterLayer(str(demPath), "elevation")
    if not layer.isValid():
        raise ValueError(f"Cannot read elevation raster: {demPath}")
    if layer.bandCount() != 1:
        raise ValueError("Elevation must have one band")
    if not layer.crs().isValid() or layer.crs().isGeographic():
        raise ValueError("Elevation must use a projected CRS with horizontal units in metres")


def createTerrainRasters(demPath: Path, derivedDir: Path) -> dict:
    """Derive slope and aspect from the elevation DEM.

    Returns a dict mapping 'slope' and 'aspect' to their new GeoTIFF paths.
    """
    import processing

    checkDem(demPath)
    outputs = {}
    for label, (algorithm, parameters) in TERRAIN_ALGORITHMS.items():
        target = derivedDir / f"{label}.tif"
        if target.exists():
            raise FileExistsError(target)
        processing.run(algorithm, {**parameters, "INPUT": str(demPath), "OUTPUT": str(target)})
        outputs[label] = target
    return outputs
