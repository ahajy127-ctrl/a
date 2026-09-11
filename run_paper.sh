#!/usr/bin/env bash
# Paper trading for Portfolio v4 (v2/paper.py). No exchange account needed. Ctrl-C to stop; state is in v2/state/.
set -euo pipefail
cd "$(dirname "$0")"
export PAPER_CAPITAL="${PAPER_CAPITAL:-1000}"   # virtual capital in $
# optional Telegram alerts: export TG_TOKEN=... TG_CHAT=...
export DASH_PORT="${DASH_PORT:-8080}"           # web dashboard: http://<ip>:8080  (0 = off)
exec python3 v2/paper.py --loop
