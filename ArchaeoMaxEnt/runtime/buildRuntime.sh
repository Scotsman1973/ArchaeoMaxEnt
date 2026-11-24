#!/bin/bash
# Build the ArchaeoMaxEnt modelling runtime on this Mac or Linux computer.
#
# Downloads everything the runtime needs and packs it as one .zip, which the
# plugin then installs (Project tab > Install from file). The plugin's
# "Build on this computer" button runs this script for you and installs the
# result; you can also run it yourself in a terminal:
#
#     bash buildRuntime.sh [OUTPUT_FOLDER] [PLATFORM]
#
# OUTPUT_FOLDER  where the finished .zip goes (default: your Downloads folder)
# PLATFORM       macos-arm64, macos-x86_64, linux-x86_64 or linux-aarch64
#                (default: this computer)
#
# What it downloads, and from where:
#   1. uv, the tool that fetches Python and the packages, at the exact version
#      and checksum listed in uv.txt (from PyPI: files.pythonhosted.org);
#   2. a standalone Python at the version in runtime.json (via uv);
#   3. every modelling package at the exact version and checksum in
#      locks/<PLATFORM>.lock (from PyPI).
# Everything is downloaded into a temporary folder that is deleted at the end.
# Nothing is installed on your computer, and your PATH is not changed.
# Needs about 2 GB of free disk space while it runs; takes 5-15 minutes.
#
# Written for bash 3.2 (the version macOS ships) and later.

set -eu -o pipefail

RUNTIME_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$(dirname "$RUNTIME_DIR")"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

if [ -n "${1:-}" ]; then
    OUTPUT_DIR="$1"
elif [ -d "$HOME/Downloads" ]; then
    OUTPUT_DIR="$HOME/Downloads"
else
    OUTPUT_DIR="$PWD"
fi
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

PLATFORM="${2:-}"
if [ -z "$PLATFORM" ]; then
    case "$(uname -s)" in
        Darwin)
            # On Apple chips a terminal running under Rosetta reports x86_64; ask the hardware instead.
            if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ]; then
                PLATFORM=macos-arm64
            else
                PLATFORM=macos-x86_64
            fi ;;
        Linux)
            case "$(uname -m)" in
                x86_64 | amd64) PLATFORM=linux-x86_64 ;;
                aarch64 | arm64) PLATFORM=linux-aarch64 ;;
                *) fail "No runtime can be built for this processor ($(uname -m))" ;;
            esac ;;
        *) fail "This script is for macOS and Linux; on Windows use buildRuntime.ps1" ;;
    esac
fi

PYTHON_VERSION="$(sed -n 's/.*"python"[[:space:]]*:[[:space:]]*"\([0-9.]*\)".*/\1/p' "$RUNTIME_DIR/runtime.json")"
[ -n "$PYTHON_VERSION" ] || fail "Cannot read the Python version from runtime.json"
UV_LINE="$(grep "^$PLATFORM " "$RUNTIME_DIR/uv.txt" || true)"
[ -n "$UV_LINE" ] || fail "uv.txt has no entry for $PLATFORM"
UV_SHA256="$(echo "$UV_LINE" | awk '{print $2}')"
UV_URL="$(echo "$UV_LINE" | awk '{print $3}')"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/archaeoMaxEnt-build.XXXXXX")"
cleanUp() {
    rm -rf "$WORK_DIR"
}
trap cleanUp EXIT
trap 'exit 130' INT TERM

echo "Building the ArchaeoMaxEnt runtime for $PLATFORM (Python $PYTHON_VERSION)"
echo "Temporary folder: $WORK_DIR"
echo "Output folder: $OUTPUT_DIR"

# ---- 1. uv, checked against its published checksum ----
WHEEL="$WORK_DIR/uv.whl"
echo "Downloading uv from $UV_URL"
if command -v curl >/dev/null 2>&1; then
    curl --fail --location --silent --show-error --retry 3 --output "$WHEEL" "$UV_URL"
elif command -v wget >/dev/null 2>&1; then
    wget --quiet --tries=3 --output-document="$WHEEL" "$UV_URL"
else
    fail "Neither curl nor wget is available to download files"
fi

if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL_SHA256="$(sha256sum "$WHEEL" | awk '{print $1}')"
else
    ACTUAL_SHA256="$(shasum -a 256 "$WHEEL" | awk '{print $1}')"
fi
[ "$ACTUAL_SHA256" = "$UV_SHA256" ] || fail "uv download is damaged or altered (checksum mismatch)"
echo "uv checksum OK"

# The wheel is a zip file; the uv program is inside it under uv-<version>.data/scripts/.
UV="$WORK_DIR/uv"
if command -v unzip >/dev/null 2>&1; then
    unzip -p "$WHEEL" '*.data/scripts/uv' > "$UV"
elif command -v python3 >/dev/null 2>&1; then
    python3 -I -c 'import sys, zipfile
archive = zipfile.ZipFile(sys.argv[1])
name = next(n for n in archive.namelist() if n.endswith(".data/scripts/uv"))
open(sys.argv[2], "wb").write(archive.read(name))' "$WHEEL" "$UV"
else
    fail "Neither unzip nor python3 is available to unpack uv"
fi
chmod +x "$UV"
[ -s "$UV" ] || fail "Could not unpack uv"
echo "Using $("$UV" --version)"

# ---- 2 and 3. Python and the locked packages, all kept inside WORK_DIR ----
export UV_PYTHON_INSTALL_DIR="$WORK_DIR/build/pythons"
export UV_CACHE_DIR="$WORK_DIR/build/uv-cache"
export UV_PYTHON_INSTALL_BIN=0
export UV_PYTHON_INSTALL_REGISTRY=0
export UV_NO_CONFIG=1
export UV_PYTHON_DOWNLOADS=automatic
export UV_SYSTEM_CERTS=1    # trust the same certificates as this computer (institutional proxies)
export UV_NO_PROGRESS=1
export NO_COLOR=1
unset PYTHONHOME PYTHONPATH VIRTUAL_ENV CONDA_PREFIX 2>/dev/null || true

"$UV" run --no-project --python "$PYTHON_VERSION" python -I "$PLUGIN_DIR/runtimeBuilder.py" \
    --uv "$UV" --build-dir "$WORK_DIR/build" --dist-dir "$OUTPUT_DIR" --platform "$PLATFORM"
