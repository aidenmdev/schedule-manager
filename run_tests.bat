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
echo === Syncing between computers ===
".venv\Scripts\python.exe" -m unittest tests.test_sync
".venv\Scripts\python.exe" -m unittest tests.test_installer
".venv\Scripts\python.exe" -m unittest tests.test_updater
echo.
echo === Tablet page and server ===
".venv\Scripts\python.exe" -m unittest tests.test_tablet
echo.
echo === App startup edge cases ===
".venv\Scripts\python.exe" -m unittest tests.test_startup
echo.
echo === The real app against fake Google (windows will flash open and closed) ===
".venv\Scripts\python.exe" -m unittest tests.test_gui
".venv\Scripts\python.exe" -m unittest tests.test_gui_sync
".venv\Scripts\python.exe" -m unittest tests.test_gui_updates
echo.
pause
