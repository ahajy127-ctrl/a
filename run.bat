@echo off
setlocal
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  py -3 -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt || exit /b 1
python preflight.py || exit /b 1
python hyperliquid_bot.py
