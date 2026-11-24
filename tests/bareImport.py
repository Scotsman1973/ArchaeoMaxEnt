from pathlib import Path as _P
REPO = _P(__file__).resolve().parents[1]
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path[:0] = [str(REPO / "tests" / "stubs"), str(REPO)]
for name in ("numpy", "pandas", "geopandas", "rasterio", "elapid", "sklearn", "matplotlib"):
    try:
        __import__(name); print("UNEXPECTED: has", name)
    except ImportError:
        pass
from PyQt5.QtWidgets import QApplication
app = QApplication([])
import ArchaeoMaxEnt
from ArchaeoMaxEnt import archaeoMaxEnt, archaeoMaxEntDialog
class Iface:
    def mainWindow(self): return None
    def addToolBarIcon(self, a): pass
    def addPluginToMenu(self, m, a): pass
    def removePluginMenu(self, m, a): pass
    def removeToolBarIcon(self, a): pass
plugin = ArchaeoMaxEnt.classFactory(Iface()); plugin.initGui()
archaeoMaxEntDialog.defaultRuntimeDir = lambda: REPO / "build" / "no-runtime-here"
plugin.run()
print("dialog open:", plugin.dialog.isVisible(), "| status:", plugin.dialog.runtimeLabel.text().split(">")[1].split("<")[0])
heavy = sorted(m for m in sys.modules if m.split(".")[0] in ("numpy", "pandas", "geopandas", "rasterio", "elapid", "sklearn", "matplotlib"))
print("heavy modules loaded:", heavy or "none")
plugin.unload()
