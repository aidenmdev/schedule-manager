@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"
echo.
echo Done. Look for "Schedule Manager v2" on your Desktop and in the Start menu.
pause
