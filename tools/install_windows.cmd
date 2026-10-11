@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1" %*
set "setup_exit=%errorlevel%"
pause
exit /b %setup_exit%
