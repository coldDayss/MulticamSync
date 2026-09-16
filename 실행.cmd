@echo off
cd /d "%~dp0"
start "" "%~dp0runtime\pythonw.exe" "%~dp0app.py"
exit /b
