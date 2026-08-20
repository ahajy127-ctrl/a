#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hyperliquid Trading Bot v26 (always-on Paper engine + optional Live engine)
============================================================================
- Port of the Nobitex v25 strategy to Hyperliquid (decentralized perps).
- ONE unified exit engine used by: paper trades, live trades and backtests.
- PAPER engine (virtual $100) ALWAYS runs and validates signals — no risk.
- LIVE engine activates from Settings/.env and trades real perps on
  Hyperliquid with an AGENT WALLET (EIP-712 signed, official SDK).
  Your funds stay in your Rabby wallet; the agent can only trade, never withdraw.

Safety layers:
  - Daily loss limit (8%), circuit breaker, crash guard, checkpoints
  - Protective trigger stop-loss on the exchange (backstop) per position
  - Min order notional enforced ($10), orderbook-depth sizing, coin whitelist
"""

import subprocess, sys, os, json, time, threading, urllib.parse, socket, hashlib
import logging, math, csv, io, traceback as tb, secrets
from datetime import datetime, timedelta, timezone

# ---------- paths & file logging ----------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, 'app.log')
STATE_FILE = os.path.join(BASE_DIR, 'state.json')
TEHRAN = timezone(timedelta(hours=3, minutes=30))   # pinned (P3-24)

try:  # log rotation: >5MB -> app.log.1
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5 * 1024 * 1024:
        os.replace(LOG_FILE, LOG_FILE + '.1')
except Exception:
    pass

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

def log_exception(context=''):
    try:
        logging.error("%s\n%s", context, tb.format_exc())
    except Exception:
        pass

def fa_now():
    """Current time in Tehran (daily resets & reports use this)."""
    return datetime.now(TEHRAN)

def load_env_file():
    env_path = os.path.join(BASE_DIR, '.env')
    try:
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, _, v = line.partition('=')
                        os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        log_exception('.env parse failed')

load_env_file()

# ---------- packages ----------
if os.environ.get('NO_AUTO_INSTALL'):
    for pkg in ('requests', 'numpy'):
        __import__(pkg)
    try:
        __import__('hyperliquid')
        import eth_account  # noqa
    except Exception:
        pass
else:
    for pkg in ('requests', 'numpy'):
        try:
            __import__(pkg)
        except Exception:
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg, '-q'])
    try:
        import hyperliquid  # noqa
    except Exception:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'hyperliquid-python-sdk', '-q'])

import requests
import numpy as np
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

# ============ Static config (strategy baseline) ============

PAPER_CAPITAL = 100.0        # virtual engine capital (always-on)
RISK_PER_TRADE = 0.08        # conservative starting risk per trade
STOP_LOSS = 0.020
TAKE_PROFIT = 0.030
DESIRED_LEV = {'BTC': 10, 'DEFAULT': 5}
MAX_LEV = 5                  # hard cap for BOTH engines (parity with v25)
LIVE_COIN_WHITELIST = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'LTC']
LIVE_MAX_BOOK_SHARE = 0.10
HL_MIN_ORDER_USD = 10.0      # Hyperliquid perp minimum notional
FEE_RATE = 0.0025            # paper round-trip cost estimate on notional
HL_FEE = 0.0005              # HL taker ~0.035% + buffer (single leg)
DAILY_LOSS_LIMIT = 0.08
SCAN_INTERVAL = 300
POS_CHECK_INTERVAL = 30
TRAIL_GAP = 0.008
RUNNER_TRAIL = 0.010
LADDER1_AT_TP = 0.70
LADDER1_LOCK = 0.40
MAX_POSITIONS = 4
MAX_TOTAL_RISK = 0.25
MAX_TRADE_HOURS = 24
SHORT_TH_EXTRA = 1
LOSS_COOLDOWN = 900
MAX_CONSEC_LOSSES = 3
CONSEC_PAUSE = 7200
SPREAD_MAX = 0.004
STRATEGY_VERSION = 26
SHADOW_MAX_PENDING = 60
SHADOW_TIMEOUT_H = 24

HL_MAINNET = 'https://api.hyperliquid.xyz'
HL_TESTNET = 'https://api.hyperliquid-testnet.xyz'

COIN_FA = {'BTC': 'بیت‌کوین', 'ETH': 'اتریوم', 'SOL': 'سولانا', 'XRP': 'ریپل',
           'DOGE': 'دوج‌کوین', 'TRX': 'ترون', 'ADA': 'کاردانو', 'LTC': 'لایت‌کوین',
           'BNB': 'بی‌ان‌بی', 'DOT': 'پولکادات', 'AVAX': 'آوالانچ',
           'LINK': 'چین‌لینک', 'SHIB': 'شیبا', 'UNI': 'یونی‌سواپ', 'ATOM': 'کازماس',
           'NEAR': 'نیر', 'FIL': 'فایل‌کوین', 'TON': 'تون‌کوین', 'ARB': 'آربیتروم',
           'OP': 'آپتیمیزم', 'XAUT': 'تتر گلد (طلا) 🥇'}
# coins scanned for the PAPER engine (live engine only uses the whitelist)
SCAN_COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'LTC', 'TRX', 'ADA', 'AVAX',
              'ARB', 'ATOM', 'LINK', 'UNI', 'SHIB', 'TON', 'FIL', 'BNB', 'OP']

# ============ Hyperliquid data layer ============

def hl_base_url():
    return HL_TESTNET if os.environ.get('HL_TESTNET', '').strip().lower() == 'true' else HL_MAINNET

_hl_info = [None]
_hl_meta = [None]
_hl_lock = threading.Lock()

def hl_info():
    """Read-only Hyperliquid client (cached)."""
    with _hl_lock:
        if _hl_info[0] is None:
            from hyperliquid.info import Info
            _hl_info[0] = Info(hl_base_url(), skip_ws=True)
        return _hl_info[0]

def hl_meta():
    if _hl_meta[0] is None:
        m = hl_info().meta()
        uni = m.get('universe', [])
        names = [u.get('name') for u in uni]
        _hl_meta[0] = {
            'names': set(names),
            'sz': {u.get('name'): u.get('szDecimals', 4) for u in uni},
            'px': {u.get('name'): u.get('pxDecimals', 2) for u in uni},
        }
    return _hl_meta[0]

def SZ_DECIMALS(coin):
    return hl_meta()['sz'].get(coin, 4)

def PX_DECIMALS(coin):
    return hl_meta()['px'].get(coin, 2)

def valid_coin(coin):
    return coin in hl_meta()['names']

# ============ State ============

state_lock = threading.RLock()
state = {}

def fresh_ledger(name):
    return {
        'name': name,
        'capital': PAPER_CAPITAL if name == 'paper' else None,  # live synced from exchange
        'positions': [],
        'trades': [],
        'equity': [],
        'daily_pnl': 0.0,
        'daily_date': '',
        'consec_losses': 0,
        'cooldown_until': 0.0,
        'trading_paused': False,     # daily-loss-limit pause
        'cp_triggered': False,       # checkpoint pause
        'banked_total': 0.0,
    }

def default_state():
    return {
        'mode': 'paper',             # 'paper': only virtual; 'live': both engines
        'ledgers': {'paper': fresh_ledger('paper'), 'live': fresh_ledger('live')},
        'prices': {}, 'prices_ts': '',
        'price_history': [], 'last_scan_ts': 0.0,
        'total_scans': 0, 'last_watchdog_alert': 0.0,
        'manual_paused': False,
        'logs': [], 'start_time': fa_now().isoformat(), 'status': 'Starting',
        'scan_table': [], 'last_signal': None, 'last_reason': '',
        'regime': None, 'crash_mode': False,
        'funding': {}, 'funding_ts': 0.0,
        'threshold_extra': 0, 'tuned': None, 'genome': None, 'factor_weights': None,
        'coin_banned': {}, 'lev_set': {},
        'hl_account': '', 'hl_agent_ok': False, 'live_reason': '',
        'backstop_enabled': os.environ.get('HL_USE_TRIGGER_SL', 'true').lower() == 'true',
    }

def add_log(msg):
    try:
        logging.info('%s', msg)
        state['logs'].append({'t': fa_now().strftime('%H:%M:%S'), 'm': msg})
        if len(state['logs']) > 80:
            state['logs'] = state['logs'][-80:]
    except Exception:
        pass

def save_state():
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception:
        log_exception('save_state failed')

def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            st = default_state()
            for k, v in saved.items():
                if k in st or k in ('logs', 'shadow_signals', 'coin_banned', 'lev_set'):
                    st[k] = v
            for name in ('paper', 'live'):
                lg = st['ledgers'].get(name)
                if not isinstance(lg, dict):
                    lg = fresh_ledger(name)
                    st['ledgers'][name] = lg
                for k, v in fresh_ledger(name).items():
                    lg.setdefault(k, v)
            state = st
        else:
            state = default_state()
    except Exception:
        log_exception('load_state failed')
        state = default_state()

def get_ledger(name):
    return state.setdefault('ledgers', {}).setdefault(name, fresh_ledger(name))

# ============ small formatting helpers ============

def fmt_price(x):
    try:
        if x is None:
            return '-'
        x = float(x)
        if x >= 1000:
            return f'{x:,.0f}'
        if x >= 10:
            return f'{x:,.2f}'
        return f'{x:.4f}'
    except Exception:
        return str(x)

def fmt_money(x):
    try:
        return f'{float(x):,.2f}$'
    except Exception:
        return '-'

def eff_leverage(coin):
    want = DESIRED_LEV.get(coin, DESIRED_LEV['DEFAULT'])
    return min(int(want), MAX_LEV)

def pf_color(pf):
    try:
        pf = float(pf)
        if pf >= 1.5:
            return 'green'
        if pf >= 1.0:
            return 'orange'
        return 'red'
    except Exception:
        return 'gray'

# ============ Market data (Hyperliquid) ============

RES_MAP = {'5': '5m', '15': '15m', '30': '30m', '60': '1h', '240': '4h', 'D': '1d'}
RES_MS = {'5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000,
          '4h': 14400000, '1d': 86400000}

def get_candles(symbol, resolution='60', count=30, with_volume=False, drop_forming=False):
    try:
        iv = RES_MAP.get(str(resolution), '1h')
        step_ms = RES_MS.get(iv, 3600000)
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (count + 3) * step_ms
        d = hl_info().candles_snapshot(symbol, iv, start_ms, now_ms)
        if not d:
            return (None, None) if with_volume else None
        closes = [float(x['c']) for x in d]
        if drop_forming and len(closes) > 1:
            closes = closes[:-1]
        if with_volume:
            vols = [float(x['v']) for x in d]
            if drop_forming and len(vols) > 1:
                vols = vols[:-1]
            return closes, vols
        return closes
    except Exception:
        return (None, None) if with_volume else None

_candle_cache = {}

def get_candles_cached(coin, resolution='60', count=30, max_age=240, drop_forming=False):
    key = (coin, resolution, count, bool(drop_forming))
    now = time.time()
    hit = _candle_cache.get(key)
    if hit and now - hit[0] < max_age:
        return hit[1]
    c = get_candles(coin, resolution, count, drop_forming=drop_forming)
    if c:
        _candle_cache[key] = (now, c)
    return c

def get_price(coin):
    try:
        m = hl_info().all_mids()
        v = m.get(coin)
        return float(v) if v else None
    except Exception:
        return None

def get_prices():
    """Refresh state['prices'] for all scanned coins; returns True on success."""
    try:
        m = hl_info().all_mids()
        if not m:
            return False
        prices = {}
        for c in SCAN_COINS:
            if c in m:
                try:
                    prices[c] = float(m[c])
                except Exception:
                    pass
        if not prices:
            return False
        with state_lock:
            state['prices'] = prices
            state['prices_ts'] = fa_now().strftime('%H:%M:%S')
            hist = state['price_history']
            hist.append({'t': time.time(), 'p': prices.get('BTC')})
            if len(hist) > 500:
                state['price_history'] = hist[-500:]
        return True
    except Exception:
        log_exception('get_prices')
        return False

def get_orderbook_info(coin):
    try:
        bk = hl_info().l2_snapshot(coin)
        levels = bk.get('levels', [[], []])
        bids = levels[0][:10]
        asks = levels[1][:10]
        if not bids or not asks:
            return None, None, None
        best_bid = float(bids[0]['px'])
        best_ask = float(asks[0]['px'])
        mid = (best_bid + best_ask) / 2
        spread = (best_ask - best_bid) / mid if mid else 0.0
        bid_usd = sum(float(l['px']) * float(l['sz']) for l in bids)
        ask_usd = sum(float(l['px']) * float(l['sz']) for l in asks)
        tot = bid_usd + ask_usd
        ob = (bid_usd - ask_usd) / tot if tot else 0.0
        return ob, spread, tot
    except Exception:
        return None, None, None

def hl_book_depth_ok(coin, order_value_usdt, direction):
    """Check the exchange book can absorb our order (share-based guard)."""
    try:
        bk = hl_info().l2_snapshot(coin)
        levels = bk.get('levels', [[], []])
        side = levels[0] if direction == 'long' else levels[1]
        near = sum(float(l['px']) * float(l['sz']) for l in side[:8])
        if near <= 0:
            return False, near
        if order_value_usdt > near * LIVE_MAX_BOOK_SHARE:
            return False, near
        return True, near
    except Exception:
        return False, None

def update_funding():
    try:
        _, ctxs = hl_info().meta_and_asset_ctxs()
        names = hl_meta()['names']
        f = {}
        for i, c in enumerate(ctxs):
            if i < len(names):
                try:
                    f[list(names)[i]] = float(c.get('funding', 0))
                except Exception:
                    pass
        if f:
            state['funding'] = f
            state['funding_ts'] = time.time()
    except Exception:
        pass

# ---------- market regime + crash guard ----------

def detect_regime():
    closes = get_candles_cached('BTC', '60', 48, max_age=600)
    if not closes or len(closes) < 30:
        return None
    arr = np.array(closes, dtype=float)
    rets = np.diff(arr) / arr[:-1]
    vol = float(np.std(rets[-24:]))
    seg = arr[-24:]
    x = np.arange(len(seg))
    slope = float(np.polyfit(x, seg, 1)[0])
    trend_pct = slope * 24 / float(np.mean(seg))
    net = abs(seg[-1] - seg[0])
    path = float(np.sum(np.abs(np.diff(seg)))) or 1.0
    eff = float(net / path)
    if vol > 0.012:
        regime = 'storm'
    elif eff > 0.35 and trend_pct > 0.008:
        regime = 'trend_up'
    elif eff > 0.35 and trend_pct < -0.008:
        regime = 'trend_down'
    else:
        regime = 'range'
    return {'regime': regime, 'volatility': round(vol * 100, 3),
            'trend_pct': round(trend_pct * 100, 2), 'efficiency': round(eff, 2),
            'updated': fa_now().strftime('%H:%M:%S')}

REGIME_FA = {'trend_up': 'روند صعودی 📈', 'trend_down': 'روند نزولی 📉',
             'range': 'رِنج / خنثی ↔️', 'storm': 'طوفانی / پرنوسان ⛈'}

def crash_guard_active():
    closes = get_candles_cached('BTC', '60', 170, max_age=900)
    if not closes or len(closes) < 24:
        return False
    drop_7d = ((closes[-1] - closes[-168]) / closes[-168]) if len(closes) >= 168 else 0.0
    drop_24h = (closes[-1] - closes[-24]) / closes[-24]
    if drop_7d < -0.07 or drop_24h < -0.05:
        if not state.get('crash_mode'):
            state['crash_mode'] = True
            reason_txt = f"{drop_7d*100:.1f}% در ۷ روز" if drop_7d < -0.07 else f"{drop_24h*100:.1f}% در ۲۴ ساعت"
            add_log(f'CRASH GUARD ON: BTC {reason_txt} - no new entries')
            send_telegram(f'🌊 محافظ سقوط فعال شد: بیت‌کوین {reason_txt}. معامله جدید باز نمیشه.')
        return True
    if state.get('crash_mode'):
        state['crash_mode'] = False
        add_log('Crash guard OFF')
        send_telegram('🌤 بازار آروم شد - محافظ سقوط غیرفعال شد')
    return False

# ============ Scoring / signals / learning layer ============

FACTOR_KEYS = ['rsi', 'rsi_deep', 'momentum', 'trend', 'mtf', 'orderbook',
               'volume', 'funding', 'regime', 'whale_flow', 'volume_climax',
               'falling_knife', 'session']
WEIGHT_MIN, WEIGHT_MAX, WEIGHT_LR = 0.5, 1.5, 0.06
GENOME_DEFAULT = {'rsi_lo': 38, 'rsi_hi': 62, 'sl': STOP_LOSS, 'tp': TAKE_PROFIT,
                  'threshold': 4, 'mtf_req': False}

def get_factor_weights():
    w = state.get('factor_weights')
    if not isinstance(w, dict):
        w = {k: 1.0 for k in FACTOR_KEYS}
        state['factor_weights'] = w
    for k in FACTOR_KEYS:
        w.setdefault(k, 1.0)
    return w

def weighted(points, factor):
    return points * get_factor_weights().get(factor, 1.0)

def get_genome():
    g = state.get('genome') or {}
    merged = dict(GENOME_DEFAULT)
    for k in GENOME_DEFAULT:
        if k in g:
            merged[k] = g[k]
    merged['rsi_lo'] = max(20, min(42, merged['rsi_lo']))
    merged['rsi_hi'] = max(58, min(80, merged['rsi_hi']))
    merged['sl'] = max(0.01, min(0.03, merged['sl']))
    merged['tp'] = max(merged['sl'] * 1.3, min(0.06, merged['tp']))
    merged['threshold'] = max(3, min(6, merged['threshold']))
    return merged

def get_tuned():
    t = state.get('tuned') or {}
    return {'sl': t.get('sl', STOP_LOSS), 'tp': t.get('tp', TAKE_PROFIT),
            'threshold': t.get('threshold', 4)}

def live_threshold():
    base = get_tuned()['threshold'] + state.get('threshold_extra', 0)
    try:
        base += shadow_threshold_adjust()
    except Exception:
        pass
    return max(3, min(6, base))

def _calc_kelly_for_trades(trades):
    if len(trades) < 10:
        return RISK_PER_TRADE
    recent = trades[-30:]
    wins = [t.get('pnl', 0) for t in recent if t.get('pnl', 0) > 0]
    losses = [abs(t.get('pnl', 0)) for t in recent if t.get('pnl', 0) <= 0]
    if not wins or not losses:
        return RISK_PER_TRADE
    wr = len(wins) / len(recent)
    ratio = (np.mean(wins) / np.mean(losses)) if np.mean(losses) > 0 else 1.0
    k = (wr - (1 - wr) / max(ratio, 0.1)) * 0.5
    return float(min(0.15, max(0.05, k if k > 0 else 0.05)))

def kelly_risk(lg_name='paper'):
    k_paper = _calc_kelly_for_trades(get_ledger('paper')['trades'])
    if lg_name == 'live' or state.get('mode') == 'live':
        l_trades = get_ledger('live')['trades']
        if len(l_trades) >= 15:
            k_live = _calc_kelly_for_trades(l_trades)
            return round(0.7 * k_live + 0.3 * k_paper, 4)
    return round(k_paper, 4)

def coin_volatility(coin):
    closes = get_candles_cached(coin, '60', 25, drop_forming=True)
    if not closes or len(closes) < 10:
        return None
    arr = np.array(closes, dtype=float)
    rets = np.diff(arr) / arr[:-1]
    return float(np.std(rets))

def dynamic_levels(coin):
    t = get_tuned()
    base_sl, base_tp = t['sl'], t['tp']
    vol = coin_volatility(coin)
    if vol is None:
        return base_sl, base_tp, 'normal'
    if vol > 0.009:
        return base_sl * 1.5, base_tp * 1.3, 'high'
    if vol < 0.003:
        return base_sl * 0.8, base_tp * 0.8, 'low'
    return base_sl, base_tp, 'normal'

def funding_score_bonus(direction):
    """HL funding-rate tilt: heavy short funding supports longs and vice versa."""
    f = state.get('funding') or {}
    vals = [v for v in f.values() if isinstance(v, (int, float))]
    if not vals:
        return 0, []
    avg = float(np.mean(vals))
    if avg < -0.0002 and direction == 'long':
        return 1, ['فاندینگ منفی (شورت‌ها پرداخت می‌کنن) - حمایت خرید']
    if avg > 0.0002 and direction == 'short':
        return 1, ['فاندینگ مثبت (لانگ‌ها پرداخت می‌کنن) - فشار فروش']
    return 0, []

# ---------- signal scoring ----------

def analyze_coin(coin):
    prices = get_candles_cached(coin, '60', 25, drop_forming=True)
    if not prices or len(prices) < 20:
        return None
    prices = prices[-20:]
    gains, losses = [], []
    for i in range(1, len(prices)):
        ch = prices[i] - prices[i - 1]
        gains.append(max(0, ch))
        losses.append(max(0, -ch))
    ag, al = np.mean(gains[-14:]), np.mean(losses[-14:])
    if ag < 1e-12 and al < 1e-12:
        rsi = 50.0
    else:
        rsi = 100 - (100 / (1 + ag / max(al, 1e-9)))
    mom = (prices[-1] - prices[-5]) / prices[-5] if len(prices) > 5 else 0
    sma7, sma20 = np.mean(prices[-7:]), np.mean(prices[-20:])
    score, direction, reasons, factors = 0, None, [], []
    rsi_anchor = False
    fa = COIN_FA.get(coin, coin)
    g = get_genome()
    if rsi < g['rsi_lo']:
        score += weighted(3, 'rsi')
        direction, rsi_anchor = 'long', True
        factors.append('rsi')
        reasons.append(f'{fa}: RSI اشباع فروش ({rsi:.0f}) (+3)')
        if rsi < g['rsi_lo'] - 10:
            score += weighted(1, 'rsi_deep')
            factors.append('rsi_deep')
            reasons.append(f'{fa}: اشباع عمیق (+1)')
    elif rsi > g['rsi_hi']:
        score += weighted(3, 'rsi')
        direction, rsi_anchor = 'short', True
        factors.append('rsi')
        reasons.append(f'{fa}: RSI اشباع خرید ({rsi:.0f}) (+3)')
        if rsi > g['rsi_hi'] + 10:
            score += weighted(1, 'rsi_deep')
            factors.append('rsi_deep')
            reasons.append(f'{fa}: اشباع عمیق (+1)')
    if direction == 'long' and prices[-1] < prices[-2]:
        score -= 2
        reasons.append(f'{fa}: ⚠️ کندل آخر هنوز نزولیه (-2)')
    elif direction == 'short' and prices[-1] > prices[-2]:
        score -= 2
        reasons.append(f'{fa}: ⚠️ کندل آخر هنوز صعودیه (-2)')
    if direction == 'long' and len(prices) >= 4 and all(prices[i] < prices[i-1] * 0.993 for i in range(-3, 0)):
        score -= 3
        reasons.append(f'{fa}: 🔪 هشدار چاقوی در حال سقوط (۳ کندل متوالی نزول شدید) (-3)')
    elif direction == 'short' and len(prices) >= 4 and all(prices[i] > prices[i-1] * 1.007 for i in range(-3, 0)):
        score -= 3
        reasons.append(f'{fa}: 🚀 هشدار پامپ متوالی (۳ کندل متوالی صعود شدید) (-3)')
    if mom > 0.005:
        if direction == 'short':
            score -= 3
            reasons.append(f'{fa}: 🚫 مومنتوم صعودی خلاف فروش ({mom*100:+.2f}%) (-3)')
        else:
            score += weighted(2, 'momentum')
            if not direction:
                direction = 'long'
            factors.append('momentum')
            reasons.append(f'{fa}: مومنتوم صعودی ({mom*100:+.2f}%) (+2)')
    elif mom < -0.005:
        if direction == 'long':
            score -= 3
            reasons.append(f'{fa}: 🚫 مومنتوم نزولی خلاف خرید ({mom*100:+.2f}%) (-3)')
        else:
            score += weighted(2, 'momentum')
            if not direction:
                direction = 'short'
            factors.append('momentum')
            reasons.append(f'{fa}: مومنتوم نزولی ({mom*100:+.2f}%) (+2)')
    if (sma7 - sma20) / max(sma20, 1e-9) > 0.0015:
        if direction == 'short':
            score -= 1
            reasons.append(f'{fa}: ⚠️ روند کوتاه صعودی خلاف فروش (-1)')
        else:
            score += weighted(1, 'trend')
            factors.append('trend')
            reasons.append(f'{fa}: روند کوتاه صعودی (+1)')
    elif (sma20 - sma7) / max(sma20, 1e-9) > 0.0015:
        if direction == 'long':
            score -= 1
            reasons.append(f'{fa}: ⚠️ روند کوتاه نزولی خلاف خرید (-1)')
        else:
            score += weighted(1, 'trend')
            factors.append('trend')
            reasons.append(f'{fa}: روند کوتاه نزولی (+1)')
    return {'coin': coin, 'rsi': rsi, 'momentum': mom, 'sma7': sma7, 'sma20': sma20,
            'score': score, 'direction': direction, 'reasons': reasons,
            'rsi_anchor': rsi_anchor, 'factors': factors}

def trend_of(closes):
    if not closes or len(closes) < 10:
        return 0
    short = np.mean(closes[-5:])
    lng = np.mean(closes[-15:]) if len(closes) >= 15 else np.mean(closes)
    diff = (short - lng) / lng
    if diff > 0.001:
        return 1
    if diff < -0.001:
        return -1
    return 0

def get_mtf_trend(symbol):
    h4 = get_candles_cached(symbol, '240', 30, drop_forming=True)
    d1 = get_candles_cached(symbol, 'D', 20, drop_forming=True)
    return (trend_of(h4) if h4 else None), (trend_of(d1) if d1 else None)

def confirm_signal(sig):
    coin, direction = sig['coin'], sig['direction']
    fa = COIN_FA.get(coin, coin)
    if not direction:
        return sig
    want = 1 if direction == 'long' else -1
    t4, td = get_mtf_trend(coin)
    sig['mtf'] = {'h4': t4, 'd1': td}
    if t4 is not None and td is not None:
        if t4 == want and td == want:
            sig['score'] += weighted(2, 'mtf')
            sig.setdefault('factors', []).append('mtf')
            sig['reasons'].append(f'{fa}: ۴ساعته و روزانه هر دو هم‌جهت (+2)')
        elif t4 == want or td == want:
            sig['score'] += weighted(1, 'mtf')
            sig.setdefault('factors', []).append('mtf')
            sig['reasons'].append(f'{fa}: یکی از تایم‌فریم‌های بالاتر هم‌جهت (+1)')
        elif t4 == -want and td == -want:
            sig['score'] -= 1
            sig['reasons'].append(f'{fa}: ⚠️ ۴ساعته و روزانه خلاف جهت (-1)')
    ob, spread, tot_val = get_orderbook_info(coin)
    sig['orderbook'], sig['spread'] = ob, spread
    if tot_val is not None and tot_val < 2000:
        ob = None
        sig['reasons'].append(f'{fa}: 🚫 عمق اردربوک ضعیف (ارزش < ۲۰۰۰$) - نادیده گرفتن فشار اردربوک')
    if spread is not None and spread > SPREAD_MAX:
        sig['score'] -= 3
        sig['reasons'].append(f'{fa}: 🚫 اسپرد باز ({spread*100:.2f}%) (-3)')
    if ob is not None:
        if (direction == 'long' and ob > 0.15) or (direction == 'short' and ob < -0.15):
            sig['score'] += weighted(1, 'orderbook')
            sig.setdefault('factors', []).append('orderbook')
            sig['reasons'].append(f'{fa}: دیوار {"خرید" if direction=="long" else "فروش"} قوی‌تر ({ob*100:+.0f}%) (+1)')
        elif (direction == 'long' and ob < -0.3) or (direction == 'short' and ob > 0.3):
            sig['score'] -= 1
            sig['reasons'].append(f'{fa}: ⚠️ فشار اردربوک خلاف سیگنال ({ob*100:+.0f}%) (-1)')
    vc = get_candles(coin, '60', 22, with_volume=True, drop_forming=True)
    if vc and vc[1]:
        vols = vc[1]
        if len(vols) >= 10:
            recent = np.mean(vols[-3:])
            baseline = np.mean(vols[:-3]) or 1
            vr = recent / baseline
            sig['vol_ratio'] = round(vr, 2)
            if vr > 1.5:
                sig['score'] += weighted(1, 'volume')
                sig.setdefault('factors', []).append('volume')
                sig['reasons'].append(f'{fa}: حجم {vr:.1f}x میانگین - حرکت واقعی (+1)')
            elif vr < 0.5:
                sig['score'] -= 1
                sig['reasons'].append(f'{fa}: ⚠️ حجم خیلی کم ({vr:.1f}x) (-1)')
            if vr > 3.0:
                sig['score'] += weighted(2, 'whale_flow')
                sig.setdefault('factors', []).append('whale_flow')
                sig['reasons'].append(f'{fa}: 🐋 انباشت/توزیع خاموش نهنگ (VSA) (+2)')
    bonus, oc_notes = funding_score_bonus(direction)
    if bonus:
        sig['score'] += weighted(bonus, 'funding')
        sig.setdefault('factors', []).append('funding')
        for n in oc_notes:
            sig['reasons'].append(n + ' (+1)')
    sig = apply_regime(sig)
    return apply_session(sig)

def apply_regime(sig):
    rg = state.get('regime') or {}
    regime = rg.get('regime')
    if not regime:
        return sig
    direction = sig['direction']
    rfa = REGIME_FA.get(regime, regime)
    if regime == 'trend_up':
        if direction == 'long':
            sig['score'] += weighted(1, 'regime')
            sig.setdefault('factors', []).append('regime')
            sig['reasons'].append(f'رژیم {rfa}: لانگ هم‌جهت (+1)')
        else:
            sig['score'] -= 1
            sig['reasons'].append(f'رژیم {rfa}: ⚠️ شورت خلاف روند (-1)')
    elif regime == 'trend_down':
        if direction == 'short':
            sig['score'] += weighted(1, 'regime')
            sig.setdefault('factors', []).append('regime')
            sig['reasons'].append(f'رژیم {rfa}: شورت هم‌جهت (+1)')
        else:
            sig['score'] -= 1
            sig['reasons'].append(f'رژیم {rfa}: ⚠️ خرید خلاف روند (-1)')
    elif regime == 'range':
        if sig.get('rsi') is not None and (sig['rsi'] < 38 or sig['rsi'] > 62):
            sig['score'] += weighted(1, 'regime')
            sig.setdefault('factors', []).append('regime')
            sig['reasons'].append(f'رژیم {rfa}: سیگنال بازگشتی در رنج (+1)')
    elif regime == 'storm':
        sig['score'] -= 1
        sig['storm'] = True
        sig['reasons'].append(f'رژیم {rfa}: احتیاط (-1)')
    return sig

# ---------- market sessions (global exchange / bank opens) ----------

SESSION_FA = {
    'asia': 'آسیا 🌙 (حجم کم)',
    'europe': 'اروپا 🇪🇺 (حجم متوسط)',
    'overlap': 'اروپا+آمریکا 🔥 (اوج حجم)',
    'us': 'آمریکا 🇺🇸 (پرنوسان)',
    'quiet': 'شب آرام 😴'
}

def current_session(now=None):
    t = now or fa_now()
    h = t.hour + t.minute / 60.0
    if 3.5 <= h < 11.5:
        return 'asia'
    elif 11.5 <= h < 17.5:
        return 'europe'
    elif 17.5 <= h < 21.5:
        return 'overlap'
    elif 21.5 <= h or h < 1.5:
        return 'us'
    else:
        return 'quiet'

def session_info():
    now = fa_now()
    s = current_session(now)
    return {'session': s, 'label': SESSION_FA.get(s, s), 'hour': now.strftime('%H:%M')}

def apply_session(sig):
    sess = current_session()
    sig['session'] = sess
    if sess == 'quiet':
        sig['score'] += weighted(-1, 'session')
        sig.setdefault('factors', []).append('session')
        sig['reasons'].append(f'جلسه {SESSION_FA[sess]}: حجم جهانی خوابه - احتیاط (-1)')
    elif sess == 'asia':
        if sig.get('score', 0) < 5:
            sig['score'] += weighted(-0.5, 'session')
            sig.setdefault('factors', []).append('session')
            sig['reasons'].append(f'جلسه {SESSION_FA[sess]}: حجم کم آسیا (-0.5)')
    elif sess == 'overlap':
        if sig.get('score', 0) >= 5:
            sig['score'] += weighted(1, 'session')
            sig.setdefault('factors', []).append('session')
            sig['reasons'].append(f'جلسه {SESSION_FA[sess]}: اوج نقدینگی جهانی (+1)')
        else:
            sig['score'] += weighted(0.5, 'session')
            sig.setdefault('factors', []).append('session')
            sig['reasons'].append(f'جلسه {SESSION_FA[sess]}: همپوشانی اروپا+آمریکا (+0.5)')
    elif sess == 'europe':
        sig['score'] += weighted(0.5, 'session')
        sig.setdefault('factors', []).append('session')
        sig['reasons'].append(f'جلسه {SESSION_FA[sess]}: حجم اروپا (+0.5)')
    elif sess == 'us':
        sig['score'] += weighted(0.5, 'session')
        sig.setdefault('factors', []).append('session')
        sig['reasons'].append(f'جلسه {SESSION_FA[sess]}: جلسه آمریکا (+0.5)')
    return sig

def active_coins():
    return [c for c in SCAN_COINS if valid_coin(c)]

def coin_allowed(coin):
    if not valid_coin(coin):
        return False
    banned = state.get('coin_banned', {})
    if banned.get(coin, 0) > time.time():
        return False
    return True

def analyze():
    if len(state.get('price_history', [])) < 3:
        return None
    candidates = []
    for coin in active_coins():
        s = analyze_coin(coin)
        if s:
            candidates.append(s)
    if not candidates:
        return None
    state['scan_table'] = [{'coin': c['coin'], 'score': round(c['score'], 1),
                            'direction': c['direction'], 'rsi': round(c['rsi'])}
                           for c in sorted(candidates, key=lambda x: -x['score'])]
    held = {p.get('coin') for lg in ('paper', 'live')
            for p in get_ledger(lg)['positions']}
    directional = [c for c in candidates if c['direction'] and c.get('rsi_anchor')
                   and coin_allowed(c['coin']) and c['coin'] not in held]
    for c in directional:
        if c['score'] >= 2:
            record_shadow(c)
    if not directional:
        return None
    th = live_threshold()
    qualified = []
    for cand in sorted(directional, key=lambda x: -x['score']):
        if cand['score'] < 3:
            continue
        c2 = confirm_signal(cand)
        cand_th = th + (SHORT_TH_EXTRA if c2.get('direction') == 'short' else 0)
        rg_now = (state.get('regime') or {}).get('regime')
        if rg_now == 'trend_down' and c2.get('direction') == 'long':
            cand_th += 1
        elif rg_now == 'storm':
            cand_th = max(5, cand_th)
        if c2['score'] >= cand_th and c2['direction']:
            if get_genome().get('mtf_req') and 'mtf' not in c2.get('factors', []):
                continue
            qualified.append({'coin': c2['coin'], 'direction': c2['direction'],
                              'confidence': min(0.85, 0.5 + c2['score'] * 0.05),
                              'score': c2['score'], 'reasons': c2['reasons'],
                              'factors': c2.get('factors', [])})
            state['last_signal'] = {'coin': c2['coin'], 'rsi': c2['rsi'],
                                    'momentum': c2['momentum'], 'score': c2['score'],
                                    'direction': c2['direction'], 'reasons': c2['reasons']}
    return qualified or None

# ============ Shadow learning (light) ============

def record_shadow(cand):
    shadows = state.setdefault('shadow_signals', [])
    if len(shadows) >= SHADOW_MAX_PENDING:
        return
    px = state['prices'].get(cand['coin'])
    if not px:
        return
    shadows.append({'ts': time.time(), 'coin': cand['coin'], 'direction': cand['direction'],
                    'score': cand['score'], 'entry': px, 'outcome': None})

def shadow_threshold_adjust():
    return state.get('threshold_extra', 0)

def evaluate_shadows():
    shadows = state.get('shadow_signals', [])
    if not shadows:
        return
    prices = state['prices']
    hits, misses = [], []
    now = time.time()
    for s in shadows:
        if s.get('outcome') is not None:
            continue
        if now - s['ts'] < SHADOW_TIMEOUT_H * 3600:
            continue
        px = prices.get(s['coin'])
        if not px:
            continue
        entry = s['entry']
        if s['direction'] == 'long':
            ok = (px - entry) / entry >= 0.006
        else:
            ok = (entry - px) / entry >= 0.006
        s['outcome'] = 'hit' if ok else 'miss'
        (hits if ok else misses).append(s['score'])
    if hits and misses:
        h_avg, m_avg = float(np.mean(hits)), float(np.mean(misses))
        delta = h_avg - m_avg
        extra = state.get('threshold_extra', 0)
        if delta >= 1.0:
            state['threshold_extra'] = max(-1.0, min(1.0, extra - 0.5))
            add_log(f'Shadow learning: high-score shadows WIN (Δ{delta:.1f}) - threshold eased to {state["threshold_extra"]:+.1f}')
        elif delta <= -1.0:
            state['threshold_extra'] = max(-1.0, min(1.0, extra + 0.5))
            add_log(f'Shadow learning: high-score shadows LOSE (Δ{delta:.1f}) - threshold raised to {state["threshold_extra"]:+.1f}')
    state['shadow_signals'] = [s for s in shadows if s.get('outcome') is None]

# ============ UNIFIED TRADE ENGINE ============

def ledger_label(name):
    return 'مجازی 🔵' if name == 'paper' else 'واقعی (هایپرلیکوئید) 🔴'

def check_daily_limit_lg(lg, base_capital):
    today = fa_now().strftime('%Y-%m-%d')
    if lg.get('daily_date') != today:
        lg['daily_date'] = today
        lg['daily_pnl'] = 0.0
        if lg.get('trading_paused'):
            lg['trading_paused'] = False
            add_log(f"New day - {lg['name']} trading resumed")
    limit = base_capital * DAILY_LOSS_LIMIT
    if lg['daily_pnl'] <= -limit and not lg.get('trading_paused'):
        lg['trading_paused'] = True
        add_log(f"DAILY LOSS LIMIT ({lg['name']}): {lg['daily_pnl']:.2f} - paused for today")
        send_telegram(f'🛑 سقف ضرر روزانه {ledger_label(lg["name"])} فعال شد ({lg["daily_pnl"]:.2f}$)')
    return not lg.get('trading_paused')

def checkpoint_guard(lg, base_capital):
    trades = lg['trades']
    n = len(trades)
    if lg.get('cp_triggered'):
        if n >= 10:
            rec = trades[-10:]
            w_rec = sum(1 for t in rec if t.get('pnl', 0) > 0)
            gp_rec = sum(t['pnl'] for t in rec if t.get('pnl', 0) > 0)
            gl_rec = abs(sum(t['pnl'] for t in rec if t.get('pnl', 0) < 0))
            pf_rec = (gp_rec / gl_rec) if gl_rec > 0 else 99
            if (w_rec / len(rec)) >= 0.40 and pf_rec >= 1.30:
                lg['cp_triggered'] = False
                add_log(f"Checkpoint recovered ({lg['name']}) - trading resumed")
                send_telegram(f'✅ بهبود عملکرد {ledger_label(lg["name"])} (PF={pf_rec:.2f}) - معامله جدید فعال شد')
        return
    if n < 10:
        return
    window = trades[-60:]
    wn = len(window)
    wins = sum(1 for t in window if t.get('pnl', 0) > 0)
    wr = wins / wn
    cap = lg['capital'] if lg['capital'] is not None else base_capital
    gp = sum(t['pnl'] for t in window if t.get('pnl', 0) > 0)
    gl = abs(sum(t['pnl'] for t in window if t.get('pnl', 0) < 0))
    pf = (gp / gl) if gl > 0 else 99
    dd = 0.0
    eq_curve = [e['eq'] for e in lg.get('equity', []) if e.get('eq')]
    if eq_curve:
        peak = 0.0
        for v in eq_curve:
            if v > peak:
                peak = v
            elif peak > 0:
                cur_dd = (peak - v) / peak * 100
                if cur_dd > dd:
                    dd = cur_dd
    reason = None
    if n >= 15 and (wr < 0.20 or cap < base_capital * 0.88):
        reason = f'چک‌پوینت ۱ {ledger_label(lg["name"])} (معامله {n}): وین‌ریت {wr*100:.0f}٪ / سرمایه {cap:.1f}$'
    elif n >= 25 and pf < 0.6:
        reason = f'چک‌پوینت ۲ {ledger_label(lg["name"])} (معامله {n}): PF={pf:.2f}'
    elif n >= 10 and dd >= 12.0:
        reason = f'چک‌پوینت ۳ {ledger_label(lg["name"])} (معامله {n}): حداکثر افت {dd:.1f}٪'
    if reason:
        lg['cp_triggered'] = True
        add_log(f'*** {reason} - new entries paused on this ledger ***')
        send_telegram(f'🛑 {reason}\nمعاملات جدید این موتور متوقف شد. داشبورد رو چک کن.')
        save_state()

def engine_exit_leg(lg, pos, fraction, price):
    fraction = min(1.0, max(0.0, fraction))
    if pos.get('live'):
        size = pos.get('size', 0.0) or 0.0
        close_sz = size * fraction
        if close_sz <= 0:
            return 0.0
        entry = pos['entry_price']
        pnl = (price - entry) * close_sz if pos['direction'] == 'long' else (entry - price) * close_sz
        fee = abs(close_sz * price) * HL_FEE
        net = pnl - fee
        with state_lock:
            lg['capital'] = (lg['capital'] or 0) + net
            pos['size'] = size - close_sz
            pos['margin'] = pos.get('margin', 0) * (1 - fraction)
        lg['daily_pnl'] = lg.get('daily_pnl', 0.0) + net
        if fraction < 0.999:
            pos['banked'] = pos.get('banked', 0.0) + net
        return net
    leg_margin = pos['margin'] * fraction
    if leg_margin <= 0:
        return 0.0
    entry = pos['entry_price']
    pnl_pct = ((price - entry) / entry) if pos['direction'] == 'long' else ((entry - price) / entry)
    pnl = leg_margin * pnl_pct * pos['leverage']
    fee = leg_margin * pos['leverage'] * FEE_RATE
    net = pnl - fee
    with state_lock:
        lg['capital'] = (lg['capital'] or 0) + net
    lg['daily_pnl'] = lg.get('daily_pnl', 0.0) + net
    pos['margin'] -= leg_margin
    if fraction < 0.999:
        pos['banked'] = pos.get('banked', 0.0) + net
    return net

def engine_open(lg, *, coin, direction, price, margin, leverage, sl_pct, tp_pct,
                ts, live_id=None, size=None, reasons=None, factors=None, snapshot=None):
    if direction == 'long':
        sl = price * (1 - sl_pct)
        tp = price * (1 + tp_pct)
    else:
        sl = price * (1 + sl_pct)
        tp = price * (1 - tp_pct)
    pos = {
        'ledger': lg['name'], 'coin': coin, 'direction': direction,
        'entry_price': price, 'margin': margin, 'leverage': leverage,
        'size': size, 'stop_loss': sl, 'take_profit': tp,
        'trail_trigger': tp_pct * 0.75, 'be_trigger': tp_pct * 0.40,
        'best_price': price, 'trail_active': False,
        'live': lg['name'] == 'live', 'live_id': live_id,
        'banked': 0.0, 'close_fails': 0, 'close_next': 0.0,
        'closing': False, 'liq_warned': False, 'sl_oid': None, 'sl_px': None,
        'reasons': reasons or [], 'factors': factors or [],
        'snapshot': snapshot or {}, 'open_ts': ts,
    }
    with state_lock:
        lg['positions'].append(pos)
    return pos

def finalize_close(lg, pos, price, reason, ts, leg_exec, quiet=False):
    with state_lock:
        if pos.get('closing') or pos.get('closed'):
            return False
        pos['closing'] = True
    try:
        margin_before = pos['margin']
        if pos.get('live'):
            if (pos.get('size') or 0) <= 0:
                pos['closed'] = True
                return False
        elif margin_before is None or margin_before <= 0:
            pos['closed'] = True
            return False
        net = leg_exec(pos, 1.0, price)
        if net is None:
            return False
        total_pnl = net + pos.get('banked', 0.0)
        trade_rec = {
            'coin': pos.get('coin', '-'), 'direction': pos['direction'],
            'entry': pos['entry_price'], 'exit': price,
            'pnl': total_pnl, 'pnl_final_leg': net, 'banked': pos.get('banked', 0.0),
            'pnl_pct': (((price - pos['entry_price']) / pos['entry_price']) if pos['direction'] == 'long'
                        else ((pos['entry_price'] - price) / pos['entry_price'])) * 100,
            'reason': reason, 'live': lg['name'] == 'live', 'ledger': lg['name'],
            'factors': pos.get('factors', []), 'snapshot': pos.get('snapshot'),
            'time': fa_now().isoformat() if not quiet else None, 'ts': ts,
        }
        with state_lock:
            lg['trades'].append(trade_rec)
            if len(lg['trades']) > 500:
                lg['trades'] = lg['trades'][-300:]
            try:
                lg['positions'].remove(pos)
            except ValueError:
                pass
        lg['equity'].append({'t': fa_now().isoformat(), 'eq': round(lg['capital'], 4)})
        if len(lg['equity']) > 500:
            lg['equity'] = lg['equity'][-300:]
        if net <= 0 and reason == 'stop_loss':
            lg['consec_losses'] = lg.get('consec_losses', 0) + 1
            if lg['consec_losses'] >= MAX_CONSEC_LOSSES:
                lg['cooldown_until'] = time.time() + CONSEC_PAUSE
                lg['consec_losses'] = 0
                add_log(f'CIRCUIT BREAKER ({lg["name"]}): pausing {CONSEC_PAUSE//3600}h')
                send_telegram(f'⛔️ {MAX_CONSEC_LOSSES} ضرر پیاپی {ledger_label(lg["name"])} - {CONSEC_PAUSE//3600} ساعت استراحت')
            elif lg['consec_losses'] >= 2:
                lg['cooldown_until'] = time.time() + LOSS_COOLDOWN
                add_log(f'Cooldown {LOSS_COOLDOWN//60}min ({lg["name"]})')
        elif net > 0:
            lg['consec_losses'] = 0
        if not quiet:
            reason_fa = {'take_profit': 'حد سود 🎯', 'stop_loss': 'حد ضرر 🛑',
                         'profit_lock': 'قفل سود 🔒', 'max_age': 'طولانی شدن ⏰',
                         'runner_end': 'پایان دونده 🏃', 'kill_switch': 'اضطراری 🔴',
                         'sync_closed': 'بسته شد روی صرافی ⚠️',
                         'liquidated_or_manual': 'لیکویید/بسته دستی ⚠️'}.get(reason, reason)
            emoji = '✅' if net > 0 else '❌'
            add_log(f'Closed ({lg["name"]}): {COIN_FA.get(pos.get("coin"), "-")} {pos["direction"]} {net:+.4f}$ [{reason}]')
            send_telegram(
                f'{emoji} معامله بسته شد ({ledger_label(lg["name"])})\n'
                f'ارز: {COIN_FA.get(pos.get("coin"), pos.get("coin"))}\n'
                f'سود/زیان: {net:+.4f}$ (کل: {total_pnl:+.4f}$)\nعلت: {reason_fa}\n'
                f'موجودی: {lg["capital"]:.2f}$')
        if not quiet:
            try:
                checkpoint_guard(lg, PAPER_CAPITAL if lg['name'] == 'paper' else live_base_capital())
            except Exception:
                log_exception('checkpoint_guard failed')
            save_state()
        pos['closed'] = True
        return True
    finally:
        pos['closing'] = False

def live_base_capital():
    base = state.get('live_base')
    if base:
        return base
    lg = get_ledger('live')
    if lg['capital']:
        state['live_base'] = lg['capital']
        return lg['capital']
    return 100.0

def manage_engine_pos(lg, pos, price, ts, leg_exec, quiet=False):
    if pos.get('closing') or pos.get('closed') or not price:
        return
    entry = pos['entry_price']
    pos.setdefault('best_price', entry)
    pos.setdefault('trail_active', False)
    age_h = (ts - pos.get('open_ts', ts)) / 3600
    cur_gain_now = ((price - entry) / entry) if pos['direction'] == 'long' else ((entry - price) / entry)
    cur_regime_now = (state.get('regime') or {}).get('regime', 'range')
    regime_opp_now = ((pos['direction'] == 'long' and cur_regime_now == 'trend_down') or
                      (pos['direction'] == 'short' and cur_regime_now == 'trend_up'))
    if age_h > MAX_TRADE_HOURS * 4:
        if not quiet:
            add_log(f'Hard close: {pos.get("coin")} older than {MAX_TRADE_HOURS*4}h')
        finalize_close(lg, pos, price, 'max_age', ts, leg_exec, quiet)
        return
    if age_h > MAX_TRADE_HOURS:
        if (age_h > MAX_TRADE_HOURS * 3 and cur_gain_now < 0.0) or cur_gain_now < -0.015 or (age_h > MAX_TRADE_HOURS * 2 and regime_opp_now and cur_gain_now < 0.003):
            finalize_close(lg, pos, price, 'max_age', ts, leg_exec, quiet)
            return
        elif cur_gain_now >= 0.003 and not pos.get('overtime'):
            pos['overtime'] = True
            pos['trail_active'] = True
            if not quiet:
                add_log(f'{pos.get("coin")} overtime but in profit ({cur_gain_now*100:+.1f}%) - tight trail')
    tp_dist = abs(pos['take_profit'] - entry) / entry
    partial_trig = tp_dist * 0.5
    trail_trig = pos.get('trail_trigger', tp_dist * 0.75)
    be_trig = pos.get('be_trigger', tp_dist * 0.40)
    cur_regime = (state.get('regime') or {}).get('regime', 'range')
    regime_opp = ((pos['direction'] == 'long' and cur_regime == 'trend_down') or
                  (pos['direction'] == 'short' and cur_regime == 'trend_up'))
    if regime_opp and cur_gain_now >= 0.008 and not pos['trail_active']:
        pos['trail_active'] = True
        be_sl = entry * 1.002 if pos['direction'] == 'long' else entry * 0.998
        if (pos['direction'] == 'long' and be_sl > pos['stop_loss']) or (pos['direction'] == 'short' and be_sl < pos['stop_loss']):
            pos['stop_loss'] = be_sl
        if not quiet:
            add_log(f'Regime reversal protection ({lg["name"]}): {pos.get("coin")} trailing ON & SL to BE')
    if not pos.get('partial_done'):
        cur_gain = cur_gain_now
        if cur_gain >= partial_trig:
            net = leg_exec(pos, 0.4, price)
            if net is not None:
                pos['partial_done'] = True
                if not quiet:
                    add_log(f'Partial TP ({lg["name"]}): 40% at {cur_gain*100:+.1f}% = {net:+.4f}$')
                    send_telegram(f'💰 برداشت پله‌ای {ledger_label(lg["name"])}: ۴۰٪ ({net:+.4f}$)')
    if age_h >= 12 and cur_gain_now >= 0.005:
        be_sl = entry * 1.002 if pos['direction'] == 'long' else entry * 0.998
        if (pos['direction'] == 'long' and be_sl > pos['stop_loss']) or (pos['direction'] == 'short' and be_sl < pos['stop_loss']):
            pos['stop_loss'] = be_sl
            if not quiet and not pos.get('time_be'):
                pos['time_be'] = True
                add_log(f'Time-Decay BE ({lg["name"]}): {pos.get("coin")} >12h in profit, SL moved to breakeven')
    if pos['direction'] == 'long':
        if price > pos['best_price']:
            pos['best_price'] = price
        gain = (pos['best_price'] - entry) / entry
        if not pos.get('half_cashed') and gain >= be_trig:
            net = leg_exec(pos, 0.5, price)
            if net is not None:
                pos['half_cashed'] = True
                if not quiet:
                    add_log(f'Half-cash ({lg["name"]}): 50% at {gain*100:+.1f}% = {net:+.4f}$')
                    send_telegram(f'💵 نصف معامله {ledger_label(lg["name"])} نقد شد ({net:+.4f}$) - نصف دیگه با استاپ اصلی')
        if gain >= 0.012:
            be_floor = entry * 1.002
            if be_floor > pos['stop_loss']:
                pos['stop_loss'] = be_floor
                if not quiet and not pos.get('be_locked'):
                    pos['be_locked'] = True
                    add_log(f'Breakeven lock ({lg["name"]}): {pos.get("coin")} SL moved to BE (+1.2% gain)')
        if gain >= tp_dist * LADDER1_AT_TP:
            lock_sl = entry * (1 + gain * LADDER1_LOCK)
            if lock_sl > pos['stop_loss']:
                pos['stop_loss'] = lock_sl
                if not pos.get('ladder1'):
                    pos['ladder1'] = True
                    if not quiet:
                        add_log(f'Ladder1 ({lg["name"]}): lock {LADDER1_LOCK*100:.0f}% of gain')
        if gain >= trail_trig or gain >= 0.025:
            lock_sl = entry * (1 + gain * 0.60)
            if lock_sl > pos['stop_loss']:
                pos['stop_loss'] = lock_sl
            if not pos['trail_active']:
                pos['trail_active'] = True
                if not quiet:
                    add_log(f'Profit lock ON ({lg["name"]}) gain {gain*100:.1f}%')
        cur_trail_gap = 0.004 if gain >= 0.025 else 0.012
        if price <= pos['stop_loss']:
            reason = 'runner_end' if pos.get('runner') else (
                'profit_lock' if (pos.get('ladder1') or pos['trail_active']) else 'stop_loss')
            finalize_close(lg, pos, price, reason, ts, leg_exec, quiet)
        elif price >= pos['take_profit'] and not pos.get('runner'):
            net = leg_exec(pos, 0.6, price)
            if net is not None:
                pos['runner'] = True
                pos['stop_loss'] = max(pos['stop_loss'], price * (1 - RUNNER_TRAIL))
                if not quiet:
                    add_log(f'RUNNER ({lg["name"]}): banked 60% at TP ({net:+.4f}$), 40% rides')
                    send_telegram(f'🏃 حالت دونده {ledger_label(lg["name"])}: ۶۰٪ نقد ({net:+.4f}$) - ۴۰٪ سوار روند')
        elif pos.get('runner'):
            cur_rt = 0.002 if gain >= 0.04 else RUNNER_TRAIL
            if price <= pos['best_price'] * (1 - cur_rt):
                finalize_close(lg, pos, price, 'runner_end', ts, leg_exec, quiet)
            else:
                floor = pos['best_price'] * (1 - cur_rt)
                if floor > pos['stop_loss']:
                    pos['stop_loss'] = floor
        elif pos['trail_active'] and price <= pos['best_price'] * (1 - cur_trail_gap):
            finalize_close(lg, pos, price, 'profit_lock', ts, leg_exec, quiet)
    else:
        if price < pos['best_price']:
            pos['best_price'] = price
        gain = (entry - pos['best_price']) / entry
        if not pos.get('half_cashed') and gain >= be_trig:
            net = leg_exec(pos, 0.5, price)
            if net is not None:
                pos['half_cashed'] = True
                if not quiet:
                    add_log(f'Half-cash ({lg["name"]}): 50% at {gain*100:+.1f}% = {net:+.4f}$')
        if gain >= 0.012:
            be_floor = entry * 0.998
            if be_floor < pos['stop_loss']:
                pos['stop_loss'] = be_floor
                if not quiet and not pos.get('be_locked'):
                    pos['be_locked'] = True
                    add_log(f'Breakeven lock ({lg["name"]}): {pos.get("coin")} SL moved to BE (+1.2% gain)')
        if gain >= tp_dist * LADDER1_AT_TP:
            lock_sl = entry * (1 - gain * LADDER1_LOCK)
            if lock_sl < pos['stop_loss']:
                pos['stop_loss'] = lock_sl
                if not pos.get('ladder1'):
                    pos['ladder1'] = True
        if gain >= trail_trig or gain >= 0.025:
            lock_sl = entry * (1 - gain * 0.60)
            if lock_sl < pos['stop_loss']:
                pos['stop_loss'] = lock_sl
            if not pos['trail_active']:
                pos['trail_active'] = True
        cur_trail_gap = 0.004 if gain >= 0.025 else 0.012
        if price >= pos['stop_loss']:
            reason = 'runner_end' if pos.get('runner') else (
                'profit_lock' if (pos.get('ladder1') or pos['trail_active']) else 'stop_loss')
            finalize_close(lg, pos, price, reason, ts, leg_exec, quiet)
        elif price <= pos['take_profit'] and not pos.get('runner'):
            net = leg_exec(pos, 0.6, price)
            if net is not None:
                pos['runner'] = True
                pos['stop_loss'] = min(pos['stop_loss'], price * (1 + RUNNER_TRAIL))
                if not quiet:
                    add_log(f'RUNNER ({lg["name"]}): banked 60% at TP ({net:+.4f}$)')
        elif pos.get('runner'):
            cur_rt = 0.002 if gain >= 0.04 else RUNNER_TRAIL
            if price >= pos['best_price'] * (1 + cur_rt):
                finalize_close(lg, pos, price, 'runner_end', ts, leg_exec, quiet)
            else:
                floor = pos['best_price'] * (1 + cur_rt)
                if floor < pos['stop_loss']:
                    pos['stop_loss'] = floor
        elif pos['trail_active'] and price >= pos['best_price'] * (1 + cur_trail_gap):
            finalize_close(lg, pos, price, 'profit_lock', ts, leg_exec, quiet)

# ============ Hyperliquid LIVE layer (agent wallet) ============

_hl_ex = [None]

def hl_exchange():
    """Signed Hyperliquid client (agent wallet signing for the master account)."""
    priv = os.environ.get('HL_AGENT_PRIVATE_KEY', '').strip()
    account = os.environ.get('HL_ACCOUNT_ADDRESS', '').strip()
    if not priv or not account:
        state['hl_agent_ok'] = False
        return None
    try:
        if _hl_ex[0] is None:
            from hyperliquid.exchange import Exchange
            import eth_account
            wallet = eth_account.Account.from_key(priv)
            _hl_ex[0] = Exchange(wallet, hl_base_url(), account_address=account)
        return _hl_ex[0]
    except Exception:
        log_exception('hl_exchange init failed')
        state['hl_agent_ok'] = False
        return None

def hl_account():
    return os.environ.get('HL_ACCOUNT_ADDRESS', '').strip()

def hl_test_connection():
    """Verify keys: read account state via the agent. Returns (ok, info)."""
    try:
        ex = hl_exchange()
        if ex is None:
            return False, 'کلید Agent یا آدرس Master تنظیم نشده'
        st = ex.info.user_state(hl_account())
        ms = st.get('marginSummary', {})
        val = float(ms.get('accountValue', 0) or 0)
        return True, f'اتصال موفق - موجودی حساب: {val:.2f}$'
    except Exception as e:
        return False, f'خطا در اتصال: {e}'

def sync_live_capital(quiet=False):
    ex = hl_exchange()
    if ex is None:
        return None
    try:
        st = ex.info.user_state(hl_account())
        ms = st.get('marginSummary', {})
        val = float(ms.get('accountValue', 0) or 0)
        lg = get_ledger('live')
        if val > 0:
            with state_lock:
                lg['capital'] = val
            if not state.get('live_base') or state.get('live_base') <= 0:
                state['live_base'] = val
        return val
    except Exception:
        log_exception('sync_live_capital')
        return None

def hl_place_sl(pos, size=None):
    """Place/replace the protective stop-loss trigger order on the exchange."""
    if not state.get('backstop_enabled'):
        return
    ex = hl_exchange()
    if ex is None:
        return
    try:
        coin = pos['coin']
        hl_cancel_sl(pos)
        sz = size if size is not None else pos.get('size', 0.0)
        if sz <= 0:
            return
        is_buy = pos['direction'] == 'short'
        trigger_px = round(float(pos['stop_loss']), PX_DECIMALS(coin))
        px_dec = PX_DECIMALS(coin)
        if trigger_px <= 0:
            return
        if is_buy:
            limit_px = round(trigger_px * 1.01, px_dec)
        else:
            limit_px = round(trigger_px * 0.99, px_dec)
        resp = ex.order(coin, is_buy, sz, limit_px,
                        {"trigger": {"triggerPx": str(trigger_px), "isMarket": True, "tpsl": "sl"}},
                        reduce_only=True)
        if resp.get('status') == 'ok':
            sts = resp['response']['data']['statuses'][0]
            if 'resting' in sts:
                pos['sl_oid'] = sts['resting']['oid']
                pos['sl_px'] = float(trigger_px)
                add_log(f'Backstop SL {coin}: {trigger_px} (oid {pos["sl_oid"]})')
        else:
            add_log(f'Backstop SL failed {coin}: {resp}')
    except Exception:
        log_exception('hl_place_sl')

def hl_cancel_sl(pos):
    ex = hl_exchange()
    oid = pos.get('sl_oid')
    if ex is None or not oid:
        pos['sl_oid'] = None
        return
    try:
        ex.cancel(pos['coin'], oid)
    except Exception:
        pass
    pos['sl_oid'] = None

def hl_open_live(coin, direction, margin, price, lev):
    ex = hl_exchange()
    if ex is None:
        return None
    try:
        if state.get('lev_set', {}).get(coin) != lev:
            r = ex.update_leverage(int(lev), coin, True)
            if r.get('status') == 'ok':
                state.setdefault('lev_set', {})[coin] = lev
        ok_depth, book_val = hl_book_depth_ok(coin, margin * lev, direction)
        if not ok_depth:
            add_log(f'LIVE guard: {coin} book too thin for {margin*lev:.0f}$ (near={book_val:.0f}$)')
            return None
        sz_dec = SZ_DECIMALS(coin)
        px_dec = PX_DECIMALS(coin)
        sz = math.floor((margin * lev) / price * 10 ** sz_dec) / 10 ** sz_dec
        if sz <= 0:
            return None
        if margin * lev < HL_MIN_ORDER_USD:
            add_log(f'LIVE guard: {coin} notional {margin*lev:.1f}$ < min {HL_MIN_ORDER_USD}$')
            return None
        px = round(price, px_dec)
        resp = ex.market_open(coin, direction == 'long', sz, px=px, slippage=0.01)
        if resp.get('status') != 'ok':
            add_log(f'LIVE open failed {coin}: {resp}')
            send_telegram(f'⚠️ باز کردن {COIN_FA.get(coin)} در هایپرلیکوئید ناموفق بود: {resp.get("response")}')
            return None
        sts = resp['response']['data']['statuses'][0]
        if 'filled' in sts:
            avg = float(sts['filled']['avgPx'])
            oid = sts['filled']['oid']
        elif 'resting' in sts:
            avg = px
            oid = sts['resting']['oid']
        else:
            add_log(f'LIVE open {coin}: unexpected statuses {sts}')
            return None
        add_log(f'LIVE opened {direction} {coin} sz={sz} @ {avg} (oid {oid})')
        return {'oid': oid, 'entry': avg, 'size': sz}
    except Exception:
        log_exception('hl_open_live')
        return None

def hl_close_live(pos, fraction, price):
    """Close a live position on Hyperliquid; cancels the backstop. Returns True/False."""
    ex = hl_exchange()
    if ex is None:
        return False
    try:
        # short-circuit: if the exchange no longer holds this coin (e.g. the
        # backstop trigger already closed it), just settle locally
        st = ex.info.user_state(hl_account())
        exch_szi = 0.0
        for p in st.get('assetPositions', []):
            if p['position'].get('coin') == pos['coin']:
                exch_szi = float(p['position'].get('szi') or 0)
                break
        if abs(exch_szi) < 1e-12:
            hl_cancel_sl(pos)
            return True
        sz = pos.get('size', 0.0) or 0.0
        close_sz = sz * fraction
        # never try to close more than the exchange holds
        close_sz = min(close_sz, abs(exch_szi))
        sz_dec = SZ_DECIMALS(pos['coin'])
        close_sz = math.floor(close_sz * 10 ** sz_dec) / 10 ** sz_dec
        if close_sz <= 0:
            hl_cancel_sl(pos)
            return True
        px = round(price, PX_DECIMALS(pos['coin']))
        resp = ex.market_close(pos['coin'], close_sz, px=px, slippage=0.01)
        if resp.get('status') != 'ok':
            add_log(f'LIVE close failed {pos["coin"]}: {resp}')
            return False
        sts = resp['response']['data']['statuses'][0]
        if 'filled' not in sts and 'resting' not in sts:
            add_log(f'LIVE close {pos["coin"]}: unexpected {sts}')
            return False
        return True
    except Exception:
        log_exception('hl_close_live')
        return False

def live_leg_exec(lg):
    def _exec(pos, fraction, price):
        now = time.time()
        if now < pos.get('close_next', 0):
            return None
        ok = hl_close_live(pos, fraction, price)
        if not ok:
            for retry in range(1, 4):
                time.sleep(3)
                add_log(f'LIVE close retry #{retry}/3 for {pos.get("coin")}...')
                ok = hl_close_live(pos, fraction, price)
                if ok:
                    break
        if not ok:
            pos['close_fails'] = pos.get('close_fails', 0) + 1
            pos['close_next'] = now + min(30 * (2 ** min(pos['close_fails'], 4)), 300)
            if pos['close_fails'] >= 3:
                send_telegram('🚨 ۳ بار بستن موقعیت لایو شکست خورد! لطفاً دستی ببند.')
            return None
        pos['close_fails'] = 0
        pos['close_next'] = 0
        net = engine_exit_leg(lg, pos, fraction, price)
        # re-arm the protective SL for the remainder (partial closes)
        if fraction < 0.999 and pos.get('size', 0) > 0 and not pos.get('closed'):
            try:
                hl_place_sl(pos)
            except Exception:
                pass
        return net
    return _exec

def live_manage_wrapper(pos, price, ts):
    try:
        check_liquidation_distance(pos, price)
    except Exception:
        pass
    lg = get_ledger('live')
    manage_engine_pos(lg, pos, price, ts, live_leg_exec(lg))
    # keep the exchange-side backstop in sync with the software SL
    try:
        if not pos.get('closed') and pos.get('size', 0) > 0:
            sl = pos.get('stop_loss')
            old = pos.get('sl_px')
            move_th = max(0.0005 * pos['entry_price'], 10 ** (-PX_DECIMALS(pos['coin'])))
            if old is None or sl is None or abs(sl - old) > move_th:
                hl_place_sl(pos)
    except Exception:
        log_exception('backstop sync')

def check_liquidation_distance(pos, current):
    try:
        ex = hl_exchange()
        if ex is None:
            return
        st = ex.info.user_state(hl_account())
        for p in st.get('assetPositions', []):
            it = p.get('position', {})
            if it.get('coin') != pos['coin']:
                continue
            liq = float(it.get('liquidationPx') or 0)
            if liq <= 0:
                continue
            dist = abs(current - liq) / current
            if dist < 0.08 and not pos.get('liq_warned'):
                pos['liq_warned'] = True
                add_log(f'LIQ WARN {pos["coin"]}: liq {liq} dist {dist*100:.1f}%')
                send_telegram(f'⚠️ فاصله تا لیکویید کمه! {COIN_FA.get(pos["coin"])} lq={fmt_price(liq)} (فاصله {dist*100:.1f}%)')
            break
    except Exception:
        pass

def reconcile_live_positions():
    ex = hl_exchange()
    if ex is None:
        return
    try:
        st = ex.info.user_state(hl_account())
        pos_map = {p['position']['coin']: p['position'] for p in st.get('assetPositions', [])}
        lg = get_ledger('live')
        for pos in list(lg['positions']):
            hl_pos = pos_map.get(pos['coin'])
            szi = float(hl_pos['szi']) if hl_pos else 0.0
            local_szi = pos.get('size', 0) * (1 if pos['direction'] == 'long' else -1)
            if abs(szi) < 1e-12:
                price = state['prices'].get(pos['coin'], pos['entry_price'])
                add_log(f'RECONCILE: {pos["coin"]} gone from exchange (szi={szi}) - settling')
                finalize_close(lg, pos, price, 'sync_closed', time.time(), paper_leg_exec(lg))
            elif abs(szi - local_szi) / max(abs(local_szi), 1e-9) > 0.01:
                new_size = abs(szi)
                add_log(f'RECONCILE: {pos["coin"]} size adjusted {local_szi} -> {szi}')
                with state_lock:
                    pos['size'] = new_size
                if pos.get('sl_oid'):
                    try:
                        hl_place_sl(pos)
                    except Exception:
                        pass
        # open exchange positions not tracked locally -> adopt them (safety)
        tracked = {p['coin'] for p in lg['positions']}
        for coin, hp in pos_map.items():
            if coin in tracked:
                continue
            szi = float(hp['szi'])
            if abs(szi) < 1e-9:
                continue
            direction = 'long' if szi > 0 else 'short'
            entry = float(hp['entryPx'] or 0)
            if entry <= 0:
                continue
            add_log(f'RECONCILE: adopting untracked {coin} {direction} {abs(szi)} @ {entry}')
            pos = engine_open(lg, coin=coin, direction=direction, price=entry,
                              margin=abs(szi) * entry / eff_leverage(coin),
                              leverage=eff_leverage(coin), sl_pct=STOP_LOSS, tp_pct=TAKE_PROFIT,
                              ts=time.time(), live_id=hp.get('posId'),
                              size=abs(szi), reasons=['reconcile'], factors=[])
            if pos:
                hl_place_sl(pos)
    except Exception:
        log_exception('reconcile_live_positions')

def paper_leg_exec(lg):
    def _exec(pos, fraction, price):
        return engine_exit_leg(lg, pos, fraction, price)
    return _exec

def manage_all_positions():
    ts = time.time()
    lg_p = get_ledger('paper')
    for pos in list(lg_p['positions']):
        price = state['prices'].get(pos.get('coin'))
        if price:
            manage_engine_pos(lg_p, pos, price, ts, paper_leg_exec(lg_p))
    for pos in list(get_ledger('live')['positions']):
        price = state['prices'].get(pos.get('coin'))
        if price:
            live_manage_wrapper(pos, price, ts)

# ============ Position opening ============

def entries_blocked_reason(lg, base_capital):
    if state.get('crash_mode'):
        return 'محافظ سقوط فعال'
    if state.get('manual_paused'):
        return 'توقف دستی'
    if lg.get('cp_triggered'):
        return 'چک‌پوینت اضطراری'
    if lg.get('trading_paused'):
        return 'سقف ضرر روزانه'
    if lg.get('cooldown_until', 0) > time.time():
        return 'استراحت پس از ضرر پیاپی'
    if len(lg['positions']) >= MAX_POSITIONS:
        return 'حداکثر موقعیت باز'
    return None

def open_engine_position(lg_name, signal, quiet=False):
    lg = get_ledger(lg_name)
    coin = signal.get('coin', 'BTC')
    if any(p.get('coin') == coin for p in lg['positions']):
        return False
    dir_now = signal.get('direction', 'long')
    same_dir_count = sum(1 for p in lg['positions'] if p.get('direction') == dir_now)
    if same_dir_count >= 3:
        if not quiet:
            add_log(f'Entry blocked ({lg_name}): directional cap reached (max 3 {dir_now}s)')
        return False
    base_capital = PAPER_CAPITAL if lg_name == 'paper' else live_base_capital()
    if not check_daily_limit_lg(lg, base_capital):
        return False
    if entries_blocked_reason(lg, base_capital):
        return False
    capital = lg['capital']
    if capital is None or capital <= 0:
        return False
    lev = eff_leverage(coin)
    used_margin = sum(p.get('margin', 0) for p in lg['positions'])
    if used_margin >= capital * MAX_TOTAL_RISK:
        return False
    risk = kelly_risk(lg_name)
    margin = capital * risk * signal['confidence']
    try:
        th = live_threshold()
        sc = float(signal.get('score', th))
        if sc < th + 1:
            margin *= 0.7
        elif sc >= th + 2:
            margin *= 1.3
    except Exception:
        pass
    if lg.get('consec_losses', 0) == 2:
        margin *= 0.8
    elif lg.get('consec_losses', 0) >= 3:
        margin *= 0.6
    margin = min(margin, max(0.0, capital * MAX_TOTAL_RISK - used_margin))
    price = state['prices'].get(coin)
    if not price:
        return False
    sl_pct, tp_pct, vol_regime = dynamic_levels(coin)
    if vol_regime == 'high':
        margin *= 0.7
    if (state.get('regime') or {}).get('regime') == 'storm':
        margin *= 0.5
    if margin < capital * 0.02:
        return False
    live_id, size = None, None
    if lg_name == 'live':
        if coin not in LIVE_COIN_WHITELIST:
            add_log(f'LIVE guard: {coin} not in whitelist - skipped')
            return False
        notional = margin * lev
        if notional < HL_MIN_ORDER_USD:
            add_log(f'LIVE guard: {coin} notional {notional:.1f}$ < min {HL_MIN_ORDER_USD}$ - skipped')
            return False
        opened = hl_open_live(coin, signal['direction'], margin, price, lev)
        if opened is None:
            return False
        live_id = opened['oid']
        price = opened['entry']
        size = opened['size']
    pos = engine_open(lg, coin=coin, direction=signal['direction'], price=price,
                      margin=margin, leverage=lev, sl_pct=sl_pct, tp_pct=tp_pct,
                      ts=time.time(), live_id=live_id, size=size,
                      reasons=signal.get('reasons', []), factors=signal.get('factors', []),
                      snapshot={'threshold': live_threshold(), 'tuned': dict(get_tuned()),
                                'regime': (state.get('regime') or {}).get('regime'),
                                'session': current_session(),
                                'kelly_risk': round(risk, 3), 'score': signal.get('score')})
    if lg_name == 'live' and size:
        try:
            hl_place_sl(pos)
        except Exception:
            pass
    fa = COIN_FA.get(coin, coin)
    dir_fa = 'خرید 📈' if signal['direction'] == 'long' else 'فروش 📉'
    state['last_reason'] = (f'{ledger_label(lg_name)} | {fa} | {dir_fa} | امتیاز {signal["score"]:.1f}\n'
                            + '\n'.join('• ' + r for r in signal.get('reasons', []))
                            + f'\nورود: {fmt_price(price)} | SL: {fmt_price(pos["stop_loss"])} | TP: {fmt_price(pos["take_profit"])}')
    add_log(f'Opened ({lg_name}): {signal["direction"]} {coin} @ {fmt_price(price)} m={margin:.2f}$ lev={lev}x')
    send_telegram(
        f'{"🔴 معامله واقعی (هایپرلیکوئید)" if lg_name=="live" else "🔵 معامله مجازی"} باز شد\n'
        f'ارز: {fa}\nجهت: {dir_fa}\nورود: {fmt_price(price)}\n'
        f'حد ضرر: {fmt_price(pos["stop_loss"])} | حد سود: {fmt_price(pos["take_profit"])}\n'
        f'امتیاز: {signal["score"]:.1f}')
    save_state()
    return True

def panic_close_all(source='dashboard'):
    n = 0
    lg_l = get_ledger('live')
    for pos in list(lg_l['positions']):
        cur = state['prices'].get(pos.get('coin'), pos['entry_price'])
        if finalize_close(lg_l, pos, cur, 'kill_switch', time.time(), live_leg_exec(lg_l)):
            n += 1
    lg_p = get_ledger('paper')
    for pos in list(lg_p['positions']):
        cur = state['prices'].get(pos.get('coin'), pos['entry_price'])
        if finalize_close(lg_p, pos, cur, 'kill_switch', time.time(), paper_leg_exec(lg_p)):
            n += 1
    state['manual_paused'] = True
    add_log(f'*** PANIC ({source}): {n} positions closed ***')
    send_telegram(f'🛑 توقف اضطراری ({source}): {n} معامله بسته شد')
    save_state()
    return n

# ============ Backtest ON the unified engine ============

def _bt_entry_signal(closes, vols, i, coin, threshold, sl, tp):
    if i < 20:
        return None
    prices = closes[i - 20:i + 1]
    gains, losses = [], []
    for j in range(1, len(prices)):
        ch = prices[j] - prices[j - 1]
        gains.append(max(0, ch))
        losses.append(max(0, -ch))
    ag, al = np.mean(gains[-14:]), np.mean(losses[-14:])
    rsi = 100 - (100 / (1 + ag / max(al, 1e-9))) if (ag + al) > 1e-12 else 50
    mom = (prices[-1] - prices[-5]) / prices[-5] if len(prices) > 5 else 0
    sma7, sma20 = np.mean(prices[-7:]), np.mean(prices[-20:])
    score, direction = 0, None
    g = get_genome()
    if rsi < g['rsi_lo']:
        score += 3
        direction = 'long'
    elif rsi > g['rsi_hi']:
        score += 3
        direction = 'short'
    if mom > 0.005 and direction != 'short':
        score += 2
        direction = direction or 'long'
    elif mom < -0.005 and direction != 'long':
        score += 2
        direction = direction or 'short'
    if direction == 'long' and (sma7 - sma20) / max(sma20, 1e-9) < -0.0015:
        score -= 1
    if direction == 'short' and (sma20 - sma7) / max(sma20, 1e-9) < -0.0015:
        score -= 1
    if score >= threshold and direction:
        return {'coin': coin, 'direction': direction, 'score': score}
    return None

def bt_on_data(closes, vols, coin, sl, tp, threshold):
    if not closes or len(closes) < 30:
        return None
    entries, pnls = [], []
    entry = exit_px = None
    direction = None
    for i in range(len(closes)):
        px = closes[i]
        if entry is None:
            sig = _bt_entry_signal(closes, vols, i, coin, threshold, sl, tp)
            if sig:
                entry = px
                direction = sig['direction']
                entries.append({'i': i, 'px': px, 'dir': direction})
            continue
        stop = entry * (1 - sl) if direction == 'long' else entry * (1 + sl)
        targ = entry * (1 + tp) if direction == 'long' else entry * (1 - tp)
        if (direction == 'long' and px <= stop) or (direction == 'short' and px >= stop):
            exit_px = stop
            reason = 'sl'
        elif (direction == 'long' and px >= targ) or (direction == 'short' and px <= targ):
            exit_px = targ
            reason = 'tp'
        else:
            continue
        pnl = ((exit_px - entry) / entry) if direction == 'long' else ((entry - exit_px) / entry)
        pnl = pnl * 100  # percent on notional (x1)
        pnls.append(round(pnl, 3))
        entry = exit_px = None
        direction = None
    if not pnls:
        return None
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    return {'trades': len(pnls), 'win_rate': round(len(wins) / len(pnls) * 100, 1),
            'profit_factor': round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else 0),
            'total_pct': round(sum(pnls), 2), 'avg': round(np.mean(pnls), 3)}

def run_backtest(coin='BTC', days=30, sl=None, tp=None, threshold=None):
    closes = get_candles(coin, '60', days * 24, drop_forming=True)
    if not closes:
        return None
    _, vols = get_candles(coin, '60', days * 24, with_volume=True, drop_forming=True)
    vols = vols or None
    sl = sl or STOP_LOSS
    tp = tp or TAKE_PROFIT
    threshold = threshold or get_tuned()['threshold']
    return bt_on_data(closes, vols, coin, sl, tp, threshold)

# ============ Per-coin permission filter ============

def update_coin_filter():
    """Ban coins with a persistently losing recent record (paper evidence)."""
    lg = get_ledger('paper')
    trades = lg['trades']
    now = time.time()
    banned = state.setdefault('coin_banned', {})
    # expire old bans
    for c in list(banned):
        if banned[c] <= now:
            del banned[c]
    from collections import defaultdict
    by_coin = defaultdict(list)
    for t in trades:
        if now - t.get('ts', 0) < 30 * 86400:
            by_coin[t.get('coin')].append(t.get('pnl', 0))
    for c, ps in by_coin.items():
        if len(ps) >= 6 and sum(1 for p in ps if p > 0) / len(ps) < 0.25:
            banned[c] = now + 86400
            add_log(f'Coin filter: {c} benched 24h (recent win rate low)')

# ============ Performance statistics ============

def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - margin) * 100, min(1.0, center + margin) * 100)

def perf_stats(lg_name='paper'):
    lg = get_ledger(lg_name)
    trades = lg['trades']
    if not trades:
        return None
    pnls = [t.get('pnl', 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    eq = PAPER_CAPITAL if lg_name == 'paper' else live_base_capital()
    peak, max_dd = eq, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    sharpe = sortino = 0.0
    if len(pnls) >= 3:
        arr = np.array(pnls, dtype=float)
        sd = float(np.std(arr))
        sharpe = round(float(np.mean(arr)) / sd * (len(arr) ** 0.5), 2) if sd > 1e-12 else 0.0
        downs = arr[arr < 0]
        dsd = float(np.std(downs)) if len(downs) > 1 else (abs(float(downs[0])) if len(downs) == 1 else 0.0)
        sortino = round(float(np.mean(arr)) / dsd * (len(arr) ** 0.5), 2) if dsd > 1e-12 else 99.0
    lo, hi = wilson_ci(len(wins), len(pnls))
    return {'count': len(pnls), 'win_rate': round(len(wins) / len(pnls) * 100, 1),
            'wr_ci': (round(lo, 1), round(hi, 1)),
            'avg_win': round(float(np.mean(wins)), 4) if wins else 0,
            'avg_loss': round(float(np.mean(losses)), 4) if losses else 0,
            'profit_factor': round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else 0),
            'max_drawdown': round(max_dd * 100, 1),
            'total_pnl': round(sum(pnls), 4), 'sharpe': sharpe, 'sortino': sortino}

def trades_csv(lg_name='paper'):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['coin', 'direction', 'entry', 'exit', 'pnl', 'pnl_pct', 'reason', 'live', 'time'])
    for t in get_ledger(lg_name)['trades']:
        w.writerow([t.get('coin', ''), t.get('direction', ''), t.get('entry', 0),
                    t.get('exit', 0), f"{t.get('pnl', 0):.6f}", f"{t.get('pnl_pct', 0):.3f}",
                    t.get('reason', ''), t.get('live', False), t.get('time', '')])
    return out.getvalue()

# ============ Telegram ============

def tg_proxies():
    p = os.environ.get('TG_PROXY', '').strip()
    if p:
        return {'http': p, 'https': p}
    return None

def send_telegram(msg):
    token = state.get('tg_token', '')
    chat = state.get('tg_chat', '')
    if not token or not chat:
        return False
    payload = {'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'}
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    try:
        r = requests.post(url, json=payload, timeout=10, proxies=tg_proxies())
        if r.status_code == 200:
            return True
    except Exception:
        pass
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

def tg_test():
    return send_telegram('🤖 ربات هایپرلیکوئید v26 وصل شد!')

tg_offset = [0]

def tg_poll():
    token = state.get('tg_token', '')
    chat = str(state.get('tg_chat', ''))
    if not token or not chat:
        return
    try:
        r = requests.get(f'https://api.telegram.org/bot{token}/getUpdates',
                         params={'offset': tg_offset[0] + 1, 'timeout': 0},
                         timeout=10, proxies=tg_proxies())
        if r.status_code != 200:
            return
        for upd in r.json().get('result', []):
            tg_offset[0] = max(tg_offset[0], upd['update_id'])
            msg = upd.get('message', {})
            if str(msg.get('chat', {}).get('id', '')) != chat:
                continue
            text = (msg.get('text') or '').strip().lower()
            if text.startswith('/status'):
                p_lg = get_ledger('paper')
                l_lg = get_ledger('live')
                send_telegram(
                    f"📊 وضعیت\nحالت: {'🔴 لایو+مجازی' if state['mode']=='live' else '🔵 فقط مجازی'}\n"
                    f"🔵 مجازی: {p_lg['capital']:.2f}$ ({len(p_lg['positions'])} باز)\n"
                    + (f"🔴 واقعی (HL): {(l_lg['capital'] or 0):.2f}$ ({len(l_lg['positions'])} باز)\n" if state['mode'] == 'live' else '')
                    + f"اسکن: {state['total_scans']} | توقف دستی: {'بله' if state.get('manual_paused') else 'خیر'}")
            elif text.startswith('/paper'):
                st = perf_stats('paper')
                if st:
                    send_telegram(f"🔵 موتور مجازی (۱۰۰$)\nمعاملات: {st['count']} | برد: {st['win_rate']}% "
                                  f"(CI {st['wr_ci'][0]}-{st['wr_ci'][1]})\nPF: {st['profit_factor']} | PnL: {st['total_pnl']:+.2f}$")
                else:
                    send_telegram('موتور مجازی هنوز معامله‌ای نداره')
            elif text.startswith('/stop'):
                state['manual_paused'] = True
                send_telegram('⏸ ورودهای جدید متوقف شد (بازها مدیریت می‌شن) - /start برای ادامه')
            elif text.startswith('/start'):
                state['manual_paused'] = False
                send_telegram('▶️ ادامه')
            elif text.startswith('/report'):
                for lg_name in ('paper', 'live'):
                    st = perf_stats(lg_name)
                    if st:
                        send_telegram(f"📈 {ledger_label(lg_name)}\n{st['count']} معامله | برد {st['win_rate']}% | "
                                      f"PF {st['profit_factor']} | PnL {st['total_pnl']:+.2f}$ | DD {st['max_drawdown']}%")
            elif text.startswith('/killswitch'):
                panic_close_all('telegram')
                send_telegram('🔴 کلید اضطراری: همه بسته شد و ربات متوقف شد')
            elif text.startswith('/ping'):
                send_telegram('🏓 پونگ! ربات فعاله')
            elif text.startswith('/help'):
                send_telegram('/status /paper /report /stop /start /killswitch /ping')
        save_state()
    except Exception:
        pass

# ============ reports / health ============

def daily_report_text():
    lines = [f"📋 گزارش روزانه - {fa_now().strftime('%Y-%m-%d')}", '']
    for lg_name in ('paper', 'live'):
        st = perf_stats(lg_name)
        if st:
            lines.append(f"{ledger_label(lg_name)}:")
            lines.append(f"معاملات: {st['count']} | برد: {st['win_rate']}% (CI {st['wr_ci'][0]}-{st['wr_ci'][1]})")
            lines.append(f"PF: {st['profit_factor']} | PnL: {st['total_pnl']:+.2f}$ | DD: {st['max_drawdown']}%")
            lines.append('')
    rg = state.get('regime') or {}
    lines.append(f"رژیم بازار: {REGIME_FA.get(rg.get('regime'), '-')}")
    lines.append(f"اسکن‌های انجام‌شده: {state['total_scans']}")
    return '\n'.join(lines)

def system_health():
    try:
        scan_age = time.time() - state.get('last_scan_ts', 0)
    except Exception:
        scan_age = 99999
    try:
        st = os.statvfs(BASE_DIR)
        disk_free = st.f_bavail * st.f_frsize / (1024 ** 3)
    except Exception:
        disk_free = None
    return {'scan_age_s': scan_age, 'scan_ok': scan_age < 1800, 'disk_free_gb': disk_free}

def heartbeat_text():
    p = get_ledger('paper')
    l = get_ledger('live')
    rg = (state.get('regime') or {}).get('regime', '-')
    return (f'💓 ربات زنده است\nحالت: {state["mode"]}\n'
            f'مجازی: {p["capital"]:.2f}$ ({len(p["positions"])} باز)\n'
            f'واقعی: {(l["capital"] or 0):.2f}$ ({len(l["positions"])} باز)\n'
            f'رژیم: {REGIME_FA.get(rg)} | اسکن: {state["total_scans"]}')

# ============ MAIN LOOP ============

def bot_loop():
    add_log(f'Hyperliquid bot v{STRATEGY_VERSION} started (paper always-on, live={"ON" if state["mode"]=="live" else "off"}, net={hl_base_url()})')
    state['status'] = 'Active'
    if state.get('mode') == 'live':
        ok, info = hl_test_connection()
        if not ok:
            add_log(f'LIVE CONFIG INCOMPLETE: {info}')
            send_telegram(f'⚠️ حالت لایو فعاله ولی اتصال هایپرلیکوئید برقرار نیست: {info}')
        else:
            add_log(f'HL connection OK: {info}')
            sync_live_capital()
            reconcile_live_positions()
    last_scan = last_manage = last_funding = last_optimize = 0.0
    last_heartbeat = time.time()
    while True:
        try:
            now = time.time()
            if state.get('tg_token') and state.get('tg_chat'):
                hhmm = fa_now().strftime('%H:%M')
                today_s = fa_now().strftime('%Y-%m-%d')
                if hhmm >= '22:30' and state.get('last_daily_report') != today_s:
                    state['last_daily_report'] = today_s
                    try:
                        send_telegram(daily_report_text())
                        add_log('Daily report sent')
                    except Exception:
                        log_exception('daily report failed')
                hb_iv = int(state.get('heartbeat_hours', 0) or 0)
                if hb_iv > 0 and now - last_heartbeat > hb_iv * 3600:
                    last_heartbeat = now
                    send_telegram(heartbeat_text())
            if now - state.get('last_poll', 0) > 8:
                state['last_poll'] = now
                tg_poll()
            if now - state.get('last_selfguard', 0) > 600:
                state['last_selfguard'] = now
                try:
                    h = system_health()
                    if h['disk_free_gb'] is not None and h['disk_free_gb'] < 0.5 and not state.get('disk_warned'):
                        state['disk_warned'] = True
                        send_telegram(f'🚨 فضای دیسک کمه ({h["disk_free_gb"]:.1f}G)!')
                    elif h['disk_free_gb'] is not None and h['disk_free_gb'] > 1.0:
                        state['disk_warned'] = False
                    open_any = len(get_ledger('paper')['positions']) + len(get_ledger('live')['positions'])
                    if open_any and h['scan_age_s'] > 1800 and not state.get('blackout_warned'):
                        state['blackout_warned'] = True
                        send_telegram(f'🚨 {open_any} معامله بازه و {h["scan_age_s"]//60} دقیقه‌ست قیمت نداریم! اینترنت/سرور رو چک کن')
                    elif h['scan_ok']:
                        state['blackout_warned'] = False
                    if state.get('mode') == 'live' and now - state.get('last_reconcile', 0) > 1800:
                        state['last_reconcile'] = now
                        sync_live_capital(quiet=True)
                        reconcile_live_positions()
                except Exception:
                    log_exception('self guard failed')
            if now - last_funding > 1800:
                last_funding = now
                try:
                    update_funding()
                    old_r = (state.get('regime') or {}).get('regime')
                    rg = detect_regime()
                    if rg:
                        state['regime'] = rg
                        if rg['regime'] != old_r and old_r is not None:
                            send_telegram(f'🔄 رژیم بازار: {REGIME_FA.get(rg["regime"])}')
                    crash_guard_active()
                    evaluate_shadows()
                except Exception:
                    log_exception('regime/funding tick')
            if now - last_optimize > 86400:
                last_optimize = now
                try:
                    update_coin_filter()
                except Exception:
                    pass
            if now - last_manage > POS_CHECK_INTERVAL:
                last_manage = now
                try:
                    manage_all_positions()
                except Exception:
                    log_exception('manage tick')
            if now - last_scan > SCAN_INTERVAL:
                last_scan = now
                try:
                    if get_prices():
                        state['last_scan_ts'] = now
                        state['total_scans'] = state.get('total_scans', 0) + 1
                        sigs = analyze()
                        if sigs:
                            for sig in sigs:
                                if open_engine_position('paper', sig):
                                    break
                            if state.get('mode') == 'live' and not state.get('crash_mode'):
                                for sig in sigs:
                                    if open_engine_position('live', sig):
                                        break
                        save_state()
                except Exception:
                    log_exception('scan tick')
            if now - state.get('last_save', 0) > 60:
                state['last_save'] = now
                save_state()
        except Exception:
            log_exception('bot_loop tick')
        time.sleep(5)

# ============ Web dashboard ============

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Vazirmatn,Tahoma,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;direction:rtl;padding:16px}
a{color:#58a6ff;text-decoration:none}
.top{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
.top h1{font-size:20px;color:#fff}
.pill{background:#161b22;border:1px solid #30363d;border-radius:999px;padding:4px 14px;font-size:12px}
.pill.green{background:#12261a;border-color:#238636;color:#3fb950}
.pill.red{background:#2d1216;border-color:#da3633;color:#f85149}
.pill.orange{background:#2d1f0f;border-color:#9e6a03;color:#d29922}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-bottom:12px}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:14px}
.card h3{font-size:14px;color:#8b949e;margin-bottom:10px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:600}
.green{color:#3fb950}.red{color:#f85149}.orange{color:#d29922}
button{background:#238636;color:#fff;border:0;border-radius:8px;padding:8px 16px;cursor:pointer;font-size:13px}
button.danger{background:#da3633}
button.gray{background:#30363d}
input,select{background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:8px;padding:8px;margin:4px 0}
label{font-size:12px;color:#8b949e;display:block;margin-top:8px}
.log{font-size:11px;color:#8b949e;max-height:220px;overflow-y:auto;line-height:1.7}
.log b{color:#c9d1d9}
.mono{font-family:'Courier New',monospace;direction:ltr;text-align:left}
.small{font-size:11px;color:#8b949e}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
"""

def shell(title, body, refresh=45):
    mode = state.get('mode')
    mode_pill = ('<span class="pill green">🔵 فقط مجازی (Paper)</span>' if mode != 'live'
                 else '<span class="pill red">🔴 لایو + مجازی</span>')
    live_pill = ('<span class="pill orange">⚠️ لایو پیکربندی نشده</span>' if (mode == 'live' and not state.get('hl_agent_ok'))
                 else '')
    hh = f"""<!DOCTYPE html><html lang="fa"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ربات هایپرلیکوئید</title><style>{_CSS}</style></head><body>
<div class="top"><h1>🤖 ربات هایپرلیکوئید v{STRATEGY_VERSION}</h1>
{mode_pill}{live_pill}
<span class="pill">اسکن: {state['total_scans']}</span>
<span class="pill">قیمت: {state.get('prices_ts','-')}</span>
<span class="pill">رژیم: {REGIME_FA.get((state.get('regime') or {}).get('regime'),'-')}</span>
</div>
<div class="top" style="margin-top:-8px">
<a href="/">🏠 خانه</a> <a href="/paper">🔵 مجازی</a>
<a href="/settings">⚙️ تنظیمات</a> <a href="/analytics">📊 تحلیل</a>
<a href="/csv">📥 خروجی CSV</a>
</div>
{body}
<div class="small" style="margin-top:16px">بازنمایی هر {refresh} ثانیه | شروع: {state.get('start_time','-')}</div>
<script>setTimeout(function(){{location.reload()}}, {refresh * 1000});</script>
</body></html>"""
    return hh

def positions_html(lg_name):
    lg = get_ledger(lg_name)
    if not lg['positions']:
        return '<div class="small">موقعیت باز وجود ندارد</div>'
    rows = []
    for p in lg['positions']:
        cur = state['prices'].get(p['coin'], p['entry_price'])
        g = ((cur - p['entry_price']) / p['entry_price']) if p['direction'] == 'long' else ((p['entry_price'] - cur) / p['entry_price'])
        cls = 'green' if g > 0 else 'red'
        sz_txt = f"{p.get('size') or ''}" if p.get('live') else f"{p['margin']:.2f}$"
        rows.append(f"<tr><td>{COIN_FA.get(p['coin'], p['coin'])}</td><td>{'خرید 📈' if p['direction']=='long' else 'فروش 📉'}</td>"
                    f"<td class='mono'>{fmt_price(p['entry_price'])}</td><td class='mono'>{fmt_price(cur)}</td>"
                    f"<td class='mono'>{sz_txt}</td><td class='{cls}'>{g*100:+.2f}%</td>"
                    f"<td class='mono'>{fmt_price(p['stop_loss'])}</td><td class='mono'>{fmt_price(p['take_profit'])}</td>"
                    f"<td class='small'>{(time.time()-p.get('open_ts',time.time()))/3600:.1f}h</td></tr>")
    return f"<table><tr><th>ارز</th><th>جهت</th><th>ورود</th><th>الان</th><th>حجم</th><th>سود</th><th>SL</th><th>TP</th><th>سن</th></tr>{''.join(rows)}</table>"

def stats_card(lg_name, base):
    st = perf_stats(lg_name)
    if not st:
        return f"<div class='card'><h3>{ledger_label(lg_name)}</h3><div class='small'>هنوز معامله‌ای نیست</div></div>"
    return f"""<div class='card'><h3>{ledger_label(lg_name)}</h3>
<table><tr><td>سرمایه</td><td class='mono'>{get_ledger(lg_name)['capital']:.2f}$</td></tr>
<tr><td>معاملات</td><td>{st['count']}</td></tr>
<tr><td>وین‌ریت</td><td>{st['win_rate']}% <span class='small'>(CI {st['wr_ci'][0]}-{st['wr_ci'][1]})</span></td></tr>
<tr><td>پروفیت فاکتور</td><td class='{pf_color(st["profit_factor"])}'>{st['profit_factor']}</td></tr>
<tr><td>سود/زیان کل</td><td class='{"green" if st["total_pnl"]>0 else "red"}'>{st['total_pnl']:+.2f}$</td></tr>
<tr><td>حداکثر افت</td><td>{st['max_drawdown']}%</td></tr>
<tr><td>شارپ/سورتینو</td><td class='mono'>{st['sharpe']} / {st['sortino']}</td></tr></table></div>"""

def scan_table_html():
    rows = state.get('scan_table', [])
    if not rows:
        return '<div class="small">—</div>'
    trs = ''.join(f"<tr><td>{COIN_FA.get(r['coin'], r['coin'])}</td>"
                  f"<td class='{'green' if r['score']>0 else 'red'}'>{r['score']:+.1f}</td>"
                  f"<td>{'خرید' if r['direction']=='long' else ('فروش' if r['direction']=='short' else '-')}</td>"
                  f"<td>{r['rsi']}</td></tr>" for r in rows[:14])
    return f"<table><tr><th>ارز</th><th>امتیاز</th><th>جهت</th><th>RSI</th></tr>{trs}</table>"

def recent_trades_html(lg_name, n=8):
    trades = get_ledger(lg_name)['trades'][-n:][::-1]
    if not trades:
        return '<div class="small">—</div>'
    trs = ''.join(f"<tr><td>{COIN_FA.get(t.get('coin'), t.get('coin'))}</td>"
                  f"<td>{'خرید' if t.get('direction')=='long' else 'فروش'}</td>"
                  f"<td class='{"green" if t.get('pnl',0)>0 else "red"}'>{t.get('pnl',0):+.3f}$</td>"
                  f"<td class='small'>{t.get('reason','')}</td>"
                  f"<td class='small'>{t.get('time','')[:16]}</td></tr>" for t in trades)
    return f"<table><tr><th>ارز</th><th>جهت</th><th>PNL</th><th>علت</th><th>زمان</th></tr>{trs}</table>"

def logs_html():
    logs = state.get('logs', [])[-25:][::-1]
    if not logs:
        return '<div class="small">—</div>'
    return '<div class="log">' + ''.join(f"<div><b>{l['t']}</b> {l['m']}</div>" for l in logs) + '</div>'

def paper_page(main=False):
    p = get_ledger('paper')
    body = f"""
<div class='grid'>
{stats_card('paper', PAPER_CAPITAL)}
<div class='card'><h3>📡 وضعیت بازار</h3>{regime_card()}{session_line()}</div>
</div>
<div class='grid'>
<div class='card'><h3>📈 موقعیت‌های باز (مجازی)</h3>{positions_html('paper')}</div>
<div class='card'><h3>🔍 سیگنال‌های اخیر</h3>{scan_table_html()}</div>
</div>
<div class='grid'>
<div class='card'><h3>🕘 معاملات اخیر</h3>{recent_trades_html('paper')}</div>
<div class='card'><h3>📜 لاگ</h3>{logs_html()}</div>
</div>"""
    return body

def regime_card():
    rg = state.get('regime') or {}
    if not rg:
        return '<div class="small">در حال محاسبه...</div>'
    return (f"<table><tr><td>رژیم</td><td>{REGIME_FA.get(rg.get('regime'), '-')}</td></tr>"
            f"<tr><td>نوسان</td><td>{rg.get('volatility')}%</td></tr>"
            f"<tr><td>روند</td><td>{rg.get('trend_pct')}%</td></tr>"
            f"<tr><td>بهره‌وری</td><td>{rg.get('efficiency')}</td></tr></table>")

def session_line():
    s = session_info()
    return f"<div class='small' style='margin-top:8px'>جلسه: {s['label']} ({s['hour']} تهران)</div>"

def create_settings_html(message=''):
    msg = f"<div class='green'>{message}</div>" if message else ''
    mode_sel = state.get('mode')
    live_ok = state.get('hl_agent_ok')
    conn = '<span class="pill green">✅ اتصال برقرار</span>' if live_ok else '<span class="pill red">⚠️ پیکربندی ناقص</span>'
    testnet = 'آزمایشی (Testnet)' if os.environ.get('HL_TESTNET', '').strip().lower() == 'true' else 'واقعی (Mainnet)'
    body = f"""
{msg}
<div class='grid'>
<div class='card'><h3>⚙️ حالت اجرا</h3>
<form method='post' action='/action'>
<input type='hidden' name='do' value='setmode'>
<label>حالت</label>
<select name='mode'>
<option value='paper' {'selected' if mode_sel=='paper' else ''}>🔵 فقط مجازی (Paper)</option>
<option value='live' {'selected' if mode_sel=='live' else ''}>🔴 لایو + مجازی</option>
</select>
<button type='submit' style='margin-top:10px'>ذخیره</button>
</form>
<div class='small' style='margin-top:8px'>شبکه: {testnet} | {conn}</div>
</div>
<div class='card'><h3>🛡️ کنترل اضطراری</h3>
<div class='actions'>
<form method='post' action='/action'><input type='hidden' name='do' value='pause'><button class='gray' type='submit'>⏸ توقف ورود جدید</button></form>
<form method='post' action='/action'><input type='hidden' name='do' value='resume'><button type='submit'>▶️ ادامه</button></form>
<form method='post' action='/action'><input type='hidden' name='do' value='kill'><button class='danger' type='submit'>🔴 توقف اضطراری (همه بسته)</button></form>
</div>
</div>
</div>
<div class='grid'>
<div class='card'><h3>📬 تنظیمات تلگرام</h3>
<form method='post' action='/action'>
<input type='hidden' name='do' value='settg'>
<label>توکن ربات تلگرام</label>
<input type='text' name='tg_token' value='{state.get('tg_token','')}' style='width:100%'>
<label>Chat ID</label>
<input type='text' name='tg_chat' value='{state.get('tg_chat','')}' style='width:100%'>
<label>گزارش قلب تپنده (ساعت، 0=خاموش)</label>
<input type='number' name='hb' value='{state.get('heartbeat_hours',0)}' style='width:100%'>
<button type='submit' style='margin-top:10px'>ذخیره</button>
</form></div>
<div class='card'><h3>🔑 اتصال هایپرلیکوئید (Agent Wallet)</h3>
<div class='small' style='line-height:1.9'>
آدرس حساب اصلی (Master): <b class='mono'>{os.environ.get('HL_ACCOUNT_ADDRESS','—')}</b><br>
کلید Agent: {'✅ تنظیم شده' if os.environ.get('HL_AGENT_PRIVATE_KEY','') else '❌ تنظیم نشده'}<br>
برای راه‌اندازی کامل، اسکریپت <b>setup_hyperliquid.py</b> را روی سرور اجرا کنید.
</div></div>
</div>"""
    return body

def create_analytics_html():
    b = run_backtest('BTC', days=14)
    b_eth = run_backtest('ETH', days=14)
    def bt_card(coin, res):
        if not res:
            return f"<div class='card'><h3>بک‌تست {COIN_FA.get(coin)}</h3><div class='small'>داده کافی نیست</div></div>"
        return f"""<div class='card'><h3>بک‌تست {COIN_FA.get(coin)} (۱۴ روز)</h3>
<table><tr><td>معاملات</td><td>{res['trades']}</td></tr>
<tr><td>وین‌ریت</td><td>{res['win_rate']}%</td></tr>
<tr><td>پروفیت فاکتور</td><td class='{pf_color(res['profit_factor'])}'>{res['profit_factor']}</td></tr>
<tr><td>بازده کل</td><td class='{"green" if res["total_pct"]>0 else "red"}'>{res['total_pct']:+.2f}%</td></tr></table></div>"""
    return f"""<div class='grid'>{bt_card('BTC', b)}{bt_card('ETH', b_eth)}
<div class='card'><h3>🎯 آستانه فعلی</h3>
<table><tr><td>آستانه سیگنال</td><td>{live_threshold()}</td></tr>
<tr><td>تعدیل یادگیری</td><td>{state.get('threshold_extra',0):+.1f}</td></tr>
<tr><td>SL/TP پایه</td><td class='mono'>{get_tuned()['sl']*100:.1f}% / {get_tuned()['tp']*100:.1f}%</td></tr>
<tr><td>سایه‌های در انتظار</td><td>{len(state.get('shadow_signals', []))}</td></tr></table></div>
</div>"""

def live_overview_page():
    l = get_ledger('live')
    ok = state.get('hl_agent_ok')
    if not ok:
        body = f"""
<div class='card'><h3>🔴 موتور واقعی</h3>
<div class='small'>برای فعال‌سازی لایو ابتدا کلیدهای هایپرلیکوئید را تنظیم کنید
(<b>setup_hyperliquid.py</b> را اجرا کنید).</div></div>"""
        return body
    body = f"""
<div class='grid'>{stats_card('live', live_base_capital())}</div>
<div class='grid'>
<div class='card'><h3>📈 موقعیت‌های باز (واقعی - هایپرلیکوئید)</h3>{positions_html('live')}</div>
</div>
<div class='grid'>
<div class='card'><h3>🕘 معاملات واقعی اخیر</h3>{recent_trades_html('live')}</div>
<div class='card'><h3>📜 لاگ</h3>{logs_html()}</div>
</div>"""
    return body

def home_page():
    body = f"""
<div class='grid'>{stats_card('paper', PAPER_CAPITAL)}</div>
<div class='grid'>
<div class='card'><h3>📈 موقعیت‌های باز (مجازی)</h3>{positions_html('paper')}</div>
<div class='card'><h3>🔍 سیگنال‌های اخیر</h3>{scan_table_html()}</div>
</div>
<div class='grid'>
<div class='card'><h3>🕘 معاملات اخیر (مجازی)</h3>{recent_trades_html('paper')}</div>
<div class='card'><h3>📜 لاگ</h3>{logs_html()}</div>
</div>"""
    return body

# ---------- HTTP handler ----------

def _hash_pw(pw):
    return hashlib.sha256(('hlbot::' + pw).encode()).hexdigest()

def dash_pass():
    env = os.environ.get('DASH_PASS', '').strip()
    if env:
        return env
    return state.get('dash_pass', '')

def auth_ok(cookie):
    try:
        return cookie == _hash_pw(dash_pass())
    except Exception:
        return False

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        try:
            self._handle()
        except Exception:
            log_exception('web get')

    def _handle(self):
        path = urllib.parse.urlparse(self.path).path
        cookie = self.headers.get('Cookie', '') or ''
        if dash_pass() and 'sess=' not in cookie:
            cookie_s = ''
            for part in cookie.split(';'):
                if part.strip().startswith('sess='):
                    cookie_s = part.strip()[5:]
            if not auth_ok(cookie_s):
                body = f"""<!DOCTYPE html><html lang='fa'><head><meta charset='utf-8'><title>ورود</title><style>{_CSS}</style></head><body>
<div class='card' style='max-width:360px;margin:80px auto'>
<h3>🔐 ورود به داشبورد</h3>
<form method='post' action='/login'>
<label>رمز عبور</label><input type='password' name='pw' style='width:100%'>
<button type='submit' style='margin-top:10px'>ورود</button></form></div></body></html>"""
                self._send_html(body)
                return
        if path == '/':
            self._send_html(shell('خانه', home_page()))
        elif path == '/paper':
            self._send_html(shell('مجازی', paper_page()))
        elif path == '/live':
            self._send_html(shell('واقعی', live_overview_page()))
        elif path == '/settings':
            self._send_html(shell('تنظیمات', create_settings_html()))
        elif path == '/analytics':
            self._send_html(shell('تحلیل', create_analytics_html()))
        elif path == '/csv':
            data = trades_csv('paper').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename=trades.csv')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)
            return
        self._try_save()

    def _try_save(self):
        try:
            if time.time() - state.get('_last_web_save', 0) > 30:
                state['_last_web_save'] = time.time()
                save_state()
        except Exception:
            pass

    def _send_html(self, body):
        data = body.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            length = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(length).decode('utf-8', 'replace') if length else ''
            params = urllib.parse.parse_qs(raw)
            def gv(k):
                return (params.get(k) or [''])[0]
            if path == '/login':
                pw = gv('pw')
                if _hash_pw(pw) == _hash_pw(dash_pass()):
                    self.send_response(302)
                    self.send_header('Location', '/')
                    self.send_header('Set-Cookie', f'sess={_hash_pw(pw)}; Path=/; Max-Age=604800')
                    self.end_headers()
                else:
                    self.send_response(302)
                    self.send_header('Location', '/')
                    self.end_headers()
                return
            cookie = self.headers.get('Cookie', '') or ''
            sess = ''
            for part in cookie.split(';'):
                if part.strip().startswith('sess='):
                    sess = part.strip()[5:]
            if dash_pass() and not auth_ok(sess):
                self.send_response(302)
                self.send_header('Location', '/')
                self.end_headers()
                return
            if path == '/action':
                action = gv('do')
                if action == 'setmode':
                    with state_lock:
                        state['mode'] = 'paper' if gv('mode') == 'paper' else 'live'
                    save_state()
                    msg = f"حالت به {'paper' if state['mode']=='paper' else 'live'} تغییر کرد"
                    add_log(f'Mode set to {state["mode"]} via dashboard')
                    if state['mode'] == 'live':
                        ok, info = hl_test_connection()
                        state['hl_agent_ok'] = ok
                        if ok:
                            sync_live_capital()
                            reconcile_live_positions()
                        msg += f" | اتصال HL: {info}"
                    self._send_html(shell('تنظیمات', create_settings_html(msg)))
                    return
                elif action == 'pause':
                    state['manual_paused'] = True
                    add_log('Manual pause via dashboard')
                    self.send_response(302); self.send_header('Location', '/settings'); self.end_headers()
                    return
                elif action == 'resume':
                    state['manual_paused'] = False
                    add_log('Resume via dashboard')
                    self.send_response(302); self.send_header('Location', '/settings'); self.end_headers()
                    return
                elif action == 'kill':
                    panic_close_all('dashboard')
                    self.send_response(302); self.send_header('Location', '/settings'); self.end_headers()
                    return
                elif action == 'settg':
                    with state_lock:
                        state['tg_token'] = gv('tg_token').strip()
                        state['tg_chat'] = gv('tg_chat').strip()
                        try:
                            state['heartbeat_hours'] = int(float(gv('hb') or 0))
                        except Exception:
                            pass
                    save_state()
                    tg_test()
                    self._send_html(shell('تنظیمات', create_settings_html('تنظیمات تلگرام ذخیره شد')))
                    return
            self.send_response(302)
            self.send_header('Location', '/')
            self.end_headers()
        except Exception:
            log_exception('web post')
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

def start_server(port=8080):
    try:
        httpd = ThreadingHTTPServer(('0.0.0.0', port), Handler)
        add_log(f'Web dashboard on port {port}')
        httpd.serve_forever()
    except Exception:
        log_exception('web server failed')

# ============ Self test ============

def run_selftest():
    """Offline sanity tests (no network required)."""
    tests = []
    def T(name, fn):
        try:
            ok = fn()
            tests.append((name, ok, ''))
        except Exception as e:
            tests.append((name, False, str(e)))

    def t_engine_long():
        lg = fresh_ledger('paper')
        lg['capital'] = 100.0
        pos = engine_open(lg, coin='BTC', direction='long', price=100.0, margin=10.0,
                          leverage=5, sl_pct=0.02, tp_pct=0.03, ts=time.time())
        net = engine_exit_leg(lg, pos, 1.0, 102.0)  # +2% * 5x * 10$ = +1$ - fee
        return abs(net - (1.0 - 10 * 5 * FEE_RATE)) < 1e-9
    T('موتور: سود لانگ', t_engine_long)

    def t_engine_short():
        lg = fresh_ledger('paper')
        lg['capital'] = 100.0
        pos = engine_open(lg, coin='BTC', direction='short', price=100.0, margin=10.0,
                          leverage=5, sl_pct=0.02, tp_pct=0.03, ts=time.time())
        net = engine_exit_leg(lg, pos, 1.0, 98.0)  # +2% * 5x * 10$ = +1$ - fee
        return abs(net - (1.0 - 10 * 5 * FEE_RATE)) < 1e-9
    T('موتور: سود شورت', t_engine_short)

    def t_partial():
        lg = fresh_ledger('paper')
        lg['capital'] = 100.0
        pos = engine_open(lg, coin='ETH', direction='long', price=100.0, margin=10.0,
                          leverage=5, sl_pct=0.02, tp_pct=0.03, ts=time.time())
        engine_exit_leg(lg, pos, 0.4, 101.0)
        return abs(pos['margin'] - 6.0) < 1e-9 and len(lg['positions']) == 1
    T('موتور: برداشت پله‌ای', t_partial)

    def t_rsi():
        prices = [100 + (i % 7) * 2 for i in range(20)]
        gains, losses = [], []
        for i in range(1, len(prices)):
            ch = prices[i] - prices[i - 1]
            gains.append(max(0, ch))
            losses.append(max(0, -ch))
        ag, al = np.mean(gains[-14:]), np.mean(losses[-14:])
        rsi = 100 - (100 / (1 + ag / max(al, 1e-9)))
        return 0 <= rsi <= 100
    T('RSI در بازه معتبر', t_rsi)

    def t_regime():
        # synthetic range data -> 'range'
        import numpy as _np
        closes = [100 + _np.sin(i / 3) * 0.5 for i in range(48)]
        arr = _np.array(closes, dtype=float)
        rets = _np.diff(arr) / arr[:-1]
        vol = float(_np.std(rets[-24:]))
        seg = arr[-24:]
        x = _np.arange(len(seg))
        slope = float(_np.polyfit(x, seg, 1)[0])
        trend_pct = slope * 24 / float(_np.mean(seg))
        net = abs(seg[-1] - seg[0])
        path = float(_np.sum(_np.abs(_np.diff(seg)))) or 1.0
        eff = float(net / path)
        if vol > 0.012:
            rg = 'storm'
        elif eff > 0.35 and trend_pct > 0.008:
            rg = 'trend_up'
        elif eff > 0.35 and trend_pct < -0.008:
            rg = 'trend_down'
        else:
            rg = 'range'
        return rg == 'range'
    T('تشخیص رژیم رنج', t_regime)

    def t_session():
        from datetime import datetime as _dt
        return current_session(_dt(2026, 8, 20, 13, 0, tzinfo=TEHRAN)) == 'europe'
    T('تشخیص جلسه اروپا', t_session)

    def t_wilson():
        lo, hi = wilson_ci(5, 10)
        return lo < 50 < hi
    T('فاصله اطمینان وین‌ریت', t_wilson)

    def t_dynamic():
        sl, tp, vr = dynamic_levels('BTC')
        return vr in ('high', 'low', 'normal') and sl > 0 < tp
    T('سطوح پویا', t_dynamic)

    def t_kelly():
        k = kelly_risk('paper')
        return 0.05 <= k <= 0.15
    T('ریسک کلی', t_kelly)

    def t_bt():
        r = _bt_entry_signal([100 + i * 0.1 for i in range(30)], None, 25, 'BTC', 4, 0.02, 0.03)
        return r is None or r['direction'] in ('long', 'short')
    T('سیگنال بک‌تست', t_bt)

    failed = [t for t in tests if not t[1]]
    print(f"\n===== Self test: {len(tests) - len(failed)}/{len(tests)} passed =====")
    for name, ok, err in tests:
        print(f"  {'✅' if ok else '❌'} {name}{' - ' + err if err else ''}")
    return 1 if failed else 0

# ============ server & bootstrap ============

def main():
    print("""
==============================================================
  Hyperliquid Trading Bot v26 (Paper engine + optional Live)
  Strategy: port of Nobitex v25 -> Hyperliquid perps
  Dashboard:  http://localhost:8080
  Setup keys: python3 setup_hyperliquid.py
==============================================================
""")
    load_state()
    env_pass = os.environ.get('DASH_PASS', '').strip()
    if env_pass:
        state['dash_pass'] = env_pass
    env_tg_tok = os.environ.get('TG_TOKEN', '').strip()
    env_tg_chat = os.environ.get('TG_CHAT', '').strip()
    if env_tg_tok and env_tg_chat:
        state['tg_token'] = env_tg_tok
        state['tg_chat'] = env_tg_chat
    env_mode = os.environ.get('MODE', '').strip().lower()
    if env_mode in ('paper', 'live'):
        if state.get('mode') != env_mode:
            add_log(f'MODE override from .env: {env_mode}')
        state['mode'] = env_mode
    elif state.get('mode') not in ('paper', 'live'):
        state['mode'] = 'paper'
    state['hl_account'] = hl_account()
    state['hl_agent_ok'] = hl_exchange() is not None
    print('[2/4] Web server...')
    threading.Thread(target=start_server, daemon=True).start()
    print('[3/4] OK')
    print(f'[3.5/4] Live engine: {"ready (keys found)" if state["hl_agent_ok"] else "paper-only (run setup_hyperliquid.py for live)"}')
    print('[4/4] Bot loop... (Ctrl+C to stop)\n')
    bot_loop()

if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(run_selftest())
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped by user.')
    except Exception:
        log_exception('FATAL: bot crashed')
        try:
            send_telegram('🔴 ربات کرش کرد! جزئیات در app.log')
        except Exception:
            pass
        raise
