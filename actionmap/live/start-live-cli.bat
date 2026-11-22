@echo off
rem Nodach Live, command-line version (the window is start-live.bat)
cd /d "%~dp0"
python live_client.py %*
pause
