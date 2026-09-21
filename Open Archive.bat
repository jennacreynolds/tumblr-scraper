@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (py -3 open_archive.py %*) else (python open_archive.py %*)
pause
