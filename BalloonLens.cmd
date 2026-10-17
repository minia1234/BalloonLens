@echo off
setlocal
set "desktop_python=%~dp0helper\.venv\Scripts\pythonw.exe"
if not exist "%desktop_python%" (
    echo Run Setup.cmd first to prepare BalloonLens.
    pause
    exit /b 1
)
pushd "%~dp0helper"
start "" "%desktop_python%" -m balloonlens.desktop
popd
endlocal
