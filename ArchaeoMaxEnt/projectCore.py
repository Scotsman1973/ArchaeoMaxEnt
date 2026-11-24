"""Non-destructive project creation, data import and persistent logging.

Importable inside QGIS (standard library only at module level); importVector
needs geopandas and is called from the modelling runtime.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

RAW_INPUTS_FOLDER = "rawInputs"
DERIVED_INPUTS_FOLDER = "derivedInputs"
WEIGHTED_INPUTS_FOLDER = "weightedInputs"
MODELLING_OUTPUTS_FOLDER = "modellingOutputs"
LOGS_FOLDER = "logs"
PROJECT_FOLDERS = (
    RAW_INPUTS_FOLDER,
    DERIVED_INPUTS_FOLDER,
    WEIGHTED_INPUTS_FOLDER,
    MODELLING_OUTPUTS_FOLDER,
    LOGS_FOLDER,
)
LOG_FILE_NAME = "processing.log"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
RASTER_SUFFIXES = (".tif", ".tiff")
VECTOR_SUFFIXES = (".shp", ".gpkg")
RESERVED_FID_FIELD = "fid"
RENAMED_FID_FIELD = "source_fid"


def safeName(text: str) -> str:
    """Return text reduced to letters, digits, '_' and '-' for use in file names.

    Raises ValueError when nothing usable remains.
    """
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", text).strip("_")
    if not value:
        raise ValueError("A non-empty name is required")
    return value


class Project:
    """A project folder holding copied inputs, derived rasters, models and a log."""

    def __init__(self, projectDir: Path, logToFile: bool = True):
        """Open an existing project folder; unless logToFile is False, log to its processing.log."""
        self.projectDir = Path(projectDir)
        self.log = logging.getLogger(f"ArchaeoMaxEnt.{id(self)}")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        if logToFile:
            handler = logging.FileHandler(self.folder(LOGS_FOLDER) / LOG_FILE_NAME, encoding="utf-8")
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
            self.log.addHandler(handler)

    @classmethod
    def create(cls, parentDir: str, name: str) -> "Project":
        """Create a new project folder and its subfolders; never reuse an existing one."""
        projectDir = Path(parentDir).expanduser().resolve() / safeName(name)
        if projectDir.exists():
            raise FileExistsError(f"Project directory already exists: {projectDir}")
        projectDir.mkdir(parents=True)
        for folderName in PROJECT_FOLDERS:
            (projectDir / folderName).mkdir()
        project = cls(projectDir)
        project.log.info("Project created: %s", projectDir)
        return project

    def folder(self, name: str) -> Path:
        """Return the path of one of the project's standard subfolders."""
        if name not in PROJECT_FOLDERS:
            raise ValueError(f"Unknown project folder: {name}")
        return self.projectDir / name

    def closeLog(self):
        """Detach and close the log file handlers so the folder can be deleted."""
        for handler in list(self.log.handlers):
            self.log.removeHandler(handler)
            handler.close()

    def discard(self):
        """Close the log and delete the whole project folder (used to roll back a failed creation)."""
        self.closeLog()
        shutil.rmtree(self.projectDir)

    def importRaster(self, source: str, name: str) -> Path:
        """Copy a GeoTIFF into rawInputs/ under a safe name and return the copy's path."""
        sourcePath = Path(source).resolve()
        if sourcePath.suffix.lower() not in RASTER_SUFFIXES or not sourcePath.is_file():
            raise ValueError("Select an existing elevation GeoTIFF")
        target = self.folder(RAW_INPUTS_FOLDER) / f"{safeName(name)}.tif"
        if target.exists():
            raise FileExistsError(target)
        shutil.copy2(sourcePath, target)
        self.log.info("Copied raster %s to %s", sourcePath, target)
        return target

    def importArea(self, source: str, name: str, layerName: str | None = None) -> tuple:
        """Import the geographical area from a polygon layer or a GeoTIFF; return (GeoPackage path, note).

        A Shapefile or GeoPackage is copied as by importVector. A GeoTIFF is
        turned into a polygon of the cells that count as inside the area (see
        geoProcessing.rasterAreaPolygons), saved as a GeoPackage in rawInputs/,
        so the rest of the plugin always works with a polygon layer.
        Needs geopandas and rasterio (runs in the modelling runtime).
        """
        sourcePath = Path(source).resolve()
        if sourcePath.suffix.lower() not in RASTER_SUFFIXES:
            target = self.importVector(source, name, layerName)
            return target, ""
        if not sourcePath.is_file():
            raise ValueError("Select an existing polygon layer or GeoTIFF for the geographical area")
        from .geoProcessing import rasterAreaPolygons

        target = self.folder(RAW_INPUTS_FOLDER) / f"{safeName(name)}.gpkg"
        if target.exists():
            raise FileExistsError(target)
        frame, rule = rasterAreaPolygons(sourcePath)
        try:
            frame.to_file(target, driver="GPKG", layer=safeName(name))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        note = f"Geographical area made from the GeoTIFF {sourcePath.name}: {rule}."
        self.log.info("Imported %s as the geographical area polygon -> %s (%s)", sourcePath, target, rule)
        return target, note

    def importVector(self, source: str, name: str, layerName: str | None = None) -> Path:
        """Copy a Shapefile or GeoPackage layer into a new GeoPackage in rawInputs/.

        Uses geopandas, the same library that later reads the copy, so every
        column keeps its name and values. A source attribute named 'fid' is
        renamed to 'source_fid' because GeoPackage reserves 'fid' for its own
        feature identifier.
        """
        import geopandas as gpd

        sourcePath = Path(source).resolve()
        if not sourcePath.is_file() or sourcePath.suffix.lower() not in VECTOR_SUFFIXES:
            raise ValueError("Select a valid Shapefile or GeoPackage")
        try:
            frame = gpd.read_file(sourcePath, layer=layerName) if layerName else gpd.read_file(sourcePath)
        except Exception as exc:
            raise ValueError(f"Cannot read vector layer {sourcePath}: {exc}") from exc
        if frame.crs is None:
            raise ValueError(f"Vector layer has no CRS: {sourcePath}")
        if frame.empty:
            raise ValueError(f"Vector layer has no features: {sourcePath}")

        target = self.folder(RAW_INPUTS_FOLDER) / f"{safeName(name)}.gpkg"
        if target.exists():
            raise FileExistsError(target)

        fidColumns = [column for column in frame.columns if column.lower() == RESERVED_FID_FIELD]
        if fidColumns:
            if any(column.lower() == RENAMED_FID_FIELD for column in frame.columns):
                raise ValueError(f"Conflicting {RESERVED_FID_FIELD} and {RENAMED_FID_FIELD} fields")
            frame = frame.rename(columns={column: RENAMED_FID_FIELD for column in fidColumns})

        try:
            frame.to_file(target, driver="GPKG", layer=safeName(name))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        self.log.info("Imported %s (%d features) -> %s", sourcePath, len(frame), target)
        return target