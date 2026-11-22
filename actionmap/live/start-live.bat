@echo off
rem Nodach Live window (click to talk). The old command-line client is start-live-cli.bat.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw live_gui.py %*
  exit /b 0
)
python live_gui.py %*
if errorlevel 1 pause
