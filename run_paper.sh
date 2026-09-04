#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
MODE=paper python3 hyperliquid_bot.py
