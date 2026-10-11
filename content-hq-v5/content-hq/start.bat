@echo off
rem Starts Content HQ and opens the dashboard. Double-click this file.
cd /d "%~dp0"
start "" /min cmd /c "timeout /t 4 >nul & start http://localhost:8000"
.venv\Scripts\python app.py
pause
