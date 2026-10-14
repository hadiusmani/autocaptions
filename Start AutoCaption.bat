@echo off
REM AutoCaption launcher - double-click to start.
title AutoCaption
cd /d "%~dp0"

REM ---- ffmpeg ----
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Installing ffmpeg ^(one time^)...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  for /f "delims=" %%P in ('powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"') do set "PATH=%%P"
)
where ffmpeg >nul 2>nul || (echo ffmpeg install did not finish. Close this window and double-click again. & pause & exit /b 1)

REM ---- python ----
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY ( python --version >nul 2>nul && set "PY=python" )
if not defined PY (
  echo Installing Python ^(one time^)...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  for /f "delims=" %%P in ('powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"') do set "PATH=%%P"
  py -3 --version >nul 2>nul && set "PY=py -3"
)
if not defined PY (echo Python install did not finish. Close this window and double-click again. & pause & exit /b 1)

REM ---- app environment ----
if not exist .venv\Scripts\python.exe (
  echo Setting up AutoCaption ^(one time, a minute or two^)...
  %PY% -m venv .venv
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q -r requirements.txt || (echo Setup failed. & pause & exit /b 1)
)
.venv\Scripts\python app.py
pause
