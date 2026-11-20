@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PHOTOCHECK_PYTHON="
if exist "runtime\python.exe" set "PHOTOCHECK_PYTHON=%CD%\runtime\python.exe"
if not defined PHOTOCHECK_PYTHON if exist ".venv\Scripts\python.exe" set "PHOTOCHECK_PYTHON=%CD%\.venv\Scripts\python.exe"
if not defined PHOTOCHECK_PYTHON if exist "D:\Program Files\PhotoCheck\venv\Scripts\python.exe" set "PHOTOCHECK_PYTHON=D:\Program Files\PhotoCheck\venv\Scripts\python.exe"

if defined PHOTOCHECK_PYTHON (
    "%PHOTOCHECK_PYTHON%" "app\main.py"
    goto :after_run
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 "app\main.py"
    goto :after_run
)

where python >nul 2>nul
if not errorlevel 1 (
    python "app\main.py"
    goto :after_run
)

echo [ERROR] Python 3.11 was not found.
echo Install Python 3.11 or create .venv, then run this file again.
pause
exit /b 1

:after_run
if errorlevel 1 pause
exit /b %errorlevel%
