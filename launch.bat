@echo off
cd /d "%~dp0"

:: 优先用无控制台的 pythonw / pyw 启动（start 分离后本窗口立即关闭，
:: 只会闪一下；完全无闪可给 "pythonw -m tcer" 建桌面快捷方式）。
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 -m tcer
    goto :end
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw -m tcer
    goto :end
)

:: 回退：带控制台的 python（至少能启动，且启动错误可见）
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m tcer
    if %errorlevel%==0 goto :end
)
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
