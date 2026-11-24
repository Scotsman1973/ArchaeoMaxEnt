"""Read-only Environmental Explorer.

Charts how an environmental layer is distributed across the whole
geographical area, an optional sub-area and the archaeological sites, to help
the researcher choose class weights. It never alters model inputs.

The chart is calculated and drawn by the modelling runtime and shown here as
an image, so this dialog needs only the QGIS API (no matplotlib in QGIS).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)

from .projectCore import LOGS_FOLDER

ALL_CATEGORIES_LABEL = "All categories"
RASTER_VALUES_LABEL = "Raster values"
RASTER_FILTER = "GeoTIFF (*.tif *.tiff)"
VECTOR_FILTER = "Vector (*.gpkg *.shp)"
RASTER_SUFFIXES = (".tif", ".tiff")
WINDOW_SIZE = (900, 640)
CHART_FILE_NAME = "explorerChart.png"
NO_SUB_AREA_TEXT = "Sub-area: none (optional Shapefile or GeoPackage inside the geographical area)"


class Explorer(QDialog):
    """Dialog comparing environmental classes across area, sub-area and sites."""

    def __init__(self, callRuntime, categories, sitesPath, fieldName, areaPath, projectDir, parent=None):
        """Build the controls; callRuntime(operation, **args) runs work in the modelling runtime."""
        super().__init__(parent)
        self.callRuntime = callRuntime
        self.sitesPath = sitesPath
        self.fieldName = fieldName
        self.areaPath = areaPath
        self.startDir = str(projectDir)
        self.chartPath = Path(projectDir) / LOGS_FOLDER / CHART_FILE_NAME
        self.dataPath = None
        self.subAreaPath = None

        self.setWindowTitle("Environmental Explorer (read-only)")
        self.resize(*WINDOW_SIZE)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Open a GeoTIFF or vector layer to compare its classes across the geographical area, "
            "an optional sub-area and the sites. Charts never alter model inputs."))

        controlRow = QHBoxLayout()
        self.categoryCombo = QComboBox()
        self.categoryCombo.addItem(ALL_CATEGORIES_LABEL)
        self.categoryCombo.addItems([str(category) for category in categories])
        self.fieldCombo = QComboBox()
        controlRow.addWidget(QLabel("Sites"))
        controlRow.addWidget(self.categoryCombo)
        controlRow.addWidget(QLabel("Field"))
        controlRow.addWidget(self.fieldCombo)
        layout.addLayout(controlRow)

        self.subAreaLabel = QLabel(NO_SUB_AREA_TEXT)
        layout.addWidget(self.subAreaLabel)
        self.chartLabel = QLabel("Open a raster or vector layer to draw a chart.")
        self.chartLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chartLabel.setMinimumHeight(420)
        layout.addWidget(self.chartLabel, 1)

        buttonRow = QHBoxLayout()
        for title, handler in (
            ("Open raster", self.openRaster),
            ("Open vector", self.openVector),
            ("Open sub-area", self.openSubArea),
            ("Clear sub-area", self.clearSubArea),
            ("Export PNG", self.exportPng),
        ):
            button = QPushButton(title)
            button.clicked.connect(handler)
            buttonRow.addWidget(button)
        layout.addLayout(buttonRow)

        self.categoryCombo.currentIndexChanged.connect(self.refresh)
        self.fieldCombo.currentIndexChanged.connect(self.refresh)

    def chooseFile(self, title, fileFilter):
        """Ask for an existing file, starting in the project folder; return '' if cancelled."""
        path, _ = QFileDialog.getOpenFileName(self, title, self.startDir, fileFilter)
        return path

    def setFieldChoices(self, names):
        """Replace the field list without triggering a refresh for each change."""
        self.fieldCombo.blockSignals(True)
        self.fieldCombo.clear()
        self.fieldCombo.addItems(names)
        self.fieldCombo.blockSignals(False)

    def openRaster(self):
        """Choose a GeoTIFF to explore and redraw the chart."""
        path = self.chooseFile("Choose raster", RASTER_FILTER)
        if path:
            self.dataPath = path
            self.setFieldChoices([RASTER_VALUES_LABEL])
            self.refresh()

    def openVector(self):
        """Choose a categorical vector layer to explore, list its fields and redraw the chart."""
        path = self.chooseFile("Choose vector", VECTOR_FILTER)
        if path:
            try:
                summary = self.callRuntime("vectorSummary", path=path)
            except Exception as exc:
                QMessageBox.critical(self, "Explorer error", str(exc))
                return
            self.dataPath = path
            self.setFieldChoices(summary["fields"])
            self.refresh()

    def openSubArea(self):
        """Choose a polygon sub-area to add to the comparison and redraw the chart."""
        path = self.chooseFile("Choose sub-area", VECTOR_FILTER)
        if path:
            self.subAreaPath = path
            self.subAreaLabel.setText(f"Sub-area: {Path(path).name}")
            self.refresh()

    def clearSubArea(self):
        """Remove the sub-area from the comparison and redraw the chart."""
        self.subAreaPath = None
        self.subAreaLabel.setText(NO_SUB_AREA_TEXT)
        self.refresh()

    def refresh(self, *_):
        """Ask the runtime to recalculate the class shares and show the new chart."""
        if not self.dataPath:
            return
        isRaster = self.dataPath.lower().endswith(RASTER_SUFFIXES)
        fieldName = None if isRaster else self.fieldCombo.currentText()
        if not isRaster and not fieldName:
            return
        category = self.categoryCombo.currentText() if self.categoryCombo.currentIndex() > 0 else None
        try:
            self.callRuntime("explorerChart", dataPath=self.dataPath, areaPath=self.areaPath,
                             sitesPath=self.sitesPath, siteField=self.fieldName, outputPath=str(self.chartPath),
                             category=category, fieldName=fieldName, subAreaPath=self.subAreaPath)
        except Exception as exc:
            QMessageBox.critical(self, "Explorer error", str(exc))
            return
        pixmap = QPixmap(str(self.chartPath))
        self.chartLabel.setPixmap(pixmap.scaled(
            self.chartLabel.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def exportPng(self):
        """Save a copy of the current chart as a PNG file."""
        if not self.chartPath.is_file():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export chart", self.startDir, "PNG (*.png)")
        if path:
            shutil.copyfile(self.chartPath, path)
