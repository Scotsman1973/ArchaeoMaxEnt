# Releasing ArchaeoMaxEnt

Two GitHub workflows make everything users need. Nothing has to be built on
your own computer.

| Workflow (Actions tab) | What it makes | When to run it |
|---|---|---|
| **Modelling runtime** | one runtime `.zip` per kind of computer (Windows, Mac with Apple chip, Mac with Intel chip, Linux, Linux Arm), published as release `runtime-<id>` | only when the packages change (the lock files in `ArchaeoMaxEnt/runtime/locks/`) |
| **Plugin release** | `ArchaeoMaxEnt-<version>-plugin.zip`, published as release `v<version>` | for every new plugin version |

Users only ever install the plugin zip. The first time ArchaeoMaxEnt opens, it
offers to install its modelling tools. It downloads the right runtime `.zip`
from the `runtime-<id>` release, or builds it on the user's computer if it
cannot be downloaded.

## One-time setup

1. Create a **public** repository named exactly `ArchaeoMaxEnt` on github.com,
   with no README, licence or .gitignore. (Public, because the free Linux Arm build
   machine is only available to public repositories.)
2. Upload this folder to it. In PowerShell, in this folder:

   ```powershell
   git init -b main
   git add .
   git commit -m "ArchaeoMaxEnt 0.2"
   git remote add origin https://github.com/YOUR-USERNAME/ArchaeoMaxEnt.git
   git push -u origin main
   ```

   You do not need to edit `OWNER` in `runtime.json`: the Plugin release workflow
   fills in your repository's address when it makes the plugin zip.

## Publish the runtimes (first release, and whenever the packages change)

1. Open **Actions** > **Modelling runtime** > **Run workflow**, tick
   **Publish the archives as a GitHub release**, and click **Run workflow**.
2. Wait for all five builds to get a green tick (about 15–30 minutes). Each is built
   on its own kind of computer with `ArchaeoMaxEnt/runtime/buildRuntime.ps1` or
   `buildRuntime.sh`, checked against the lock file, and test-installed.
3. The release `runtime-<id>` now holds the five runtime `.zip` files, their
   `.sha256` checksums and `checksums.json`.

Every push that changes the runtime files also builds the five runtimes without
publishing them, as a test. To download one of those test builds, open the run
and look under **Artifacts** at the bottom. GitHub wraps each one in its own zip;
unzip that once to get the runtime `.zip`.

The workflow refuses to publish over an existing `runtime-<id>` release. Rebuilt
files would have new checksums, and plugins already in use would then reject them.

## Publish a plugin version

1. Set `version=` in `ArchaeoMaxEnt/metadata.txt` (e.g. `0.3`), then commit and push:

   ```powershell
   git add .
   git commit -m "ArchaeoMaxEnt 0.3"
   git push
   ```

2. Open **Actions** > **Plugin release** > **Run workflow** > **Run workflow**.
3. The release `v0.3` now holds `ArchaeoMaxEnt-0.3-plugin.zip`. That is the file to
   give users or to upload to plugins.qgis.org. The workflow puts the runtime
   checksums and your repository's address into it.

The Plugin release workflow stops with a clear message if the runtimes for its
lock files have not been published yet, or if that version already has a release.

## Making the zips on your own computer instead

- Runtime for your computer:
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\ArchaeoMaxEnt\runtime\buildRuntime.ps1 -OutputDir .\dist`
  (on Mac or Linux: `bash ArchaeoMaxEnt/runtime/buildRuntime.sh dist`)
- Plugin zip: `python tools/packagePlugin.py --repository YOUR-USERNAME/ArchaeoMaxEnt`
  (any Python 3.9 or later, including the one inside QGIS)
