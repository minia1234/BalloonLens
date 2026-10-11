@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m balloonlens.native_host %*
