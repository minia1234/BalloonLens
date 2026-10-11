# Builds release\BalloonLens-Setup.exe, the one-file setup window (tools\setup_gui.py), with PyInstaller.
# The window needs only the standard library and tkinter, so the executable does not bundle the translation stack.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
$python = Join-Path $root 'helper\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'helper\.venv not found. Create the Python environment first (see README).' }
& $python -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller is missing. Run: helper\.venv\Scripts\python.exe -m pip install pyinstaller' }

$work = Join-Path ([IO.Path]::GetTempPath()) 'balloonlens-setup-build'
& $python -m PyInstaller --noconfirm --onefile --windowed --name BalloonLens-Setup `
    --distpath (Join-Path $root 'release') --workpath $work --specpath $work (Join-Path $root 'tools\setup_gui.py')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
Write-Host "[ok] Built $(Join-Path $root 'release\BalloonLens-Setup.exe')"
