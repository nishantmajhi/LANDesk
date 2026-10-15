@echo off
title LANDesk Viewer
echo ============================================
echo   LANDesk Viewer  -  Run on the PC you sit at
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.9+ from python.org
    pause
    exit /b 1
)

:: Install dependencies if needed
echo Checking dependencies...
pip install Pillow pyperclip -q --exists-action i
echo.

:: Launch viewer (a connect dialog will appear)
python "%~dp0viewer\viewer.py"

pause
