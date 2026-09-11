@echo off
cd /d %~dp0
set PAPER_CAPITAL=1000
set DASH_PORT=8080
python v2\paper.py --loop
