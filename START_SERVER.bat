@echo off
title LANDesk Server
echo ============================================
echo   LANDesk Server  -  Run on the PC to control
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
pip install mss Pillow pynput pyperclip cryptography -q --exists-action i
echo.

:: Ask for the password rather than keeping one in this file. It goes to the
:: server in the environment, not on the command line, because a command line
:: is visible to every other user on the machine.
set "LANDESK_PASSWORD="
set /p LANDESK_PASSWORD=Connection password:
if not defined LANDESK_PASSWORD (
    echo.
    echo No password entered. The server will not start without one.
    echo Run it by hand with --no-password if that is really what you want.
    pause
    exit /b 1
)
echo.

:: --host 0.0.0.0 accepts connections from anywhere on your network, which is
:: what this launcher is for. Leave it out to accept only this machine.
python "%~dp0server\server.py" ^
    --host 0.0.0.0 ^
    --port 9000 ^
    --quality 55 ^
    --fps 20

set "LANDESK_PASSWORD="
pause
