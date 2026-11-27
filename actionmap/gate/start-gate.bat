@echo off
rem Nodach Gate window (live list of what Claude does, Pause all, reports). Claude desktop also opens it.
setlocal
cd /d "%~dp0"
where pyw >nul 2>nul && pyw -3.14 -c "import sys" >nul 2>nul && (start "" pyw -3.14 "%~dp0gate_gui.py" & exit /b 0)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0gate_gui.py" & exit /b 0)
python "%~dp0gate_gui.py"
