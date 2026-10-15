@echo off
setlocal EnableDelayedExpansion
title LANDesk - Build EXEs
color 0A

echo.
echo  ============================================================
echo    LANDesk Build Script  -  Creates standalone .exe files
echo    Run this ONCE on any PC that has Python installed.
echo    The output EXEs need NO Python on the target machine.
echo  ============================================================
echo.

:: ── Check Python ─────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo         Install Python 3.9+ from https://python.org
    echo         Make sure to tick "Add Python to PATH" during install.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version') do echo [OK] Found %%v

:: ── Install build tools + runtime deps ───────────────────────────────────────
:: Use "python -m pip" instead of bare "pip" so it works even when pip's
:: Scripts folder is not on PATH (common with user installs on Windows).
echo.
echo [STEP 1/4] Installing dependencies...
python -m pip install pyinstaller mss Pillow pynput pyperclip cryptography --quiet --exists-action i
if errorlevel 1 (
    echo [ERROR] pip install failed. Check your internet connection.
    pause
    exit /b 1
)
echo [OK] Dependencies installed.

:: ── Optional: install UPX for smaller EXEs ───────────────────────────────────
echo.
echo [INFO] Checking for UPX (optional compressor)...
where upx >nul 2>&1
if errorlevel 1 (
    echo [INFO] UPX not found - EXEs will still work, just slightly larger.
    echo        To install: https://github.com/upx/upx/releases
    echo        Extract upx.exe to C:\Windows\System32 or any folder in PATH.
) else (
    echo [OK] UPX found - will compress executables.
)

:: ── Clean previous build artifacts ───────────────────────────────────────────
echo.
echo [STEP 2/4] Cleaning previous builds...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
echo [OK] Cleaned.

:: ── Build Server EXE ─────────────────────────────────────────────────────────
echo.
echo [STEP 3/4] Building LANDesk-Server.exe...
echo            (this takes 30-90 seconds)
echo.
python -m PyInstaller landesk_server.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo [ERROR] Server build failed! See output above for details.
    pause
    exit /b 1
)
echo.
echo [OK] Server EXE built.

:: ── Build Viewer EXE ─────────────────────────────────────────────────────────
echo.
echo [STEP 4/4] Building LANDesk-Viewer.exe...
echo            (this takes 30-90 seconds)
echo.
python -m PyInstaller landesk_viewer.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo [ERROR] Viewer build failed! See output above for details.
    pause
    exit /b 1
)

:: ── Collect outputs ───────────────────────────────────────────────────────────
echo.
echo [DONE] Collecting output files...
if not exist "dist\release" mkdir "dist\release"
copy /Y "dist\LANDesk-Server.exe" "dist\release\LANDesk-Server.exe" >nul
copy /Y "dist\LANDesk-Viewer.exe" "dist\release\LANDesk-Viewer.exe" >nul

:: Show file sizes
for %%f in ("dist\release\LANDesk-Server.exe") do set SERVER_SIZE=%%~zf
for %%f in ("dist\release\LANDesk-Viewer.exe") do set VIEWER_SIZE=%%~zf
set /a SERVER_MB=!SERVER_SIZE! / 1048576
set /a VIEWER_MB=!VIEWER_SIZE! / 1048576

echo.
echo  ============================================================
echo    BUILD SUCCESSFUL!
echo  ============================================================
echo.
echo    Output folder:  %~dp0dist\release\
echo.
echo    LANDesk-Server.exe   ~!SERVER_MB! MB   (run on PC to be controlled)
echo    LANDesk-Viewer.exe   ~!VIEWER_MB! MB   (run on PC you sit at)
echo.
echo    Next steps:
echo    1. Copy BOTH files to wherever you need them via Nearby Sharing
echo    2. On the PC to control: run LANDesk-Server.exe
echo    3. On your PC: run LANDesk-Viewer.exe
echo    4. No Python required on either machine!
echo  ============================================================
echo.
explorer "dist\release"
pause
