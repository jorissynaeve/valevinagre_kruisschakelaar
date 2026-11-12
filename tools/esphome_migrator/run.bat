@echo off
rem Start de ESPHome-migratietool. De eerste keer maakt dit een omgeving met ESPHome aan (enkele minuten).
setlocal
set "VENV=%LOCALAPPDATA%\kk-esphome\venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo Eerste keer: omgeving met ESPHome aanmaken...
  py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%" || (echo Python 3.11 of nieuwer is nodig. & pause & exit /b 1)
  "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install --quiet esphome || (echo Installatie van ESPHome mislukt. & pause & exit /b 1)
)
"%VENV%\Scripts\python.exe" "%~dp0app.py"
if errorlevel 1 pause
