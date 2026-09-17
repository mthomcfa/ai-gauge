#requires -version 7
<#
Build a standalone Windows .exe using PyInstaller.

Usage:
    .\build.ps1            # one-folder build (recommended)
    .\build.ps1 -OneFile   # single-file build (slower startup)
#>
param(
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Virtualenv not found at .venv. Run: py -m venv .venv; .venv\Scripts\pip install -e .[dev]"
}

# Pin the build tool to an exact version so a compromised or yanked future
# PyInstaller release can't silently enter the shipped binary.
& $venvPython -m pip install --quiet "pyinstaller==6.21.0"

$versionInfo = Join-Path $PSScriptRoot "build\pyinstaller-version-info.txt"
& $venvPython (Join-Path $PSScriptRoot "tools\write_pyinstaller_version_info.py") $versionInfo

if ($OneFile) {
    $targetExe = Join-Path $PSScriptRoot "dist\ai-gauge.exe"
    if (Test-Path $targetExe) {
        try {
            Remove-Item -LiteralPath $targetExe -Force -ErrorAction Stop
        } catch {
            Write-Error "Cannot replace dist\ai-gauge.exe. Close any running ai-gauge.exe process, then build again."
        }
    }
}

$args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--noupx",
    "--name", "ai-gauge",
    "--version-file", $versionInfo,
    "--paths", "src",
    # The meter catalog is data next to the code, so --paths does not carry it.
    # Without it every Claude/Codex meter is unreadable in a frozen build.
    # Windows uses ';' as the --add-data separator.
    "--add-data", "src\aigauge\providers\meter_catalog\*.json;aigauge\providers\meter_catalog",
    # The app icon, by the same rule: data next to the code, placed where the
    # package-relative lookup in app.py finds it in the frozen tree.
    "--add-data", "src\aigauge\assets\ai-gauge-256.png;aigauge\assets",
    # The .exe's own icon, which is a different thing from the window icon:
    # this is what Explorer and the taskbar shortcut show.
    "--icon", "assets\icon\ai-gauge.ico",
    "--collect-all", "PyQt6.QtWebEngineWidgets",
    "--collect-all", "PyQt6.QtWebEngineCore",
    "pyinstaller_entry.py"
)
if ($OneFile) { $args += "--onefile" }

& $venvPython @args

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
if ($OneFile) {
    Write-Host "Binary: dist\ai-gauge.exe"
} else {
    Write-Host "Folder: dist\ai-gauge\  (run ai-gauge.exe inside)"
}
