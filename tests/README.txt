Tests (run from the repository root, with numpy/rasterio/geopandas/PyQt5 in the
Python you run them with; they build their own synthetic data):

  python tools/buildRuntime.py          build dist/<runtime>.zip for this computer first
  python -I tests/runtimeTests.py       offline install, refusals, every worker operation, cancel
  python -I tests/areaTests.py          GeoTIFF geographical areas (modelling packages only)
  python -I tests/backgroundTests.py    background points stay inside the geographical area
                                        (needs only the modelling packages, not a built runtime)
  python -I tests/dialogSmoke.py        the real dialog, offscreen, through the whole workflow,
                                        including the automatic runtime install (needs internet)
  <bare python with only PyQt5> -I tests/bareImport.py
                                        the plugin loads with no scientific packages at all

stubs/qgis is a minimal stand-in for the QGIS API (PyQt5 underneath) used by the
dialog tests; it is not shipped with the plugin.
