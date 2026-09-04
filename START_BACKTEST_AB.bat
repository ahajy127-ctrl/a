@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Hyperliquid A-B Backtest - DO NOT CLOSE
set "LOG=%~dp0ab_backtest_run.log"
echo ========================================== > "%LOG%"
echo HYPERLIQUID A-B BACKTEST >> "%LOG%"
echo Started: %date% %time% >> "%LOG%"
echo ========================================== >> "%LOG%"
where py >nul 2>&1
if %errorlevel%==0 (set "PY=py") else (where python >nul 2>&1 && set "PY=python")
if not defined PY goto :nopython
if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating Python environment...
  %PY% -m venv .venv >>"%LOG%" 2>&1 || goto :fail
)
echo [2/4] Installing/checking packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt >>"%LOG%" 2>&1 || goto :fail
echo [3/4] Running preflight...
".venv\Scripts\python.exe" preflight.py >>"%LOG%" 2>&1 || goto :fail
echo [4/4] Running A/B research backtest (365 days, BTC/ETH/SOL)...
".venv\Scripts\python.exe" backtest_ab.py --coins BTC,ETH,SOL --days 365 >>"%LOG%" 2>&1
set "RC=%errorlevel%"
if not "%RC%"=="0" goto :fail
 echo.
echo ==========================================
echo A/B BACKTEST FINISHED
echo ==========================================
echo Report: research_data\ab_backtest_report.json
echo Log: %LOG%
start "" "%~dp0research_data\ab_backtest_report.json"
pause
exit /b 0
:fail
echo.
echo ==========================================
echo A/B BACKTEST STOPPED
 echo ==========================================
echo Error code: %errorlevel%
echo Log: %LOG%
pause
exit /b 1
:nopython
echo Python 3.11/3.12 not found. Install Python then run again.
pause
exit /b 1
