@echo off
cd /d "%~dp0"

:: Prefer `py` launcher (Windows Python installs it automatically)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m tcer
    if %errorlevel%==0 goto :end
)

:: Fallback: plain `python`
where python >nul 2>nul
if %errorlevel%==0 (
    python -m tcer
    if %errorlevel%==0 goto :end
)

echo.
echo [ERROR] Python not found.
echo Please install Python 3.11+ from https://www.python.org/downloads/
echo Make sure to check "Add python.exe to PATH" during installation.
echo.
pause

:end
