@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist "build\release-env\Scripts\python.exe" (
    echo Creating an isolated build environment...
    py -3 -m venv "build\release-env"
    if errorlevel 1 goto :failed
)

echo Checking build dependencies...
"build\release-env\Scripts\python.exe" -m pip install --no-deps -r requirements-build.txt
if errorlevel 1 goto :failed
"build\release-env\Scripts\python.exe" -m pip check
if errorlevel 1 goto :failed

"build\release-env\Scripts\python.exe" build_release.py
if errorlevel 1 goto :failed

echo.
echo Build complete. See the output paths above.
echo Edge or Chrome must be installed to use the application.
pause
exit /b 0

:failed
echo.
echo Build failed. Check the errors above.
pause
exit /b 1
