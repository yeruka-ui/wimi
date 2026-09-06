@echo off
REM Build WimiWatcher.exe with PyInstaller.
REM Requires: pip install pyinstaller  (plus the runtime deps in requirements.txt)

cd /d "%~dp0"

echo Cleaning previous build...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

echo Building...
python -m PyInstaller --clean --noconfirm WimiWatcher.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Done. Exe at: dist\WimiWatcher.exe
echo Place it in this folder next to config.json when distributing.
