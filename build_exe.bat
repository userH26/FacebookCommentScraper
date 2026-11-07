@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo   Facebook Comment Scraper - Closed-Source Windows Builder
echo   (Compiles to Native Machine Code + Packages to EXE)
echo ============================================================
echo.

:: 1. Check Python installation
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python is not installed or not in your PATH!
    echo Please install Python 3.10 or 3.11 from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo [1/3] Installing / Updating requirements (Playwright, PyInstaller, Cython)...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo [2/3] Compiling source code and packaging standalone executables...
python build_exe.py
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Build execution failed!
    pause
    exit /b 1
)

echo.
echo [3/3] Build Succeeded!
echo ============================================================
echo Your protected, closed-source executables are ready:
echo   - GUI Desktop App: dist\FacebookCommentScraper_GUI.exe
echo   - Command Line:    dist\FacebookCommentScraper_CLI.exe
echo ============================================================
echo.
echo These executables contain compiled native machine code.
echo No source code or uncompiled Python scripts are included.
echo You can share these standalone binary files directly!
echo.
pause
