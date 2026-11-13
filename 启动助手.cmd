@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  set "HELPER_PYTHON=%~dp0.venv\Scripts\python.exe"
  set "HELPER_PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
) else (
  set "HELPER_PYTHON=python"
  set "HELPER_PYTHONW=pythonw"
)
"%HELPER_PYTHON%" -c "import tkinter; import playwright.sync_api; import markdown_it" >nul 2>&1
if errorlevel 1 (
  echo Python with tkinter, playwright and markdown-it-py is required.
  echo See README.md for installation instructions.
  pause
  exit /b 1
)
where "%HELPER_PYTHONW%" >nul 2>&1
if errorlevel 1 (
  start "" "%HELPER_PYTHON%" "%~dp0app.py"
) else (
  start "" "%HELPER_PYTHONW%" "%~dp0app.py"
)
