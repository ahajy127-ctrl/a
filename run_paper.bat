@echo off
cd /d %~dp0
set MODE=paper
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python hyperliquid_bot.py
