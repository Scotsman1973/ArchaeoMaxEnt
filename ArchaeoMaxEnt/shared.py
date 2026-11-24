"""Constants and small helpers shared by the QGIS side and the modelling runtime.

Standard library only: this module is imported inside QGIS, where the
scientific packages are not available.
"""
from __future__ import annotations

import csv
from pathlib import Path

# Class weights chosen by the researcher.
WEIGHT_MIN = 0.0
WEIGHT_MAX = 9.0
WEIGHT_TOLERANCE = 1e-7

# Aspect is split into eight compass sectors; north spans 337.5-22.5 degrees.
ASPECT_SECTOR_COUNT = 8
ASPECT_SECTOR_DEGREES = 360 / ASPECT_SECTOR_COUNT
ASPECT_SECTOR_NAMES = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

# How a predictor raster is treated by MaxEnt.
PREDICTOR_NUMERIC = "numeric"
PREDICTOR_CATEGORICAL = "categorical"
PREDICTOR_CIRCULAR = "circular"
PREDICTOR_TYPES = (PREDICTOR_NUMERIC, PREDICTOR_CATEGORICAL, PREDICTOR_CIRCULAR)
WEIGHTED_SOURCE = "weighted"

WEIGHTS_CSV_HEADER = ("class", "description", "weight")

# Validation methods.
VALIDATION_LEAVE_ONE_OUT = "leaveOneOut"
VALIDATION_SPATIAL_K_FOLD = "spatialKFold"


def validateWeights(weights: dict):
    """Raise ValueError unless every weight is between 0.0 and 9.0 in 0.1 steps."""
    for value in weights.values():
        inRange = WEIGHT_MIN <= value <= WEIGHT_MAX
        onStep = abs(value * 10 - round(value * 10)) <= WEIGHT_TOLERANCE
        if not inRange or not onStep:
            raise ValueError("Weights must be 0.0–9.0 in 0.1 steps")


def aspectSectorCaption(sectorIndex: int) -> str:
    """Return a compass caption for an aspect class, e.g. 'NE (22.5–67.5°)'."""
    start = (sectorIndex * ASPECT_SECTOR_DEGREES - ASPECT_SECTOR_DEGREES / 2) % 360
    end = (sectorIndex * ASPECT_SECTOR_DEGREES + ASPECT_SECTOR_DEGREES / 2) % 360
    return f"{ASPECT_SECTOR_NAMES[sectorIndex]} ({start:g}–{end:g}°)"


def writeWeightsCsv(csvPath: Path, rows):
    """Write one row per class (class, description, weight) beside a weighted raster."""
    with open(csvPath, "w", newline="", encoding="utf-8") as table:
        writer = csv.writer(table)
        writer.writerow(WEIGHTS_CSV_HEADER)
        writer.writerows(rows)
