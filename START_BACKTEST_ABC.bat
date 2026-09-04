@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ==========================================
echo   HYPERLIQUID A/B/C RESEARCH BACKTEST
echo   V1.6.3 RESEARCH-C - WINDOWS
echo   A=baseline  B=regime filter  C=MTF direction filter
echo   C is RESEARCH ONLY - no live orders
echo ==========================================
echo.

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [1/4] Creating project-local Python environment...
  py -3 -m venv "%~dp0.venv"
  if errorlevel 1 goto :ERR
) else (
  echo [1/4] Project-local Python environment found.
)

if not exist "%PY%" (
  echo ERROR: Project Python was not created: "%PY%"
  goto :ERR
)

echo [2/4] Installing/checking packages in THIS environment...
"%PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :ERR

"%PY%" -c "import numpy,requests; import hyperliquid; print('Dependency check: OK')"
if errorlevel 1 goto :ERR

echo [3/4] Running preflight...
"%PY%" "%~dp0preflight.py"
if errorlevel 1 goto :ERR

if not exist "%~dp0research_data" mkdir "%~dp0research_data"

echo [4/4] Starting A/B/C research backtest...
echo This may take a while. Do not close this window.
echo.
"%PY%" -u "%~dp0backtest_ab.py" --coins BTC,ETH,SOL --days 365
set "BT_ERR=%ERRORLEVEL%"

if not "%BT_ERR%"=="0" goto :ERR

echo.
echo ==========================================
echo   A/B/C BACKTEST FINISHED SUCCESSFULLY
echo   Report: research_data\ab_backtest_report.json
echo ==========================================
echo.
pause
exit /b 0

:ERR
echo.
echo ==========================================
echo   BACKTEST FAILED
 echo  Exit code: %BT_ERR%
echo ==========================================
echo.
if exist "%~dp0research_data\ab_backtest_console.txt" (
  echo -------- LAST LOG --------
  type "%~dp0research_data\ab_backtest_console.txt"
  echo -------- END LOG --------
)
pause
exit /b 1
