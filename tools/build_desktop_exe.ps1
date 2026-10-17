# Package the launcher only. The app reuses the installed helper environment and its OCR models.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
$python = Join-Path $root 'helper\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run Setup.cmd first to prepare the Python environment.' }
$work = Join-Path $root 'build\desktop-launcher'
& $python -m PyInstaller --noconfirm --onefile --windowed --name BalloonLens `
    --distpath (Join-Path $root 'release') --workpath $work --specpath $work (Join-Path $root 'tools\desktop_launcher.py')
if ($LASTEXITCODE -ne 0) { throw 'Desktop packaging failed. PyInstaller must be installed in helper\.venv.' }
Copy-Item -LiteralPath (Join-Path $root 'release\BalloonLens.exe') -Destination (Join-Path $root 'BalloonLens.exe')
Write-Host 'Built BalloonLens.exe in the project folder and release folder.'
