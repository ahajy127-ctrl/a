#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 test_risk_guard.py
python3 hyperliquid_bot.py --selftest
