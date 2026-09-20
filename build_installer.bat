@echo off
rem Builds "Schedule Manager Setup.exe" (in the dist folder) that installs the app on other computers.
cd /d "%~dp0"
if not exist ".buildvenv\Scripts\python.exe" (
  py -3 -m venv .buildvenv
  if errorlevel 1 (
    echo Python 3.10 or newer is needed to build the installer.
    pause
    exit /b 1
  )
)
".buildvenv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".buildvenv\Scripts\python.exe" -m pip install --quiet -r requirements.txt pyinstaller
if errorlevel 1 (
  echo Installing the build tools failed. Check your internet connection.
  pause
  exit /b 1
)
".buildvenv\Scripts\python.exe" installeruild.py
echo.
pause
