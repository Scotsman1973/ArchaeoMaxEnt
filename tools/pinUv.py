"""Pin the uv version the runtime build scripts download, with its checksums.

Run from the repository root, with the uv version to use:

    python tools/pinUv.py 0.13.9

Writes ArchaeoMaxEnt/runtime/uv.txt: one line per platform with the SHA-256
and address of uv's wheel on PyPI. buildRuntime.sh and buildRuntime.ps1 read
it, download the wheel for their computer, refuse it unless the checksum
matches, and take the uv program out of it. (A wheel is a zip file; PyPI
is used because it is the same site the modelling packages come from.)
"""
import json
import sys
import urllib.request
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "ArchaeoMaxEnt" / "runtime" / "uv.txt"
# Platform key (as in runtime.json) -> the part of uv's wheel name for that platform.
WHEEL_TAGS = {
    "windows-x86_64": "win_amd64",
    "macos-arm64": "macosx_11_0_arm64",
    "macos-x86_64": "macosx_10_12_x86_64",
    "linux-x86_64": "manylinux_2_17_x86_64.manylinux2014_x86_64",
    "linux-aarch64": "manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64",
}


def main() -> int:
    """Look the version up on PyPI and write uv.txt."""
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    version = sys.argv[1]
    with urllib.request.urlopen(f"https://pypi.org/pypi/uv/{version}/json") as response:
        files = {item["filename"]: item for item in json.load(response)["urls"]}
    lines = [f"# uv {version} wheels from PyPI, written by tools/pinUv.py. Format: platform sha256 url"]
    for key, tag in WHEEL_TAGS.items():
        item = files[f"uv-{version}-py3-none-{tag}.whl"]
        lines.append(f"{key} {item['digests']['sha256']} {item['url']}")
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
