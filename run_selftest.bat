@echo off
cd /d %~dp0
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python test_risk_guard.py
python hyperliquid_bot.py --selftest
