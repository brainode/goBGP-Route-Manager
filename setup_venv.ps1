param(
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $scriptDir "venv"
$requirementsFile = Join-Path $scriptDir "requirements.txt"

if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
    throw "Python interpreter not found: $PythonExe"
}

Write-Host "Creating virtual environment in: $venvDir"
& $PythonExe -m venv $venvDir

$venvPython = Join-Path $venvDir "Scripts\python.exe"

Write-Host "Upgrading pip"
& $venvPython -m pip install --upgrade pip

Write-Host "Installing dependencies from requirements.txt"
& $venvPython -m pip install -r $requirementsFile

Write-Host ""
Write-Host "Virtual environment is ready."
Write-Host "Activate it with: .\venv\Scripts\Activate.ps1"
