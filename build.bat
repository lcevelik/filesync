@echo off
echo ========================================
echo  FileSync - Build .exe
echo ========================================

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    pause
    exit /b 1
)

:: Install PyInstaller if needed
echo Installing/checking PyInstaller...
pip install pyinstaller --quiet

:: Build single-file .exe
echo Building filesync.exe ...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "FileSync" ^
    --icon NONE ^
    filesync.py

if errorlevel 1 (
    echo.
    echo BUILD FAILED - check errors above
    pause
    exit /b 1
)

echo.
echo ========================================
echo  SUCCESS! Executable is at:
echo  dist\FileSync.exe
echo ========================================
pause
