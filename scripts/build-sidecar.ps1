param(
  [string]$Python = "",
  [switch]$SkipInstallCheck
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$EntryPoint = Join-Path $RepoRoot "src\beautiful_linkedin\server\__main__.py"
$DistPath = Join-Path $RepoRoot "dist"
$WorkPath = Join-Path $RepoRoot "build\pyinstaller"

if (-not $Python) {
  $VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
  if (Test-Path $VenvPython) {
    $Python = $VenvPython
  } else {
    $Python = "python"
  }
}

$PythonVersion = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($PythonVersion -match "^(3\.1[3-9]|[4-9]\.)") {
  Write-Warning "Python $PythonVersion can package the current checkout, but Python 3.11/3.12 is recommended for release builds because rookiepy may fail to install from scratch on newer interpreters."
}

if (-not $SkipInstallCheck) {
  & $Python -c "import PyInstaller" 2>$null
  if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed for '$Python'. Use Python 3.11/3.12 for release builds, then run: $Python -m pip install -e `".[server,dev,risky,scrapling]`""
  }
}

& $Python -m PyInstaller `
  --noconfirm `
  --clean `
  --name beautiful-linkedin-sidecar `
  --onedir `
  --console `
  --paths (Join-Path $RepoRoot "src") `
  --distpath $DistPath `
  --workpath $WorkPath `
  --collect-all beautiful_linkedin `
  --collect-all fastapi `
  --collect-all uvicorn `
  --collect-all pydantic `
  --collect-all pandas `
  --collect-all playwright `
  --collect-all jwt `
  --collect-all cryptography `
  $EntryPoint

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$SidecarExe = Join-Path $DistPath "beautiful-linkedin-sidecar\beautiful-linkedin-sidecar.exe"
if (-not (Test-Path $SidecarExe)) {
  throw "Expected sidecar executable was not created: $SidecarExe"
}

Write-Host "Built sidecar: $SidecarExe"
