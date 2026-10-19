@echo off
setlocal
if exist "%~dp0BalloonLens.exe" (
    start "" "%~dp0BalloonLens.exe"
    exit /b 0
)
set "desktop_python=%~dp0helper\.venv\Scripts\pythonw.exe"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup_windows.ps1" -EnsureReady
if errorlevel 1 (
    pause
    exit /b 1
)
pushd "%~dp0helper"
start "" "%desktop_python%" -m balloonlens.desktop
popd
endlocal
