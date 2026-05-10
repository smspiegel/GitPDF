#!/usr/bin/env pwsh
# Build the portable gitpdf app into dist/gitpdf/.
#
# Usage:
#   .\scripts\build.ps1
#
# Result: dist/gitpdf/ contains gitpdf.exe plus all dependencies.
# Drop the folder anywhere -- it is fully self-contained.

# Native CLIs (pip, pyinstaller) emit normal logs on stderr. We deliberately
# do NOT set $ErrorActionPreference = "Stop" globally because that flag turns
# every stderr line into a terminating error under Windows PowerShell. We rely
# on $LASTEXITCODE checks after each native invocation instead.
Set-Location (Split-Path -Parent $PSScriptRoot)

$venvPython = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "==> Creating venv at .\.venv ..." -ForegroundColor Cyan
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        throw "python -m venv failed -- ensure Python 3.10+ is installed and on PATH (https://www.python.org/downloads/)"
    }
    Write-Host "==> Installing project dependencies (this takes a minute) ..." -ForegroundColor Cyan
    & $venvPython -m pip install --quiet --disable-pip-version-check --upgrade pip
    & $venvPython -m pip install --disable-pip-version-check -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "pip install -e .[dev] failed" }
}

Write-Host "==> Ensuring PyInstaller is installed..." -ForegroundColor Cyan
& $venvPython -m pip install --quiet --disable-pip-version-check pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }

Write-Host "==> Ensuring PDF.js is downloaded..." -ForegroundColor Cyan
$pdfjs = ".\src\gitpdf\web\vendor\pdfjs\build\pdf.mjs"
if (-not (Test-Path $pdfjs)) {
    & $venvPython .\scripts\fetch_pdfjs.py
    if ($LASTEXITCODE -ne 0) { throw "fetch_pdfjs.py failed" }
}

# Quality gate: run the test suite before producing a binary. A red build
# here is intentional -- shipping an exe whose tests don't pass would just
# push the failure downstream. Set $env:GITPDF_SKIP_TESTS = '1' to bypass
# locally (CI must never set that).
if ($env:GITPDF_SKIP_TESTS -eq '1') {
    Write-Host "==> Skipping tests (GITPDF_SKIP_TESTS=1)" -ForegroundColor Yellow
} else {
    Write-Host "==> Running test suite..." -ForegroundColor Cyan
    & $venvPython -m pytest tests
    if ($LASTEXITCODE -ne 0) { throw "Tests failed -- aborting build." }
}

Write-Host "==> Cleaning previous build..." -ForegroundColor Cyan

# A running gitpdf(-console).exe from a previous test will hold files in
# dist\gitpdf open and make the cleanup fail with WinError 5. Stop them.
$running = Get-Process -Name "gitpdf","gitpdf-console" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "    stopping running gitpdf processes: $($running.Id -join ', ')"
    $running | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
}

if (Test-Path .\build) { Remove-Item -Recurse -Force .\build }

# Antivirus / Explorer can briefly hold a handle even after the process
# exits. Retry the dist removal a few times before giving up.
if (Test-Path .\dist\gitpdf) {
    for ($i = 0; $i -lt 5; $i++) {
        try {
            Remove-Item -Recurse -Force .\dist\gitpdf -ErrorAction Stop
            break
        } catch {
            if ($i -eq 4) {
                throw "Could not remove dist\gitpdf (locked). Close any Explorer window in that folder and retry. Original error: $_"
            }
            Start-Sleep -Milliseconds 500
        }
    }
}

Write-Host "==> Running PyInstaller..." -ForegroundColor Cyan
& $venvPython -m PyInstaller gitpdf.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = ".\dist\gitpdf\gitpdf.exe"
if (-not (Test-Path $exe)) { throw "Expected $exe to exist" }

Write-Host ""
Write-Host "Build OK." -ForegroundColor Green
Write-Host "Portable folder: $(Resolve-Path .\dist\gitpdf)"
Write-Host "Launch (users):  .\dist\gitpdf\gitpdf.exe          (windowed, auto-opens browser)"
Write-Host "Launch (debug):  .\dist\gitpdf\gitpdf-console.exe   (console attached, shows logs)"
Write-Host "CLI usage:       .\dist\gitpdf\gitpdf-console.exe diff path\to\A.pdf path\to\B.pdf --pretty"
