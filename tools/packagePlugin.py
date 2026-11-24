"""Make the plugin .zip that users install in QGIS (Plugins > Manage and Install Plugins > Install from ZIP).

Run from the repository root:

    python tools/packagePlugin.py --repository OWNER/ArchaeoMaxEnt --checksums checksums.json

Options:
  --repository  the GitHub repository, e.g. aapre/ArchaeoMaxEnt. Its address is
                written into the zipped copy of runtime/runtime.json (where the
                plugin downloads runtimes from) and into metadata.txt
                (repository and tracker links), replacing the placeholders.
  --checksums   a checksums.json from the runtime release; included as
                runtime/checksums.json so every runtime download is checked.
  --output      folder for the zip (default: dist/)

The repository's own files are not changed; only the copies in the zip are.
Writes dist/ArchaeoMaxEnt-<version>-plugin.zip (version from metadata.txt)
and prints its path. Entry names always use forward slashes, so the zip
works on Windows, macOS and Linux. Standard library only.
"""
import argparse
import configparser
import json
import re
import sys
import zipfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_DIR / "ArchaeoMaxEnt"
PLUGIN_NAME = PLUGIN_DIR.name
# What goes into the plugin zip: every .py in the plugin folder except developer helpers,
# these files, and these folders (with everything inside them).
EXCLUDED_PY_FILES = {"pluginUpload.py"}
EXTRA_FILES = ("metadata.txt", "icon.png", "README.txt", "README.html")
EXTRA_FOLDERS = ("runtime", "i18n")
SKIPPED_PARTS = {"__pycache__", ".DS_Store"}
GITHUB_ADDRESS = re.compile(r"https://github\.com/[^/]+/[^/]+/releases/")


def pluginVersion() -> str:
    """Return the version in metadata.txt."""
    metadata = configparser.ConfigParser(interpolation=None)
    metadata.read(PLUGIN_DIR / "metadata.txt", encoding="utf-8")
    return metadata["general"]["version"].strip()


def pluginFiles():
    """Yield (path on disk, name inside the zip) for every file that belongs in the plugin."""
    paths = [path for path in sorted(PLUGIN_DIR.glob("*.py")) if path.name not in EXCLUDED_PY_FILES]
    paths += [PLUGIN_DIR / name for name in EXTRA_FILES if (PLUGIN_DIR / name).is_file()]
    for folderName in EXTRA_FOLDERS:
        paths += [path for path in sorted((PLUGIN_DIR / folderName).rglob("*")) if path.is_file()]
    for path in paths:
        relative = path.relative_to(PLUGIN_DIR)
        if not SKIPPED_PARTS.intersection(relative.parts) and not path.name.endswith(".pyc"):
            yield path, f"{PLUGIN_NAME}/{relative.as_posix()}"


def withRepository(name: str, data: bytes, repository) -> bytes:
    """Fill the GitHub repository into runtime.json and metadata.txt; other files are unchanged."""
    if not repository:
        return data
    if name.endswith("runtime/runtime.json"):
        spec = json.loads(data.decode("utf-8"))
        spec["downloadUrl"] = GITHUB_ADDRESS.sub(f"https://github.com/{repository}/releases/", spec["downloadUrl"])
        return (json.dumps(spec, indent=2) + "\n").encode("utf-8")
    if name.endswith(f"{PLUGIN_NAME}/metadata.txt"):
        text = data.decode("utf-8")
        text = re.sub(r"^repository=.*$", f"repository=https://github.com/{repository}", text, flags=re.MULTILINE)
        text = re.sub(r"^tracker=.*$", f"tracker=https://github.com/{repository}/issues", text, flags=re.MULTILINE)
        return text.encode("utf-8")
    return data


def main() -> int:
    """Write the plugin zip."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", help="GitHub repository, e.g. aapre/ArchaeoMaxEnt")
    parser.add_argument("--checksums", type=Path, help="checksums.json from the runtime release")
    parser.add_argument("--output", type=Path, default=REPO_DIR / "dist")
    options = parser.parse_args()

    if options.repository and not re.fullmatch(r"[\w.-]+/[\w.-]+", options.repository):
        raise SystemExit(f"--repository must look like owner/name, not {options.repository!r}")
    options.output.mkdir(parents=True, exist_ok=True)
    target = options.output / f"{PLUGIN_NAME}-{pluginVersion()}-plugin.zip"
    checksumsName = f"{PLUGIN_NAME}/runtime/checksums.json"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, name in pluginFiles():
            if name == checksumsName and options.checksums:
                continue  # replaced by the one given below
            archive.writestr(name, withRepository(name, path.read_bytes(), options.repository))
        if options.checksums:
            checksums = json.loads(options.checksums.read_text(encoding="utf-8"))  # must be valid JSON
            archive.writestr(checksumsName, json.dumps(checksums, indent=2) + "\n")
    print(f"Wrote {target} ({target.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
