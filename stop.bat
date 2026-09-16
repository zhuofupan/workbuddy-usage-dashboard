@echo off
setlocal
cd /d "%~dp0"

rem Locate a Python interpreter (see start.bat for the full explanation).
set "PY="
set "PYDIR=%USERPROFILE%\.workbuddy\binaries\python\versions"
if exist "%PYDIR%\" for /d %%D in ("%PYDIR%\*") do if not defined PY if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
if not defined PY set "PY=python"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

"%PY%" "%CD%\server.py" --stop

echo.
echo   Press any key to close this window.
pause >nul
