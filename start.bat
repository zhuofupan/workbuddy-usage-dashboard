@echo off
chcp 65001 >nul 2>&1
setlocal
title WorkBuddy Usage Dashboard
cd /d "%~dp0"

rem Locate a Python interpreter:
rem   1) the one WorkBuddy ships under %USERPROFILE%\.workbuddy\binaries\python\versions
rem   2) otherwise whatever "python" resolves to on PATH
set "PY="
set "PYDIR=%USERPROFILE%\.workbuddy\binaries\python\versions"
if exist "%PYDIR%\" for /d %%D in ("%PYDIR%\*") do if not defined PY if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
if not defined PY set "PY=python"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo.
echo   Starting WorkBuddy usage dashboard ...
echo   A browser window will open by itself. Keep this window open to stop it later.
echo.

"%PY%" "%CD%\server.py" --port 8791 --open
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo   Exited with code %RC% - see dashboard.log for details.
echo   Dashboard stopped. Press any key to close this window.
pause >nul
