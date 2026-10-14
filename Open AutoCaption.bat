@echo off
REM Opens AutoCaption, starting it first if it is not already running.
cd /d "%~dp0"
powershell -NoProfile -Command "try { Invoke-WebRequest http://127.0.0.1:5055/api/projects -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 ( start "" http://127.0.0.1:5055 & exit /b )
title AutoCaption (close this window to quit)
call "Start AutoCaption.bat"
