@echo off
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -like '*tablet_server.py*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; 'Tablet display stopped.' } else { 'It was not running.' }"
timeout /t 3 >nul
