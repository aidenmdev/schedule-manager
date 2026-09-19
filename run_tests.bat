@echo off
rem Runs every automated test (no real Google account is touched). Takes about 2-3 minutes.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run "Schedule Manager.vbs" once first so the private Python environment gets built.
  pause
  exit /b 1
)
echo === Core logic ===
".venv\Scripts\python.exe" -m unittest tests.test_core
echo.
echo === App startup edge cases ===
".venv\Scripts\python.exe" -m unittest tests.test_startup
echo.
echo === The real app against fake Google (windows will flash open and closed) ===
".venv\Scripts\python.exe" -m unittest tests.test_gui
echo.
pause
