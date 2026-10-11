@echo off
rem One-time setup for Windows. Double-click this file.
cd /d "%~dp0"
if not exist .venv ( python -m venv .venv )
.venv\Scripts\python -m pip install -r requirements.txt
if not exist .env (
  copy .env.example .env >nul
  echo.
  echo Created the .env file. Open it in Notepad and add your keys, then double-click start.bat
)
echo.
echo Setup finished.
pause
