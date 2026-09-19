@echo off
rem ===================================================================
rem  build_exe.bat - package autofish.py into a windowless AutoFish.exe
rem
rem  Usage:
rem    build_exe.bat            build with auto UAC elevation (default)
rem    build_exe.bat noadmin    build WITHOUT the admin manifest
rem
rem  Output: the same folder as this script -> AutoFish.exe
rem  NOTE: keep this file pure ASCII. cmd.exe parses .bat with the
rem        console code page, so non-ASCII bytes show up as mojibake.
rem ===================================================================
setlocal
title Build AutoFish.exe

set "HERE=%~dp0"
set "SRC=%HERE%autofish.py"
set "SCRATCH=%HERE%..\.workbuddy\scratch\pyi"

rem Resolve a Python interpreter: prefer a local venv, then fall back to `python` on PATH.
set "PY="
if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if not defined PY if exist "%HERE%venv\Scripts\python.exe" set "PY=%HERE%venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)

rem ---------------------------------------------------------------
rem  IMPORTANT: %~dp0 always ends with a backslash. Writing
rem      --distpath "%~dp0"
rem  expands to  "D:\...\Fishing\"  and the backslash ESCAPES the
rem  closing quote, so the argument never terminates and swallows
rem  every following argument - including the script name. PyInstaller
rem  then dies with:
rem      error: the following arguments are required: scriptname
rem  Appending a dot removes the trailing backslash without changing
rem  which directory it is.
rem ---------------------------------------------------------------
set "DIST=%HERE%."

if not defined PY (
  echo [!] Python not found. Install Python and add it to PATH,
  echo     or create a virtualenv named .venv next to this script.
  echo     Or run:  python -m PyInstaller ...
  pause
  exit /b 1
)

if not exist "%SRC%" (
  echo [!] autofish.py not found next to this script:
  echo     %SRC%
  pause
  exit /b 1
)

rem Default: embed a manifest asking for administrator rights.
rem The game (Delta Force / ACE) runs elevated, and Windows UIPI silently
rem drops mouse input injected from a non-elevated process - so without
rem elevation the program looks like it runs but does nothing in game.
set "UAC=--uac-admin"
if /i "%~1"=="noadmin" set "UAC="

if "%UAC%"=="" (echo [i] manifest: none) else (echo [i] manifest: %UAC%)
echo [i] source  : %SRC%
echo [i] output  : %DIST%
echo.

"%PY%" -m PyInstaller ^
  --noconfirm --clean ^
  --onefile --windowed ^
  --name AutoFish ^
  --distpath "%DIST%" ^
  --workpath "%SCRATCH%" ^
  --specpath "%SCRATCH%" ^
  --hidden-import soundcard.mediafoundation ^
  %UAC% ^
  "%SRC%"

if errorlevel 1 (
  echo.
  echo [!] Build FAILED - see the PyInstaller output above.
  pause
  exit /b 1
)

echo.
echo [OK] %HERE%AutoFish.exe
pause
