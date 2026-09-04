@echo off
setlocal EnableExtensions
title Hyperliquid - Backtest (DO NOT CLOSE)
cd /d "%~dp0"

set "LOG=%~dp0backtest_run.log"
echo ========================================== > "%LOG%"
echo Hyperliquid Backtest Run >> "%LOG%"
echo Started: %date% %time% >> "%LOG%"
echo ========================================== >> "%LOG%"

echo.
echo ==========================================
echo   HYPERLIQUID BACKTEST
echo   The window will STAY OPEN on errors.
echo ==========================================
echo.

where py >nul 2>&1
if %errorlevel%==0 (
    set "PY=py"
) else (
    where python >nul 2>&1
    if %errorlevel%==0 (set "PY=python") else goto :nopython
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating Python environment...
    echo Creating environment...>>"%LOG%"
    %PY% -m venv .venv >>"%LOG%" 2>&1
    if errorlevel 1 goto :fail
)

echo [2/4] Checking/installing packages...
echo Checking packages...>>"%LOG%"
".venv\Scripts\python.exe" -m pip install -r requirements.txt >>"%LOG%" 2>&1
if errorlevel 1 goto :fail

if exist "preflight.py" (
    echo [3/4] Running preflight...
    ".venv\Scripts\python.exe" preflight.py >>"%LOG%" 2>&1
    if errorlevel 1 goto :fail
) else (
    echo [3/4] Preflight file not found - continuing...
)

echo [4/4] Starting backtest...
echo Running backtest...>>"%LOG%"
echo.
".venv\Scripts\python.exe" backtest_lab.py --coins BTC,ETH,SOL --days 180 --walk-forward 2>&1
set "RC=%errorlevel%"
echo Exit code: %RC%>>"%LOG%"

echo.
if "%RC%"=="0" goto :success

:fail
echo.
echo ==========================================
echo   BACKTEST STOPPED
echo ==========================================
echo.
echo Error code: %errorlevel%
echo.
echo A log was saved here:
echo %LOG%
echo.
echo IMPORTANT: This window will stay open.
echo Send me a screenshot of this window
echo OR send me the file backtest_run.log
echo.
pause
exit /b 1

:nopython
echo.
echo Python is not installed or is not in PATH.
echo Install Python 3.11 or 3.12, then run this file again.
echo.
echo Download: https://www.python.org/downloads/
echo.
pause
exit /b 1

:success
echo.
echo ==========================================
echo   BACKTEST FINISHED SUCCESSFULLY
echo ==========================================
echo.
if exist "research_data\backtest_report.json" (
    echo Report found:
    echo research_data\backtest_report.json
    echo.
    start "" "%~dp0research_data\backtest_report.json"
) else (
    echo The program finished, but the report file was not found.
    echo Check backtest_run.log
)
echo.
pause
exit /b 0
