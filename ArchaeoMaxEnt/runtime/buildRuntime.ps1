# Build the ArchaeoMaxEnt modelling runtime on this Windows computer.
#
# Downloads everything the runtime needs and packs it as one .zip, which the
# plugin then installs (Project tab > Install from file). The plugin's
# "Build on this computer" button runs this script for you and installs the
# result; you can also run it yourself in PowerShell:
#
#     powershell -ExecutionPolicy Bypass -File buildRuntime.ps1 [-OutputDir FOLDER]
#
# -OutputDir  where the finished .zip goes (default: your Downloads folder)
# -Platform   windows-x86_64 (the only Windows runtime; default)
#
# What it downloads, and from where:
#   1. uv, the tool that fetches Python and the packages, at the exact version
#      and checksum listed in uv.txt (from PyPI: files.pythonhosted.org);
#   2. a standalone Python at the version in runtime.json (via uv);
#   3. every modelling package at the exact version and checksum in
#      locks\windows-x86_64.lock (from PyPI).
# Everything is downloaded into a temporary folder that is deleted at the end.
# Nothing is installed on your computer, and neither PATH nor the registry is
# changed. Needs about 2 GB of free disk space while it runs; takes 5-15 minutes.
#
# Written for Windows PowerShell 5.1 (built into Windows 10 and 11) and later.
# This file must stay plain ASCII: PowerShell 5.1 misreads other characters.

param(
    [string]$OutputDir = "",
    [string]$Platform = "windows-x86_64"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # the progress bar makes downloads in PowerShell 5.1 very slow
[Console]::OutputEncoding = [Text.Encoding]::UTF8   # so the plugin's log shows names with accents correctly
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$RuntimeDir = $PSScriptRoot
$PluginDir = Split-Path -Parent $RuntimeDir

function Fail([string]$Message) {
    [Console]::Error.WriteLine("ERROR: $Message")
    exit 1
}

if ($Platform -ne "windows-x86_64") {
    Fail "This script builds the Windows runtime (windows-x86_64); on macOS or Linux use buildRuntime.sh"
}
if (-not [Environment]::Is64BitOperatingSystem) {
    Fail "A 64-bit Windows is required"
}
if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {
    Write-Output "Note: Windows on Arm runs the x86_64 runtime through emulation."
}

if (-not $OutputDir) {
    $OutputDir = Join-Path $env:USERPROFILE "Downloads"
    if (-not (Test-Path -LiteralPath $OutputDir)) { $OutputDir = (Get-Location).Path }
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path -LiteralPath $OutputDir).Path

$spec = Get-Content -Raw -LiteralPath (Join-Path $RuntimeDir "runtime.json") | ConvertFrom-Json
$PythonVersion = $spec.python
if (-not $PythonVersion) { Fail "Cannot read the Python version from runtime.json" }

$uvLine = Get-Content -LiteralPath (Join-Path $RuntimeDir "uv.txt") | Where-Object { $_ -like "$Platform *" } | Select-Object -First 1
if (-not $uvLine) { Fail "uv.txt has no entry for $Platform" }
$uvParts = $uvLine -split "\s+"
$UvSha256 = $uvParts[1]
$UvUrl = $uvParts[2]

$WorkDir = Join-Path ([IO.Path]::GetTempPath()) ("archaeoMaxEnt-build-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

# Quote one argument for a Windows command line (paths may contain spaces).
function Quote([string]$Text) {
    $Text = $Text -replace '(\\+)$', '$1$1'   # a trailing backslash would otherwise escape the closing quote
    return '"' + $Text.Replace('"', '\"') + '"'
}

try {
    Write-Output "Building the ArchaeoMaxEnt runtime for $Platform (Python $PythonVersion)"
    Write-Output "Temporary folder: $WorkDir"
    Write-Output "Output folder: $OutputDir"

    # ---- 1. uv, checked against its published checksum ----
    $wheel = Join-Path $WorkDir "uv.whl"
    Write-Output "Downloading uv from $UvUrl"
    Invoke-WebRequest -Uri $UvUrl -OutFile $wheel -UseBasicParsing
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $wheel).Hash.ToLowerInvariant()
    if ($actual -ne $UvSha256.ToLowerInvariant()) { Fail "uv download is damaged or altered (checksum mismatch)" }
    Write-Output "uv checksum OK"

    # The wheel is a zip file; uv.exe is inside it under uv-<version>.data/scripts/.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $uv = Join-Path $WorkDir "uv.exe"
    $archive = [IO.Compression.ZipFile]::OpenRead($wheel)
    try {
        $entry = $archive.Entries | Where-Object { $_.FullName -like "*.data/scripts/uv.exe" } | Select-Object -First 1
        if (-not $entry) { Fail "Could not find uv.exe in the downloaded file" }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $uv, $true)
    }
    finally {
        $archive.Dispose()
    }

    # ---- 2 and 3. Python and the locked packages, all kept inside WorkDir ----
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $WorkDir "build\pythons"
    $env:UV_CACHE_DIR = Join-Path $WorkDir "build\uv-cache"
    $env:UV_PYTHON_INSTALL_BIN = "0"
    $env:UV_PYTHON_INSTALL_REGISTRY = "0"
    $env:UV_NO_CONFIG = "1"
    $env:UV_PYTHON_DOWNLOADS = "automatic"
    $env:UV_SYSTEM_CERTS = "1"    # trust the same certificates as Windows (institutional proxies)
    $env:UV_NO_PROGRESS = "1"
    $env:NO_COLOR = "1"
    foreach ($name in "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX") {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }

    # Start-Process lets uv write straight to this window (or to the plugin's log) and
    # avoids PowerShell 5.1 turning uv's progress messages into errors.
    $arguments = @("run", "--no-project", "--python", $PythonVersion, "python", "-I",
                   (Join-Path $PluginDir "runtimeBuilder.py"), "--uv", $uv,
                   "--build-dir", (Join-Path $WorkDir "build"), "--dist-dir", $OutputDir, "--platform", $Platform)
    $process = Start-Process -FilePath $uv -ArgumentList (($arguments | ForEach-Object { Quote $_ }) -join " ") `
        -NoNewWindow -Wait -PassThru
    if ($process.ExitCode -ne 0) { Fail "Building the runtime failed (exit code $($process.ExitCode)); see the messages above" }
}
finally {
    Set-Location -LiteralPath ([IO.Path]::GetTempPath())
    Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
}
exit 0
