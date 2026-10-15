@echo off
cd /d "%~dp0"
title Incident Pilot
echo Incident Pilot ve uc demo servis hazirlaniyor...
where python >nul 2>&1
if errorlevel 1 (
  echo Python 3.10 veya ustunu kurun.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" run.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo Baslatma tamamlanamadi. data klasorundeki loglari kontrol edin.
pause
