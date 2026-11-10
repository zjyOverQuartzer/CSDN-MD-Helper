@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating the project virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 goto :failed
)

echo Checking build dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto :failed

if not exist "build" mkdir "build"
for /f %%V in ('call ".venv\Scripts\python.exe" -c "import sys; print(str(sys.version_info.major) + str(sys.version_info.minor))"') do set "CSDN_BUILD_PYTHON=%%V"
if not defined CSDN_BUILD_PYTHON goto :failed
echo Building the single-file application. Please wait...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --name CSDN-Markdown-Helper-v5 --manifest "%~dp0dpi-aware.manifest" --distpath dist --workpath "build\pyinstaller-py%CSDN_BUILD_PYTHON%" --specpath build --paths . packaged_main.py
if errorlevel 1 goto :failed

echo.
echo Build complete: dist\CSDN-Markdown-Helper-v5.exe
echo Double-click the exe to start. Edge or Chrome must be installed.
pause
exit /b 0

:failed
echo.
echo Build failed. Check the errors above.
pause
exit /b 1
