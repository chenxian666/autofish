@echo off
chcp 65001 >nul
title AutoFish

rem Delta Force runs elevated because of its anti-cheat (ACE). Windows UIPI then
rem silently drops our injected clicks unless we are elevated too. Re-launch as admin.
net session >nul 2>&1
if errorlevel 1 (
  echo Requesting administrator privileges...
  set "AF_SELF=%~f0"
  powershell -NoProfile -Command "Start-Process -FilePath $env:AF_SELF -Verb RunAs"
  if errorlevel 1 echo [!] Elevation was declined. Clicks will NOT reach the game.
  exit /b
)

set "PYTHONIOENCODING=utf-8"
rem Resolve a Python interpreter: prefer a local venv, then fall back to `python` on PATH.
rem Keep this generic so the repo works on any machine (no hard-coded user paths).
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [!] Python not found. Install Python and add it to PATH,
  echo     or create a virtualenv named .venv next to this script.
  echo     Fall back to: python autofish.py
  pause
  exit /b 1
)
"%PY%" "%~dp0autofish.py" %*
if errorlevel 1 pause
