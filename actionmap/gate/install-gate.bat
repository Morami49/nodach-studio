@echo off
rem Nodach Gate: one-click install. Double-click this file.
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && py -3.14 -c "import sys" >nul 2>nul && set "PY=py -3.14"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.14 isn't installed. Get it from https://www.python.org/downloads/windows/
  echo ^(tick "Add python.exe to PATH" and keep "tcl/tk" ticked^), then run this again.
  pause
  exit /b 1
)
%PY% "%~dp0install_gate.py"
echo.
pause
