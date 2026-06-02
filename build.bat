@echo off
echo ========================================
echo  SyncFlow v1.1.0 - Build .exe
echo ========================================

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    pause
    exit /b 1
)

:: Install dependencies if needed
echo Installing/checking dependencies...
pip install pyinstaller xxhash PyQt6 --quiet

:: Build single-file .exe (PyQt6 version)
echo Building SyncFlow_v1.1.0.exe ...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "SyncFlow_v1.1.0" ^
    --icon NONE ^
    filesync_qt.py --clean

if errorlevel 1 (
    echo.
    echo BUILD FAILED - check errors above
    pause
    exit /b 1
)

echo.
echo ========================================
echo  SUCCESS! Executable is at:
echo  dist\SyncFlow_v1.1.0.exe
echo ========================================
pause
