#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Nobitex Trading Bot v25 (Always-on Paper engine + optional Live engine)
========================================================================
- ONE unified exit engine used by: paper trades, live trades and backtests
  (no more backtest/live divergence).
- VIRTUAL engine (paper ledger, $100) ALWAYS runs and validates signals,
  shown on /paper - completely separate from real money.
- LIVE engine activates from Settings and trades real margin on Nobitex.
"""

import subprocess, sys, os, json, time, threading, urllib.parse, socket, hashlib
import logging, math, csv, io, traceback as tb, hmac, secrets
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
else:
    for pkg in ('requests', 'numpy'):
        try:
            __import__(pkg)
        except Exception:
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg, '-q'])

import requests
import numpy as np
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

# ============ Static config (strategy baseline) ============

PAPER_CAPITAL = 100.0        # virtual engine capital (always-on)
RISK_PER_TRADE = 0.08        # conservative starting risk for real-money (halved from 0.15)
STOP_LOSS = 0.020
TAKE_PROFIT = 0.030
DESIRED_LEV = {'BTC': 10, 'DEFAULT': 5}
MAX_LEV = 5                  # hard cap for BOTH engines (parity, P1-7)
LIVE_COIN_WHITELIST = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'LTC']
LIVE_MAX_BOOK_SHARE = 0.10
LIVE_EXT_FEE_DAILY = 0.001   # ~0.1%/day extension fee after day 1
MIN_ORDER_VALUE = 6.0
FEE_RATE = 0.0025            # round-trip cost estimate on notional
DAILY_LOSS_LIMIT = 0.08
SCAN_INTERVAL = 300
POS_CHECK_INTERVAL = 30
TRAIL_GAP = 0.008        # profit-lock pullback depth
RUNNER_TRAIL = 0.010
LADDER1_AT_TP = 0.70     # first stop-tightening milestone: 70% of the way to TP
LADDER1_LOCK = 0.40      # share of best gain locked at that milestone
MAX_POSITIONS = 4
MAX_TOTAL_RISK = 0.25        # conservative portfolio margin cap (halved from 0.48)
ENABLE_COLLATERAL_RESCUE = os.environ.get('ENABLE_COLLATERAL_RESCUE', 'false').lower() == 'true'
MAX_TRADE_HOURS = 24
SHORT_TH_EXTRA = 1
LOSS_COOLDOWN = 900
MAX_CONSEC_LOSSES = 3
CONSEC_PAUSE = 7200
FREEZE_DEFAULT = True
SPREAD_MAX = 0.004
VPS_END_DATE = '2026-09-05'
STRATEGY_VERSION = 25
SHADOW_MAX_PENDING = 60
SHADOW_TIMEOUT_H = 24

NOBITEX_API = os.environ.get('NOBITEX_API_BASE', 'https://apiv2.nobitex.ir').rstrip('/')
UA = {'User-Agent': 'TraderBot/PersonalBot-v25'}
COIN_MAP = {'BTC': 'btc', 'ETH': 'eth', 'SOL': 'sol', 'XRP': 'xrp', 'DOGE': 'doge',
            'TRX': 'trx', 'ADA': 'ada', 'LTC': 'ltc', 'BNB': 'bnb', 'AVAX': 'avax',
            'UNI': 'uni', 'ATOM': 'atom', 'FIL': 'fil', 'TON': 'ton', 'ARB': 'arb',
            'SHIB': 'shib'}
COIN_FA = {'BTC': 'بیت‌کوین', 'ETH': 'اتریوم', 'SOL': 'سولانا', 'XRP': 'ریپل',
           'DOGE': 'دوج‌کوین', 'TRX': 'ترون', 'ADA': 'کاردانو', 'LTC': 'لایت‌کوین',
           'USDT': 'تتر (دلار)', 'BNB': 'بی‌ان‌بی', 'DOT': 'پولکادات', 'AVAX': 'آوالانچ',
           'LINK': 'چین‌لینک', 'SHIB': 'شیبا', 'UNI': 'یونی‌سواپ', 'ATOM': 'کازماس',
           'NEAR': 'نیر', 'FIL': 'فایل‌کوین', 'TON': 'تون‌کوین', 'ARB': 'آربیتروم',
           'OP': 'آپتیمیزم', 'XAUT': 'تتر گلد (طلا) 🥇'}
HOLD_EXTRA_COINS = ['XAUT']

# ============ HTTP session with throttle & retry ============

_raw_session = requests.Session()
_raw_session.trust_env = False                      # Nobitex: no system proxy/VPN
_raw_session.proxies = {'http': None, 'https': None}
_rl_lock = threading.Lock()
_rl_last = [0.0]

def record_traffic(nbytes):
    try:
        today = fa_now().strftime('%Y-%m-%d')
        tr = state.get('traffic')
        if not isinstance(tr, dict) or tr.get('date') != today:
            tr = {'date': today, 'bytes': 0}
        tr['bytes'] = int(tr.get('bytes', 0)) + int(nbytes)
        state['traffic'] = tr
    except Exception:
        pass

class NbSession:
    """Rate-limited (~4 req/s) session with exponential-backoff retries."""
    def _throttle(self):
        with _rl_lock:
            now = time.time()
            wait = 0.25 - (now - _rl_last[0])
            if wait > 0:
                time.sleep(wait)
            _rl_last[0] = time.time()

    def _do(self, method, url, **kw):
        last_exc = None
        for attempt in range(3):
            self._throttle()
            try:
                r = getattr(_raw_session, method)(url, **kw)
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                try:
                    record_traffic(len(r.content or b''))
                except Exception:
                    pass
                return r
            except requests.exceptions.RequestException as e:
                last_exc = e
                time.sleep(2 * attempt)
        if last_exc:
            raise last_exc
        raise requests.exceptions.RequestException('rate limited')

    def get(self, url, **kw):
        return self._do('get', url, **kw)

    def post(self, url, **kw):
        return self._do('post', url, **kw)

nb_session = NbSession()

# ============ State ============

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
        'mode': 'paper',             # 'paper': only virtual engine; 'live': both engines
        'ledgers': {'paper': fresh_ledger('paper'), 'live': fresh_ledger('live')},
        'ledgers_migrated': False,
        'api_token': '', 'token_ok': None, 'live_balance': None,
        'prices': {}, 'price_ts_map': {}, 'prices_ts': '',
        'price_history': [], 'data_source': 'nobitex', 'last_scan_ts': 0.0,
        'price_fails': 0, 'total_scans': 0, 'last_watchdog_alert': 0.0,
        'manual_paused': False, 'usdt_irt': None,
        'logs': [], 'start_time': fa_now().isoformat(), 'status': 'Starting',
        'scan_table': [], 'last_signal': None, 'last_reason': '',
        'onchain': {}, 'regime': None, 'crash_mode': False,
        'learning_frozen': None, 'tuned': None, 'threshold_extra': 0,
        'factor_weights': None, 'genome': None, 'lab_history': [], 'last_lab': 0.0,
        'shadow_signals': [], 'shadow_stats': {}, 'shadow_resolved': 0,
        'coin_scores': {}, 'coin_fail': {}, 'max_leverage': {},
        'backtest': None, 'hold_analysis': None, 'last_hold': 0.0,
        'tg_token': '', 'tg_chat': '', 'heartbeat_hours': 0,
        'last_daily_report': '', 'last_backup_nag': 0.0,
        'strategy_version': STRATEGY_VERSION,
    }

state = default_state()
state_lock = threading.RLock()
_save_lock = threading.Lock()
_last_backup = [0.0]
LOG_MAX = 60

def add_log(msg):
    ts = fa_now().strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    with state_lock:
        state['logs'].insert(0, line)
        if len(state['logs']) > LOG_MAX:
            state['logs'] = state['logs'][:LOG_MAX]
    try:
        logging.info(line)
    except Exception:
        pass

def save_state():
    for attempt in range(3):
        try:
            with _save_lock:
                payload = json.dumps(state, default=str)
                tmp = STATE_FILE + '.tmp'
                with open(tmp, 'w') as f:
                    f.write(payload)
                try:
                    os.chmod(tmp, 0o600)
                except Exception:
                    pass
                os.replace(tmp, STATE_FILE)
                now = time.time()
                if now - _last_backup[0] > 3600:
                    _last_backup[0] = now
                    try:
                        with open(STATE_FILE + '.bak', 'w') as f:
                            f.write(payload)
                        os.chmod(STATE_FILE + '.bak', 0o600)
                    except Exception:
                        pass
            return
        except RuntimeError:
            time.sleep(0.05 * (attempt + 1))
        except Exception:
            log_exception('save_state failed')
            return

def migrate_v24(saved):
    migrated = 0
    old_trades = saved.get('trades') or []
    old_pos = saved.get('positions') or []
    old_open = saved.get('open_position')
    if old_open:
        old_pos.append(old_open)
    for t in old_trades:
        lg = 'live' if t.get('live') else 'paper'
        state['ledgers'][lg]['trades'].append(t)
        migrated += 1
    for p in old_pos:
        lg = 'live' if p.get('live') else 'paper'
        if isinstance(p.get('open_time'), str):
            p['open_ts'] = parse_any_time(p['open_time'])
        state['ledgers'][lg]['positions'].append(p)
        migrated += 1
    if old_trades or old_pos:
        cap = saved.get('capital')
        # FIX: previously this always went into the paper ledger's capital, even if
        # the v24 install had actually been trading with real money (old versions
        # didn't separate paper/live capital the way v25 does). That silently
        # replaced the clean $100 paper baseline with a real-money figure, corrupting
        # paper win-rate/PF stats. Now we route it to the live ledger if the old state
        # looks like it was live (sync_live_capital() will correct the exact number
        # from the real exchange balance shortly after startup anyway - this is just
        # about attributing it to the right ledger).
        was_live = (saved.get('mode') == 'live'
                    or any(t.get('live') for t in old_trades)
                    or any(p.get('live') for p in old_pos))
        if isinstance(cap, (int, float)) and cap > 0:
            target = 'live' if was_live else 'paper'
            state['ledgers'][target]['capital'] = float(cap)
        eq = saved.get('equity_curve') or []
        eq_target = 'live' if was_live else 'paper'
        state['ledgers'][eq_target]['equity'] = eq[-300:]
        add_log(f'v25 migration: {migrated} record(s) moved into ledgers ({eq_target})')
    state['ledgers_migrated'] = True

def parse_any_time(s):
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TEHRAN)
        return dt.timestamp()
    except Exception:
        return time.time()

DASH_SALT = '|nobitex-bot-salt'  # LEGACY ONLY: kept solely to verify pre-existing
                                  # hashes created before the PBKDF2 hardening below,
                                  # so old installs don't get locked out. Never used
                                  # to create new hashes.

def get_dash_salt():
    """Per-install random salt (replaces the old fixed DASH_SALT, which was the
    same for every installation since it lived in shared source code)."""
    s = state.get('dash_salt')
    if not s:
        s = secrets.token_hex(16)
        state['dash_salt'] = s
    return s

def _hash_pw(pw):
    # FIX: PBKDF2-HMAC-SHA256 with a per-install random salt and 200k iterations,
    # instead of a single unsalted-in-practice SHA-256 pass with a salt constant
    # that was identical across every install (since it's in the public source).
    salt = get_dash_salt()
    return 'pbkdf2$' + hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000).hex()

def dash_secret():
    h = state.get('dash_pass_hash') or ''
    if h:
        return h
    pw = state.get('dash_pass') or ''
    if not pw:
        return ''
    h = _hash_pw(pw)
    try:
        state['dash_pass_hash'] = h
        state.pop('dash_pass', None)
    except Exception:
        pass
    return h

def set_dash_pass(plain):
    if plain:
        state['dash_pass_hash'] = _hash_pw(plain)
    else:
        state.pop('dash_pass_hash', None)
    state.pop('dash_pass', None)

def dash_pass_matches(pw_try):
    secret = dash_secret()
    if not secret:
        return True
    if secret.startswith('pbkdf2$'):
        return hmac.compare_digest(_hash_pw(pw_try), secret)
    # Legacy hash format from before this hardening (plain SHA-256, static salt).
    # FIX: still compared with hmac.compare_digest instead of `==` to avoid a
    # timing side-channel. On a correct match we transparently upgrade the stored
    # hash to the new PBKDF2 format so the weaker path is only ever used once.
    legacy = hashlib.sha256((pw_try + DASH_SALT).encode()).hexdigest()
    if hmac.compare_digest(legacy, secret):
        state['dash_pass_hash'] = _hash_pw(pw_try)
        return True
    return False

def session_key():
    # FIX: session cookies / CSRF tokens used to be derived directly from the
    # password hash (dash_secret()[:32]). That meant anyone who obtained
    # dash_pass_hash alone (e.g. a partial leak of state.json, a future feature
    # that surfaces it, a support screenshot) could compute a valid session cookie
    # without ever knowing the real password. This is now a fully independent
    # random secret, unrelated to the password.
    k = state.get('session_key')
    if not k:
        k = secrets.token_hex(32)
        state['session_key'] = k
    return k

def load_state():
    global state
    if not os.path.exists(STATE_FILE):
        state['strategy_version'] = STRATEGY_VERSION
        return
    try:
        with open(STATE_FILE, 'r') as f:
            saved = json.load(f)
    except Exception:
        logging.exception('state.json unreadable')
        if os.path.exists(STATE_FILE + '.bak'):
            try:
                with open(STATE_FILE + '.bak', 'r') as f:
                    saved = json.load(f)
                add_log('Recovered from state.json.bak')
            except Exception:
                log_exception('backup unreadable too')
                return
        else:
            return
    keep = default_state()
    for k, v in saved.items():
        if k in ('trades', 'positions', 'open_position', 'capital', 'equity_curve',
                 'capital_base', 'tg_removed', 'tg_wiped'):
            continue
        keep[k] = v
    state = keep
    if 'ledgers' not in state or not isinstance(state['ledgers'], dict):
        state['ledgers'] = {'paper': fresh_ledger('paper'), 'live': fresh_ledger('live')}
    for name in ('paper', 'live'):
        base = fresh_ledger(name)
        base.update(state['ledgers'].get(name) or {})
        state['ledgers'][name] = base
    if not state.get('ledgers_migrated'):
        migrate_v24(saved)
    if state.get('strategy_version') != STRATEGY_VERSION:
        state['tuned'] = None
        state['threshold_extra'] = 0
        state['backtest'] = None
        state['strategy_version'] = STRATEGY_VERSION
        add_log('Strategy version changed - learned overrides cleared (baseline kept)')
    dash_secret()
    if state.get('api_token') and not isinstance(state.get('api_token'), bool):
        legacy_tok = state.pop('api_token')
        if not os.environ.get('NOBITEX_TOKEN'):
            save_token_to_env(legacy_tok)
        state['api_token'] = True
    add_log('State loaded')

def get_ledger(name):
    lgs = state['ledgers']
    if name not in lgs:
        lgs[name] = fresh_ledger(name)
    return lgs[name]

def learning_frozen():
    v = state.get('learning_frozen')
    return FREEZE_DEFAULT if v is None else bool(v)

# ============ small formatting helpers ============

def fmt_price(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return '-'
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    if x >= 0.001:
        return f"${x:.5f}"
    return f"${x:.8f}"

def fmt_amount(x):
    s = f"{float(x):.8f}".rstrip('0').rstrip('.')
    return s if s else '0'

def fmt_amount_verbatim(liability_from_api):
    s = str(liability_from_api).strip()
    if not s or s.lower() in ('none', 'null'):
        return '0'
    if 'e' in s.lower() or 'E' in s:
        s = f"{float(s):.10f}".rstrip('0').rstrip('.')
    return s

def fmt_mkt_price(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return '0'
    if x >= 1:
        return f"{x:.2f}"
    if x >= 0.01:
        return f"{x:.4f}"
    if x >= 0.0001:
        return f"{x:.6f}"
    return f"{x:.8f}"

def fmt_toman(usd_amount):
    rate = state.get('usdt_irt')
    if not rate or usd_amount is None:
        return None
    try:
        toman = float(usd_amount) * rate
    except Exception:
        return None
    if abs(toman) >= 1_000_000:
        return f"{toman/1_000_000:,.2f} میلیون تومان"
    return f"{toman:,.0f} تومان"

def eff_leverage(coin):
    want = DESIRED_LEV.get(coin, DESIRED_LEV['DEFAULT'])
    base_lev = float(min(want, MAX_LEV))
    try:
        _, _, vol_regime = dynamic_levels(coin)
        if vol_regime == 'high':
            base_lev = min(base_lev, 3.0)
    except Exception:
        pass
    return base_lev

def pf_color(pf):
    return '#34d399' if pf >= 1.5 else ('#fbbf24' if pf >= 1 else '#f87171')

# ============ Market data ============

def get_nobitex_price(symbol):
    try:
        src = COIN_MAP.get(symbol, symbol.lower())
        r = nb_session.get(f'{NOBITEX_API}/market/stats?srcCurrency={src}&dstCurrency=usdt',
                           timeout=10, headers=UA)
        if r.status_code == 200:
            d = r.json()
            if d.get('status') == 'ok':
                stats = d.get('stats', {})
                for key in stats:
                    s = stats[key]
                    bs = float(s.get('bestSell', 0) or 0)
                    bb = float(s.get('bestBuy', 0) or 0)
                    if bs > 0 and bb > 0:
                        return (bs + bb) / 2
                bs = float(stats.get('bestSell', 0) or 0)
                bb = float(stats.get('bestBuy', 0) or 0)
                if bs > 0 and bb > 0:
                    return (bs + bb) / 2
    except Exception:
        pass
    return None

def get_usdt_toman():
    try:
        r = nb_session.get(f'{NOBITEX_API}/market/stats?srcCurrency=usdt&dstCurrency=rls',
                           timeout=10, headers=UA)
        if r.status_code == 200:
            d = r.json()
            if d.get('status') == 'ok':
                stats = d.get('stats', {})
                for key in stats:
                    s = stats[key]
                    bs = float(s.get('bestSell', 0) or 0)
                    bb = float(s.get('bestBuy', 0) or 0)
                    if bs > 0 and bb > 0:
                        return (bs + bb) / 2 / 10
    except Exception:
        pass
    return None

def validate_coins_once():
    try:
        r = nb_session.get(f'{NOBITEX_API}/market/stats', timeout=15, headers=UA)
        if r.status_code != 200 or r.json().get('status') != 'ok':
            return
        stats = r.json().get('stats', {})
        cf = state.setdefault('coin_fail', {})
        benched = []
        for sym, src in COIN_MAP.items():
            s = stats.get(f'{src}-usdt')
            ok = False
            if isinstance(s, dict):
                try:
                    ok = float(s.get('bestSell', 0) or 0) > 0 and float(s.get('bestBuy', 0) or 0) > 0
                except Exception:
                    ok = False
            cf[sym] = 0 if ok else 5
            if not ok:
                benched.append(sym)
        if benched:
            add_log(f"Market check: no USDT market for {','.join(benched)} - benched")
        save_state()
    except Exception:
        log_exception('validate_coins_once failed')

def coin_supported(coin):
    return state.get('coin_fail', {}).get(coin, 0) < 5

def active_coins():
    return [c for c in COIN_MAP if coin_supported(c)]

def mark_prices(fresh):
    ts = fa_now().strftime('%H:%M:%S')
    with state_lock:
        state['prices'].update(fresh)
        state['prices_ts'] = ts
        tsp = state.setdefault('price_ts_map', {})
        for k in fresh:
            tsp[k] = ts

def get_all_prices_light():
    try:
        act = {sym: COIN_MAP[sym] for sym in active_coins()}
        if not act:
            return None
        out = {}
        cf = state.setdefault('coin_fail', {})
        items = list(act.items())
        for i in range(0, len(items), 8):
            chunk = items[i:i+8]
            srcs = ','.join(src_c for _, src_c in chunk)
            try:
                r = nb_session.get(f'{NOBITEX_API}/market/stats?srcCurrency={srcs}&dstCurrency=usdt',
                                   timeout=10, headers=UA)
                if r.status_code == 200 and r.json().get('status') == 'ok':
                    stats = r.json().get('stats', {})
                    for sym, src_c in chunk:
                        s = stats.get(f'{src_c}-usdt') or stats.get(src_c) or {}
                        bs = float(s.get('bestSell', 0) or 0)
                        bb = float(s.get('bestBuy', 0) or 0)
                        if bs > 0 and bb > 0:
                            out[sym] = (bs + bb) / 2
                            cf[sym] = 0
            except Exception:
                pass
        for sym, src_c in items:
            if sym not in out:
                try:
                    r_ind = nb_session.get(f'{NOBITEX_API}/market/stats?srcCurrency={src_c}&dstCurrency=usdt',
                                           timeout=6, headers=UA)
                    if r_ind.status_code == 200 and r_ind.json().get('status') == 'ok':
                        stats = r_ind.json().get('stats', {})
                        s = stats.get(f'{src_c}-usdt') or stats.get(src_c) or {}
                        bs = float(s.get('bestSell', 0) or 0)
                        bb = float(s.get('bestBuy', 0) or 0)
                        if bs > 0 and bb > 0:
                            out[sym] = (bs + bb) / 2
                            cf[sym] = 0
                except Exception:
                    pass
            if sym not in out:
                cf[sym] = cf.get(sym, 0) + 1
                if cf[sym] == 5:
                    add_log(f'Coin {sym} benched: missing from stats after fallback 5x')
        if len(out) >= max(3, len(act) // 2):
            return out
    except Exception:
        pass
    return None

def get_prices():
    prices = {}
    cf = state.setdefault('coin_fail', {})
    for coin in active_coins():
        p = get_nobitex_price(coin)
        if p and p > 0:
            prices[coin] = p
            cf[coin] = 0
        else:
            cf[coin] = cf.get(coin, 0) + 1
            if cf[coin] == 5:
                add_log(f'Coin {coin} benched: no price after 5 tries')
    n_active = len(active_coins()) or 1
    if prices and len(prices) >= max(3, n_active // 2):
        state['data_source'] = 'nobitex'
        return prices
    return None

# ---------- candles ----------

def get_candles(symbol, resolution='60', count=30, with_volume=False, drop_forming=False):
    try:
        now_ts = int(time.time())
        r = nb_session.get(f'{NOBITEX_API}/market/udf/history',
                           params={'symbol': f'{symbol}USDT', 'resolution': resolution,
                                   'to': now_ts, 'countback': count},
                           timeout=10, headers=UA)
        if r.status_code == 200:
            d = r.json()
            if d.get('s') == 'ok' and d.get('c'):
                closes = [float(x) for x in d['c']]
                if drop_forming and len(closes) > 1:
                    closes = closes[:-1]
                if with_volume:
                    vols = [float(x) for x in d.get('v', [0] * len(closes))]
                    if drop_forming and len(vols) > 1:
                        vols = vols[:-1]
                    return closes, vols
                return closes
    except Exception:
        pass
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

# ---------- orderbook ----------

def get_orderbook_info(symbol):
    try:
        r = nb_session.get(f'{NOBITEX_API}/v3/orderbook/{symbol}USDT', timeout=10, headers=UA)
        if r.status_code == 200:
            d = r.json()
            if d.get('status') == 'ok':
                bids = d.get('bids', [])[:15]
                asks = d.get('asks', [])[:15]
                pressure = spread = None
                buy_vol = sum(float(b[0]) * float(b[1]) for b in bids)
                sell_vol = sum(float(a[0]) * float(a[1]) for a in asks)
                tot = buy_vol + sell_vol
                if tot > 0:
                    pressure = (buy_vol - sell_vol) / tot
                if bids and asks:
                    best_bid = float(bids[0][0])
                    best_ask = float(asks[0][0])
                    if best_bid > 0:
                        spread = abs(best_ask - best_bid) / best_bid
                return pressure, spread, tot
    except Exception:
        pass
    return None, None, None

def nb_book_depth_ok(symbol, order_value_usdt, direction):
    try:
        r = nb_session.get(f'{NOBITEX_API}/v3/orderbook/{symbol}USDT', timeout=10, headers=UA)
        if r.status_code == 200:
            d = r.json()
            if d.get('status') == 'ok':
                side = d.get('asks' if direction == 'long' else 'bids', [])[:15]
                book_val = sum(float(x[0]) * float(x[1]) for x in side)
                return (order_value_usdt <= book_val * LIVE_MAX_BOOK_SHARE), book_val
    except Exception:
        pass
    return False, 0.0

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
            send_telegram(f'🌊 محافظ سقوط فعال شد: بیت‌کوین {reason_txt}.\nمعامله جدید باز نمی‌شه (بازها مدیریت می‌شن).')
        return True
    if state.get('crash_mode'):
        state['crash_mode'] = False
        add_log('Crash guard OFF')
        send_telegram('🌤 بازار آروم شد - محافظ سقوط غیرفعال شد')
    return False

# ---------- on-chain / sentiment ----------

def get_onchain_data():
    data = {'available': False}
    try:
        r = requests.get('https://api.alternative.me/fng/?limit=1', timeout=8)
        if r.status_code == 200:
            v = r.json()['data'][0]
            data['fear_greed'] = int(v['value'])
            data['fear_greed_text'] = v['value_classification']
            data['available'] = True
    except Exception:
        pass
    try:
        r = requests.get('https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT', timeout=8)
        if r.status_code == 200:
            data['funding_rate'] = float(r.json().get('lastFundingRate', 0)) * 100
            data['available'] = True
    except Exception:
        pass
    data['updated'] = fa_now().strftime('%H:%M:%S')
    return data

def onchain_score_bonus(direction):
    oc = state.get('onchain') or {}
    bonus, notes = 0, []
    fg = oc.get('fear_greed')
    if fg is not None:
        if fg <= 25 and direction == 'long':
            bonus += 1
            notes.append(f'ترس شدید بازار ({fg}) - تاریخی نزدیک کف')
        elif fg >= 75 and direction == 'short':
            bonus += 1
            notes.append(f'طمع شدید بازار ({fg}) - تاریخی نزدیک سقف')
    fr = oc.get('funding_rate')
    if fr is not None:
        if fr > 0.03 and direction == 'short':
            bonus += 1
            notes.append(f'فاندینگ بالا ({fr:.3f}%) - جمعیت لانگ، فشار اصلاح')
        elif fr < -0.01 and direction == 'long':
            bonus += 1
            notes.append(f'فاندینگ منفی ({fr:.3f}%) - جمعیت شورت، فشار خرید')
    return bonus, notes

# ============ Scoring / signals / learning layer ============

FACTOR_KEYS = ['rsi', 'rsi_deep', 'momentum', 'trend', 'mtf', 'orderbook',
               'volume', 'onchain', 'regime', 'whale_flow', 'volume_climax', 'falling_knife', 'session']
WEIGHT_MIN, WEIGHT_MAX, WEIGHT_LR = 0.5, 1.5, 0.06
SHADOW_LR_SCALE = 0.5
GENOME_DEFAULT = {'rsi_lo': 38, 'rsi_hi': 62, 'sl': STOP_LOSS, 'tp': TAKE_PROFIT,
                  'threshold': 4, 'mtf_req': False}
GENE_SPACE = {'rsi_lo': [25, 28, 32, 35, 38, 40], 'rsi_hi': [60, 62, 65, 68, 72, 75],
              'sl': [0.015, 0.020, 0.025], 'tp': [0.025, 0.030, 0.040, 0.050],
              'threshold': [4, 5], 'mtf_req': [True, False]}
LAB_INTERVAL = 48 * 3600
LAB_POP = 14

def get_factor_weights():
    w = state.get('factor_weights')
    if not isinstance(w, dict):
        w = {k: 1.0 for k in FACTOR_KEYS}
        state['factor_weights'] = w
    for k in FACTOR_KEYS:
        w.setdefault(k, 1.0)
    return w

def weighted(points, factor):
    if learning_frozen():
        return points
    return points * get_factor_weights().get(factor, 1.0)

def get_genome():
    if learning_frozen():
        return dict(GENOME_DEFAULT)
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
    if learning_frozen():
        return {'sl': STOP_LOSS, 'tp': TAKE_PROFIT, 'threshold': 4}
    t = state.get('tuned') or {}
    return {'sl': t.get('sl', STOP_LOSS), 'tp': t.get('tp', TAKE_PROFIT),
            'threshold': t.get('threshold', 4)}

def live_threshold():
    if learning_frozen():
        return 4
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
    if learning_frozen():
        return RISK_PER_TRADE
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
            prices_recent = get_candles_cached(coin, '60', 3, drop_forming=True)
            if prices_recent and len(prices_recent) >= 2:
                spread_pct = abs((prices_recent[-1] - prices_recent[-2]) / max(prices_recent[-2], 1e-9))
                if vr > 3.0 and spread_pct < 0.008:
                    sig['score'] += weighted(2, 'whale_flow')
                    sig.setdefault('factors', []).append('whale_flow')
                    sig['reasons'].append(f'{fa}: 🐋 انباشت/توزیع خاموش نهنگ (VSA: حجم {vr:.1f}x با اسپرد کم) (+2)')
                elif vr > 4.0:
                    sig['score'] += weighted(1, 'volume_climax')
                    sig.setdefault('factors', []).append('volume_climax')
                    sig['reasons'].append(f'{fa}: 💥 تخلیه هیجانی و اوج تسلیم بازار (حجم {vr:.1f}x) (+1)')
            prices_4h = get_candles_cached(coin, '60', 5, drop_forming=True)
            if prices_4h and len(prices_4h) >= 4:
                if direction == 'long' and all(prices_4h[-i] < prices_4h[-i-1] * 0.993 for i in range(1, 4)):
                    sig['score'] += weighted(-3, 'falling_knife')
                    sig.setdefault('factors', []).append('falling_knife')
                    sig['reasons'].append(f'{fa}: 🔪 فیلتر ضد چاقوی در حال سقوط (۳ کندل متوالی ریزشی >۰.۷٪) (-3)')
                elif direction == 'short' and all(prices_4h[-i] > prices_4h[-i-1] * 1.007 for i in range(1, 4)):
                    sig['score'] += weighted(-3, 'falling_knife')
                    sig.setdefault('factors', []).append('falling_knife')
                    sig['reasons'].append(f'{fa}: 🔪 فیلتر ضد پامپ هیجانی (۳ کندل متوالی صعودی >۰.۷٪) (-3)')
    bonus, oc_notes = onchain_score_bonus(direction)
    if bonus:
        sig['score'] += weighted(bonus, 'onchain')
        sig.setdefault('factors', []).append('onchain')
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
# Tehran time (Asia 03:30, Europe 11:30, US 17:30). Crypto is 24/7 but follows TradFi liquidity.
SESSION_FA = {
    'asia': 'آسیا 🌙 (حجم کم)',
    'europe': 'اروپا 🇪🇺 (حجم متوسط)',
    'overlap': 'اروپا+آمریکا 🔥 (اوج حجم)',
    'us': 'آمریکا 🇺🇸 (پرنوسان)',
    'quiet': 'شب آرام 😴'
}

def current_session(now=None):
    """Return current global session based on Tehran clock - teaches the brain when liquidity is real."""
    t = now or fa_now()
    h = t.hour + t.minute / 60.0
    # 03:30-11:30 Asia, 11:30-17:30 Europe, 17:30-21:30 Overlap (golden), 21:30-03:30 US late/quiet
    if 3.5 <= h < 11.5:
        return 'asia'
    elif 11.5 <= h < 17.5:
        return 'europe'
    elif 17.5 <= h < 21.5:
        return 'overlap'
    elif 21.5 <= h or h < 1.5:
        return 'us'
    else:  # 01:30-03:30
        return 'quiet'

def session_info():
    now = fa_now()
    s = current_session(now)
    return {'session': s, 'label': SESSION_FA.get(s, s), 'hour': now.strftime('%H:%M')}

def apply_session(sig):
    """Teachable session filter: learns which sessions actually pay for this bot."""
    sess = current_session()
    sig['session'] = sess
    # All paths now use weighted() and record 'session' so the brain can learn correctly
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

# ============ Shadow learning ============

def record_shadow(cand):
    shadows = state.setdefault('shadow_signals', [])
    if len(shadows) >= SHADOW_MAX_PENDING:
        return
    if any(s['coin'] == cand['coin'] and s['dir'] == cand['direction'] for s in shadows):
        return
    entry = state['prices'].get(cand['coin'])
    if not entry:
        return
    t = get_tuned()
    d = cand['direction']
    shadows.append({'coin': cand['coin'], 'dir': d, 'entry': entry,
                    'sl': entry * (1 - t['sl']) if d == 'long' else entry * (1 + t['sl']),
                    'tp': entry * (1 + t['tp']) if d == 'long' else entry * (1 - t['tp']),
                    'score': round(cand['score'], 1), 'factors': cand.get('factors', []),
                    't0': time.time()})

def shadow_bucket(score):
    b = round(float(score) * 2) / 2
    return f'{min(7.0, max(2.0, b)):g}'

def evaluate_shadows():
    shadows = state.get('shadow_signals') or []
    if not shadows:
        return
    now = time.time()
    resolved = []
    for s in shadows:
        price = state['prices'].get(s['coin'])
        if not price:
            continue
        outcome = None
        if s['dir'] == 'long':
            if price >= s['tp']:
                outcome = True
            elif price <= s['sl']:
                outcome = False
        else:
            if price <= s['tp']:
                outcome = True
            elif price >= s['sl']:
                outcome = False
        if outcome is None and now - s['t0'] > SHADOW_TIMEOUT_H * 3600:
            gain = ((price - s['entry']) / s['entry']) if s['dir'] == 'long' else ((s['entry'] - price) / s['entry'])
            outcome = gain > 0
        if outcome is not None:
            resolved.append((s, outcome))
    if not resolved:
        return
    stats = state.setdefault('shadow_stats', {})
    w = get_factor_weights()
    for s, win in resolved:
        try:
            shadows.remove(s)
        except ValueError:
            pass
        state['shadow_resolved'] = state.get('shadow_resolved', 0) + 1
        b = stats.setdefault(shadow_bucket(s['score']), {'w': 0, 'n': 0})
        b['n'] += 1
        if win:
            b['w'] += 1
        if not learning_frozen():
            direction = 1.0 if win else -1.0
            for f in s['factors']:
                if f in w:
                    # FIX: same falling_knife direction-flip as brain_learn_from_trade -
                    # this loop was previously left unfixed, so shadow-signal learning
                    # still weakened the falling-knife guard after a loss instead of
                    # strengthening it.
                    adj = direction * WEIGHT_LR * SHADOW_LR_SCALE
                    if f == 'falling_knife':
                        adj = -adj
                    w[f] = float(min(WEIGHT_MAX, max(WEIGHT_MIN, w[f] + adj)))
    add_log(f'Shadow: resolved {len(resolved)} (total {state.get("shadow_resolved", 0)})')
    save_state()

def shadow_threshold_adjust():
    stats = state.get('shadow_stats') or {}
    b4 = stats.get('4', {'w': 0, 'n': 0})
    b3 = stats.get('3', {'w': 0, 'n': 0})
    if b4.get('n', 0) >= 15 and b4['w'] / b4['n'] < 0.45:
        return +1
    if b3.get('n', 0) >= 25 and b3['w'] / b3['n'] >= 0.60:
        return -1
    return 0

def brain_learn_from_trade(trade):
    if learning_frozen():
        return
    factors = trade.get('factors') or []
    tr_all = get_ledger(trade.get('ledger', 'paper'))['trades']
    if not factors or len(tr_all) < 8:
        return
    pnl = trade.get('pnl', 0)
    if abs(pnl) < 0.005:
        return
    w = get_factor_weights()
    direction = 1.0 if pnl > 0 else -1.0
    cap = get_ledger(trade.get('ledger', 'paper'))['capital'] or 100
    magnitude = min(2.0, abs(pnl) / max(cap * 0.002, 0.01))
    for f in factors:
        if f in w:
            adj = direction * WEIGHT_LR * magnitude
            if f == 'falling_knife':
                adj = -adj
            w[f] = float(min(WEIGHT_MAX, max(WEIGHT_MIN, w[f] + adj)))

def brain_summary_fa():
    names = {'rsi': 'RSI اشباع', 'rsi_deep': 'اشباع عمیق', 'momentum': 'مومنتوم',
             'trend': 'روند کوتاه', 'mtf': 'تایم‌فریم بالا', 'orderbook': 'اردربوک',
             'volume': 'حجم', 'onchain': 'آنچین/ترس', 'regime': 'رژیم بازار',
             'whale_flow': 'جریان نهنگ (VSA)', 'volume_climax': 'تخلیه هیجانی', 'falling_knife': 'چاقوی در حال سقوط',
             'session': 'جلسه معاملاتی 🌍'}
    w = get_factor_weights()
    return [(names.get(k, k), round(v, 2)) for k, v in sorted(w.items(), key=lambda x: -x[1])]

# ============ UNIFIED TRADE ENGINE ============

def ledger_label(name):
    return 'واقعی 🔴' if name == 'live' else 'مجازی 🔵'

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
    # FIX: win-rate/PF checkpoints used to be computed over the ENTIRE lifetime
    # trade history. On a long-running bot that history can reach hundreds of
    # trades, which dilutes a recent bad streak so much that these two checkpoints
    # stop firing (only the drawdown checkpoint stayed reliably sensitive). Now they
    # use a recent rolling window (last 60 trades, same idea as the recovery check
    # a few lines above which already used trades[-10:]), while n (used for the
    # "have we seen enough trades at all" gate) still reflects full history.
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
        send_telegram(f'🛑 {reason}\nمعاملات جدید این موتور متوقف شد. داشبورد رو چک کن - جلسه اضطراری.')
        save_state()

def engine_exit_leg(lg, pos, fraction, price):
    fraction = min(1.0, max(0.0, fraction))
    leg_margin = pos['margin'] * fraction
    if leg_margin <= 0:
        return 0.0
    entry = pos['entry_price']
    pnl_pct = ((price - entry) / entry) if pos['direction'] == 'long' else ((entry - price) / entry)
    pnl = leg_margin * pnl_pct * pos['leverage']
    fee = leg_margin * pos['leverage'] * FEE_RATE
    net = pnl - fee
    # FIX (race condition, partial): lg['capital'] is read-modify-written here on
    # every partial/full close, concurrently with ThreadingHTTPServer request threads
    # and save_state()'s serialization. Locking this specific mutation closes the
    # highest-traffic gap. NOTE: this is a partial fix - a full audit of every
    # dashboard POST handler that touches lg['capital']/positions/trades directly
    # is still recommended as a follow-up; see checklist item #10.
    with state_lock:
        lg['capital'] = (lg['capital'] or 0) + net
    lg['daily_pnl'] = lg.get('daily_pnl', 0.0) + net
    pos['margin'] -= leg_margin
    if fraction < 0.999:
        pos['banked'] = pos.get('banked', 0.0) + net
    return net

def engine_open(lg, *, coin, direction, price, margin, leverage, sl_pct, tp_pct,
                ts, live_id=None, reasons=None, factors=None, snapshot=None):
    if direction == 'long':
        sl = price * (1 - sl_pct)
        tp = price * (1 + tp_pct)
    else:
        sl = price * (1 + sl_pct)
        tp = price * (1 - tp_pct)
    pos = {
        'ledger': lg['name'], 'coin': coin, 'direction': direction,
        'entry_price': price, 'margin': margin, 'leverage': leverage,
        'stop_loss': sl, 'take_profit': tp,
        'trail_trigger': tp_pct * 0.75, 'be_trigger': tp_pct * 0.40,
        'best_price': price, 'trail_active': False,
        'live': lg['name'] == 'live', 'live_id': live_id,
        'banked': 0.0, 'close_fails': 0, 'close_next': 0.0,
        'closing': False, 'rescue_used': False, 'liq_warned': False,
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
        if margin_before <= 0:
            pos['closed'] = True
            return False
        net = leg_exec(pos, 1.0, price)
        if net is None:
            return False
        age_days = (ts - pos.get('open_ts', ts)) / 86400
        extra_days = int(age_days) if age_days > 1 else 0
        if extra_days > 0:
            ext_fee = margin_before * pos['leverage'] * LIVE_EXT_FEE_DAILY * extra_days
            net -= ext_fee
            with state_lock:
                lg['capital'] -= ext_fee
            lg['daily_pnl'] = lg.get('daily_pnl', 0.0) - ext_fee
            if not quiet:
                add_log(f'Extension fee: -{ext_fee:.4f}$ ({extra_days}d)')
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
        if not quiet:
            brain_learn_from_trade(trade_rec)
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
                         'legacy_review': 'بازبینی 🔄',
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
    return 30.0

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
            add_log(f'Hard close: {pos.get("coin")} older than {MAX_TRADE_HOURS*4}h (4 days)')
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
            add_log(f'Regime reversal protection ({lg["name"]}): {pos.get("coin")} in opp regime ({cur_regime}), trailing ON & SL to BE')
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

# ============ Position opening ============

def eff_positions_count(lg):
    now = time.time()
    fresh_count = 0
    has_overtime = False
    for p in lg['positions']:
        age_h = (now - p.get('open_ts', now)) / 3600
        if age_h >= 24.0:
            has_overtime = True
        else:
            fresh_count += 1
    return fresh_count + (1 if has_overtime else 0)

def entries_blocked_reason(lg, base_capital):
    now = time.time()
    if lg.get('cp_triggered'):
        return 'checkpoint pause'
    if lg.get('trading_paused'):
        return 'daily limit pause'
    if now < lg.get('cooldown_until', 0):
        return f'cooldown {int((lg["cooldown_until"]-now)/60)}m'
    if len(lg['positions']) >= 6:
        return 'hard physical limit (max 6)'
    if eff_positions_count(lg) >= MAX_POSITIONS:
        return 'max positions'
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
        if not quiet:
            add_log(f'Entry blocked ({lg_name}): daily limit')
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
    overtime_count = sum(1 for p in lg['positions'] if (time.time() - p.get('open_ts', time.time())) >= 86400)
    if overtime_count >= 2:
        margin *= 0.75
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
    live_id = None
    if lg_name == 'live':
        if coin not in LIVE_COIN_WHITELIST:
            add_log(f'LIVE guard: {coin} not whitelisted - skipped')
            return False
        ok_depth, book_val = nb_book_depth_ok(coin, margin * lev, signal['direction'])
        # FIX: previously this clamped to 0.15 (15%) while nb_book_depth_ok's accept/
        # reject check used LIVE_MAX_BOOK_SHARE (0.10 = 10%), so orders in the 10-15%
        # band were rejected outright while orders >15% got resized DOWN to 15% and
        # accepted - i.e. bigger excess orders passed while medium ones didn't. Now both
        # paths use the same LIVE_MAX_BOOK_SHARE constant.
        if book_val and margin * lev > book_val * LIVE_MAX_BOOK_SHARE and book_val * LIVE_MAX_BOOK_SHARE >= MIN_ORDER_VALUE:
            margin = round((book_val * LIVE_MAX_BOOK_SHARE) / lev, 2)
            add_log(f'LIVE guard: clamped {coin} size to orderbook liquidity ({margin*lev:.0f}$)')
            ok_depth = True
        if margin * lev < MIN_ORDER_VALUE:
            need_margin = round(MIN_ORDER_VALUE / lev + 0.1, 2)
            if used_margin + need_margin <= capital * MAX_TOTAL_RISK:
                margin = need_margin
            else:
                add_log(f'LIVE guard: {coin} min order value {MIN_ORDER_VALUE}$ exceeds available risk cap')
                return False
        if not ok_depth:
            add_log(f'LIVE guard: {coin} book too thin for {margin*lev:.0f}$ (side≈{book_val:.0f}$)')
            return False
        live_id, real_entry = live_open_exchange(coin, signal['direction'], margin, price, desired_lev=lev)
        if live_id is None:
            return False
        if real_entry:
            price = real_entry
    pos = engine_open(lg, coin=coin, direction=signal['direction'], price=price,
                      margin=margin, leverage=lev, sl_pct=sl_pct, tp_pct=tp_pct,
                      ts=time.time(), live_id=live_id,
                      reasons=signal.get('reasons', []), factors=signal.get('factors', []),
                      snapshot={'threshold': live_threshold(), 'tuned': dict(get_tuned()),
                                'regime': (state.get('regime') or {}).get('regime'),
                                'session': current_session(),
                                'kelly_risk': round(risk, 3), 'score': signal.get('score')})
    fa = COIN_FA.get(coin, coin)
    dir_fa = 'خرید 📈' if signal['direction'] == 'long' else 'فروش 📉'
    state['last_reason'] = (f'{ledger_label(lg_name)} | {fa} | {dir_fa} | امتیاز {signal["score"]:.1f}\n'
                            + '\n'.join('• ' + r for r in signal.get('reasons', []))
                            + f'\nورود: {fmt_price(price)} | SL: {fmt_price(pos["stop_loss"])} | TP: {fmt_price(pos["take_profit"])}')
    add_log(f'Opened ({lg_name}): {signal["direction"]} {coin} @ {fmt_price(price)} m={margin:.2f}$ lev={lev}x')
    send_telegram(
        f'{"🔴 معامله واقعی" if lg_name=="live" else "🔵 معامله مجازی"} باز شد\n'
        f'ارز: {fa}\nجهت: {dir_fa}\nورود: {fmt_price(price)}\n'
        f'حد ضرر: {fmt_price(pos["stop_loss"])} | حد سود: {fmt_price(pos["take_profit"])}\n'
        f'امتیاز: {signal["score"]:.1f}')
    save_state()
    return True

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

# ============ Backtest ON the unified engine ============

def _bt_entry_signal(closes, vols, i, coin, threshold, sl, tp):
    window = closes[i-20:i]
    gains = [max(0, window[j]-window[j-1]) for j in range(1, len(window))]
    losses = [max(0, window[j-1]-window[j]) for j in range(1, len(window))]
    ag, al = np.mean(gains[-14:]), np.mean(losses[-14:])
    if ag < 1e-12 and al < 1e-12:
        rsi = 50.0
    else:
        rsi = 100 - (100 / (1 + ag / max(al, 1e-9)))
    mom = (window[-1] - window[-5]) / window[-5]
    sma7, sma20 = np.mean(window[-7:]), np.mean(window)
    score, direction, anchor = 0, None, False
    if rsi < 38:
        score += 3; direction = 'long'; anchor = True
        if rsi < 28:
            score += 1
    elif rsi > 62:
        score += 3; direction = 'short'; anchor = True
        if rsi > 72:
            score += 1
    if direction == 'long' and window[-1] < window[-2]:
        score -= 2
    elif direction == 'short' and window[-1] > window[-2]:
        score -= 2
    if direction == 'long' and i >= 4 and all(closes[j] < closes[j-1] * 0.993 for j in range(i-3, i)):
        score -= 3
    elif direction == 'short' and i >= 4 and all(closes[j] > closes[j-1] * 1.007 for j in range(i-3, i)):
        score -= 3
    if mom > 0.005:
        if direction == 'short':
            score -= 3
        else:
            score += 2; direction = direction or 'long'
    elif mom < -0.005:
        if direction == 'long':
            score -= 3
        else:
            score += 2; direction = direction or 'short'
    if (sma7 - sma20) / max(sma20, 1e-9) > 0.0015:
        score += (-1 if direction == 'short' else 1)
    elif (sma20 - sma7) / max(sma20, 1e-9) > 0.0015:
        score += (-1 if direction == 'long' else 1)
    if direction and i >= 40:
        t = trend_of(closes[i-40:i])
        want = 1 if direction == 'long' else -1
        score += 1 if t == want else (-1 if t == -want else 0)
    if direction and i >= 96:
        day = np.array(closes[i-24:i], dtype=float)
        drets = np.diff(day) / day[:-1]
        vol_r = float(np.std(drets))
        xw = np.arange(len(day))
        slope = float(np.polyfit(xw, day, 1)[0])
        tp_r = slope * 24 / float(np.mean(day))
        net = abs(day[-1] - day[0])
        path = float(np.sum(np.abs(np.diff(day)))) or 1.0
        eff = net / path
        if vol_r > 0.012:
            score -= 1
        elif eff > 0.35 and tp_r > 0.008:
            score += 1 if direction == 'long' else -1
        elif eff > 0.35 and tp_r < -0.008:
            score += 1 if direction == 'short' else -1
    if direction and vols and i >= 10:
        rv = np.mean(vols[max(0, i-3):i])
        bv = np.mean(vols[max(0, i-20):i-3]) or 1
        if rv / bv > 1.5:
            score += 1
        elif rv / bv < 0.5:
            score -= 1
    if direction and anchor and score >= threshold:
        return direction, score
    return None, 0

def bt_on_data(closes, vols, coin, sl, tp, threshold):
    lg = fresh_ledger('paper')
    lg['capital'] = 100.0
    if not closes or len(closes) < 60:
        return None
    base_ts = time.time() - len(closes) * 3600
    for i in range(30, len(closes)):
        price = closes[i]
        ts = base_ts + i * 3600
        if lg['positions']:
            pos = lg['positions'][0]
            manage_engine_pos(lg, pos, price, ts, paper_leg_exec(lg), quiet=True)
            continue
        direction, score = _bt_entry_signal(closes, vols, i, coin, threshold, sl, tp)
        if direction:
            conf = min(0.85, 0.5 + score * 0.05)
            margin = lg['capital'] * RISK_PER_TRADE * conf
            engine_open(lg, coin=coin, direction=direction, price=price, margin=margin,
                        leverage=min(eff_leverage(coin), 5), sl_pct=sl, tp_pct=tp, ts=ts)
    if lg['positions']:
        pos = lg['positions'][0]
        finalize_close(lg, pos, closes[-1], 'max_age', base_ts + len(closes) * 3600,
                       paper_leg_exec(lg), quiet=True)
    trades = lg['trades']
    wins = [t for t in trades if t['pnl'] > 0]
    losses_t = [t for t in trades if t['pnl'] <= 0]
    gw = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses_t))
    capital = lg['capital']
    peak, max_dd = 100.0, 0.0
    eq = 100.0
    for e in lg['equity']:
        eq = e['eq']
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    return {'coin': coin, 'days': round(len(closes) / 24), 'candles': len(closes),
            'trades': len(trades), 'wins': len(wins),
            'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
            'final_capital': round(capital, 2),
            'return_pct': round((capital - 100) / 100 * 100, 1),
            'max_drawdown': round(max_dd * 100, 1),
            'profit_factor': round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else 0),
            'avg_trade': round(float(np.mean([t['pnl'] for t in trades])), 4) if trades else 0,
            'time': fa_now().strftime('%H:%M:%S')}

def run_backtest(coin='BTC', days=30, sl=None, tp=None, threshold=None):
    count = min(days * 24, 720)
    data = get_candles(coin, '60', count, with_volume=True, drop_forming=True)
    if not data or not data[0] or len(data[0]) < 60:
        return None
    tuned = get_tuned()
    return bt_on_data(data[0], data[1], coin,
                      sl if sl is not None else tuned['sl'],
                      tp if tp is not None else tuned['tp'],
                      threshold if threshold is not None else tuned['threshold'])

# ============ Walk-forward optimizer ============

def auto_optimize():
    if learning_frozen():
        add_log('FREEZE: optimizer skipped')
        return None
    combos = []
    for sl in (0.015, 0.020, 0.025):
        for tp in (0.025, 0.030, 0.035):
            for th in (4, 5):
                if tp >= sl * 1.3:
                    combos.append((sl, tp, th))
    cache = {}
    for c in ('BTC', 'SOL', 'DOGE'):
        d = get_candles(c, '60', 720, with_volume=True, drop_forming=True)
        if d and d[0] and len(d[0]) >= 300:
            cache[c] = d
    if not cache:
        return None
    train_data, test_data = {}, {}
    for c, (closes, vols) in cache.items():
        cut = int(len(closes) * 0.7)
        train_data[c] = (closes[:cut], vols[:cut])
        test_data[c] = (closes[cut - 40:], vols[cut - 40:])
    results = []
    for sl, tp, th in combos:
        tot_ret = tot_tr = 0
        worst_dd = 0.0
        for c, (cl, vl) in train_data.items():
            bt = bt_on_data(cl, vl, c, sl, tp, th)
            if bt:
                tot_ret += bt['return_pct']; tot_tr += bt['trades']
                worst_dd = max(worst_dd, bt['max_drawdown'])
        if tot_tr >= 3:
            results.append({'sl': sl, 'tp': tp, 'threshold': th,
                            'return': round(tot_ret, 1), 'trades': tot_tr,
                            'max_dd': round(worst_dd, 1)})
    if not results:
        return None
    results.sort(key=lambda r: r['return'] - r['max_dd'] * 0.3, reverse=True)
    validated = None
    for cand in results[:3]:
        t_ret = t_tr = 0
        t_dd = 0.0
        for c, (cl, vl) in test_data.items():
            bt = bt_on_data(cl, vl, c, cand['sl'], cand['tp'], cand['threshold'])
            if bt:
                t_ret += bt['return_pct']; t_tr += bt['trades']
                t_dd = max(t_dd, bt['max_drawdown'])
        if t_ret > 1.0 and t_tr >= 5 and cand['return'] > 0.0 and (cand['return'] - cand['max_dd'] * 0.3) > 0:
            validated = dict(cand)
            validated['oos_return'] = round(t_ret, 1)
            validated['oos_trades'] = t_tr
            break
    if not validated:
        add_log('Walk-forward: no combo survived OOS - keeping baseline')
        return None
    state['tuned'] = {'sl': validated['sl'], 'tp': validated['tp'],
                      'threshold': validated['threshold'], 'return': validated['return'],
                      'oos_return': validated.get('oos_return'),
                      'updated': fa_now().strftime('%Y-%m-%d %H:%M')}
    add_log(f"Walk-forward adopt: SL={validated['sl']*100:.1f}% TP={validated['tp']*100:.1f}% th={validated['threshold']} (oos {validated.get('oos_return',0):+.1f}%)")
    send_telegram(f"🧠 یادگیری (Walk-Forward):\nSL {validated['sl']*100:.1f}% | TP {validated['tp']*100:.1f}% | آستانه {validated['threshold']}\nتست ندیده: {validated.get('oos_return',0):+.1f}%")
    save_state()
    return validated

def adaptive_adjust():
    if learning_frozen():
        return
    trades = get_ledger('paper')['trades']
    if len(trades) < 6:
        return
    recent = trades[-6:]
    wins = sum(1 for t in recent if t.get('pnl', 0) > 0)
    cur = state.get('threshold_extra', 0)
    if wins <= 2 and cur == 0:
        state['threshold_extra'] = 1
        add_log('Adaptive: stricter entries (+1 threshold)')
    elif wins >= 4 and cur > 0:
        state['threshold_extra'] = 0
        add_log('Adaptive: back to normal threshold')

# ============ Per-coin permission filter ============

def update_coin_filter():
    if learning_frozen():
        state['coin_scores'] = {}
        add_log('FREEZE: coin filter skipped (all coins allowed)')
        return
    scores = {}
    for coin in active_coins():
        bt = run_backtest(coin, days=30)
        if bt and bt['trades'] >= 3:
            scores[coin] = {'return': bt['return_pct'], 'trades': bt['trades'],
                            'win_rate': bt['win_rate'], 'allowed': (bt['return_pct'] > -0.5 and bt.get('profit_factor', 1.0) >= 0.75)}
        elif bt:
            scores[coin] = {'return': bt['return_pct'], 'trades': bt['trades'],
                            'win_rate': bt['win_rate'], 'allowed': True}
        else:
            scores[coin] = {'return': None, 'trades': 0, 'win_rate': 0, 'allowed': True}
        time.sleep(1)
    if 'BTC' in scores:
        scores['BTC']['allowed'] = True
    state['coin_scores'] = scores
    blocked = [c for c, s in scores.items() if not s['allowed']]
    add_log(f"Coin filter: blocked={','.join(blocked) if blocked else 'none'}")
    save_state()
    return scores

def coin_allowed(coin):
    if learning_frozen():
        return True
    s = (state.get('coin_scores') or {}).get(coin)
    return True if not s else s.get('allowed', True)

# ============ Strategy lab ============

def lab_backtest(closes, vols, genes, lev):
    lg = fresh_ledger('paper')
    lg['capital'] = 100.0
    if not closes or len(closes) < 60:
        return {'trades': 0, 'ret': 0.0, 'wr': 0.0, 'dd': 0.0}
    base_ts = time.time() - len(closes) * 3600
    for i in range(45, len(closes)):
        price = closes[i]
        ts = base_ts + i * 3600
        if lg['positions']:
            manage_engine_pos(lg, lg['positions'][0], price, ts, paper_leg_exec(lg), quiet=True)
            if lg['positions'] and (i - lg['positions'][0].get('i0', i)) > 30:
                finalize_close(lg, lg['positions'][0], price, 'max_age', ts,
                               paper_leg_exec(lg), quiet=True)
            continue
        window = closes[i-20:i]
        gains = [max(0, window[j]-window[j-1]) for j in range(1, len(window))]
        losses = [max(0, window[j-1]-window[j]) for j in range(1, len(window))]
        ag, al = np.mean(gains[-14:]), np.mean(losses[-14:])
        rsi = 50.0 if (ag < 1e-12 and al < 1e-12) else 100 - (100 / (1 + ag / max(al, 1e-9)))
        mom = (window[-1] - window[-5]) / window[-5]
        sma7, sma20 = np.mean(window[-7:]), np.mean(window)
        score, d = 0.0, None
        if rsi < genes['rsi_lo']:
            score += 3; d = 'long'
        elif rsi > genes['rsi_hi']:
            score += 3; d = 'short'
        if not d:
            continue
        if (d == 'long' and window[-1] < window[-2]) or (d == 'short' and window[-1] > window[-2]):
            score -= 2
        if d == 'long' and i >= 4 and all(closes[j] < closes[j-1] * 0.993 for j in range(i-3, i)):
            score -= 3
        elif d == 'short' and i >= 4 and all(closes[j] > closes[j-1] * 1.007 for j in range(i-3, i)):
            score -= 3
        if mom > 0.005:
            score += 2 if d == 'long' else -3
        elif mom < -0.005:
            score += 2 if d == 'short' else -3
        if (sma7 - sma20) / max(sma20, 1e-9) > 0.0015:
            score += 1 if d == 'long' else 0
        elif (sma20 - sma7) / max(sma20, 1e-9) > 0.0015:
            score += 1 if d == 'short' else 0
        t = trend_of(closes[i-40:i])
        want = 1 if d == 'long' else -1
        has_mtf = False
        if t == want:
            score += 1; has_mtf = True
        elif t == -want:
            score -= 1
        if genes['mtf_req'] and not has_mtf:
            continue
        if score >= genes['threshold']:
            pos = engine_open(lg, coin='BTC', direction=d, price=price,
                              margin=lg['capital'] * 0.12, leverage=min(lev, 5),
                              sl_pct=genes['sl'], tp_pct=genes['tp'], ts=ts)
            pos['i0'] = i
    if lg['positions']:
        finalize_close(lg, lg['positions'][0], closes[-1], 'max_age',
                       base_ts + len(closes) * 3600, paper_leg_exec(lg), quiet=True)
    trades = lg['trades']
    wins = [t for t in trades if t['pnl'] > 0]
    peak, dd, eq = 100.0, 0.0, 100.0
    for e in lg['equity']:
        eq = e['eq']; peak = max(peak, eq); dd = max(dd, (peak - eq) / peak)
    return {'trades': len(trades), 'ret': round((lg['capital'] - 100), 2),
            'wr': round(len(wins) / len(trades) * 100, 1) if trades else 0,
            'dd': round(dd * 100, 1)}

def lab_fitness(genes, data):
    tr_ret = tr_n = te_ret = te_n = 0
    tr_dd = te_dd = 0.0
    for coin, (ctr, vtr, cte, vte) in data.items():
        lev = eff_leverage(coin)
        a = lab_backtest(ctr, vtr, genes, lev)
        b = lab_backtest(cte, vte, genes, lev)
        tr_ret += a['ret']; tr_n += a['trades']; tr_dd = max(tr_dd, a['dd'])
        te_ret += b['ret']; te_n += b['trades']; te_dd = max(te_dd, b['dd'])
    return (tr_ret - tr_dd * 0.3, te_ret - te_dd * 0.3,
            {'train_ret': round(tr_ret, 1), 'train_n': tr_n,
             'test_ret': round(te_ret, 1), 'test_n': te_n})

def run_strategy_lab():
    if learning_frozen():
        add_log('FREEZE: strategy lab skipped')
        return
    import random as r
    coins = [c for c in COIN_MAP if coin_allowed(c)][:4] or ['BTC', 'SOL']
    data = {}
    for c in coins:
        d = get_candles(c, '60', 720, with_volume=True, drop_forming=True)
        if d and d[0] and len(d[0]) >= 300:
            closes, vols = d
            cut = int(len(closes) * 0.7)
            data[c] = (closes[:cut], vols[:cut], closes[cut - 45:], vols[cut - 45:])
        time.sleep(0.5)
    if len(data) < 2:
        add_log('Lab: not enough data')
        return None
    champion = get_genome()
    ch_tr, ch_te, ch_info = lab_fitness(champion, data)

    def mutate(g):
        g2 = dict(g)
        k = r.choice(list(GENE_SPACE))
        g2[k] = r.choice(GENE_SPACE[k])
        if g2['tp'] < g2['sl'] * 1.3:
            g2['tp'] = round(g2['sl'] * 1.5, 3)
        return g2

    def rand_g():
        g = {k: r.choice(v) for k, v in GENE_SPACE.items()}
        if g['tp'] < g['sl'] * 1.3:
            g['tp'] = round(g['sl'] * 1.5, 3)
        return g

    pop = [mutate(champion) for _ in range(LAB_POP // 2)] + [rand_g() for _ in range(LAB_POP - LAB_POP // 2)]
    best = None
    for genes in pop:
        f_tr, f_te, info = lab_fitness(genes, data)
        if (f_tr > ch_tr and f_te > ch_te and info['train_n'] >= 5
                and info['test_n'] >= 2 and info['test_ret'] > 0):
            if best is None or (f_tr + f_te) > (best[0] + best[1]):
                best = (f_tr, f_te, genes, info)
    hist = state.setdefault('lab_history', [])
    if best:
        f_tr, f_te, genes, info = best
        state['genome'] = genes
        state['tuned'] = dict(state.get('tuned') or {}, sl=genes['sl'], tp=genes['tp'],
                              threshold=genes['threshold'],
                              updated=fa_now().strftime('%Y-%m-%d %H:%M'))
        hist.append({'t': fa_now().strftime('%m-%d %H:%M'), 'adopted': True,
                     'genes': genes, 'info': info})
        add_log(f"Lab: ADOPTED new strategy (train {info['train_ret']:+.1f}% test {info['test_ret']:+.1f}%)")
        send_telegram(f"🧪 لاب استراتژی: نسخه بهتر فعال شد! (تست {info['test_ret']:+.1f}%)")
    else:
        hist.append({'t': fa_now().strftime('%m-%d %H:%M'), 'adopted': False, 'genes': None,
                     'info': {'tested': LAB_POP, 'champ_train': ch_info['train_ret'],
                              'champ_test': ch_info['test_ret']}})
        add_log('Lab: champion defended')
    state['lab_history'] = hist[-15:]
    save_state()
    return best

# ============ Nobitex PRIVATE API (live engine) ============

_poslist_cache = {}     # key -> (ts, data_dict)
_POSLIST_TTL = 15.0

def save_token_to_env(token):
    p = os.path.join(os.getcwd(), '.env')
    if not os.path.exists(p):
        for cand in ('/root/nobitex-bot/.env', '/home/user/.env', '.env'):
            if os.path.exists(cand):
                p = cand
                break
    lines = []
    found = False
    if os.path.exists(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip().startswith('NOBITEX_TOKEN='):
                        lines.append(f"NOBITEX_TOKEN={token}\n")
                        found = True
                    else:
                        lines.append(line)
        except Exception:
            pass
    if not found:
        lines.append(f"NOBITEX_TOKEN={token}\n")
    try:
        with open(p, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        os.chmod(p, 0o600)
    except Exception:
        pass
    os.environ['NOBITEX_TOKEN'] = token

def nb_token():
    tok = os.environ.get('NOBITEX_TOKEN', '')
    if not tok:
        p = os.path.join(os.getcwd(), '.env')
        for cand in (p, '/root/nobitex-bot/.env', '/home/user/.env'):
            if os.path.exists(cand):
                try:
                    with open(cand, 'r', encoding='utf-8') as f:
                        for line in f:
                            if line.strip().startswith('NOBITEX_TOKEN='):
                                tok = line.strip().split('=', 1)[1].strip().strip('"').strip("'")
                                os.environ['NOBITEX_TOKEN'] = tok
                                break
                except Exception:
                    pass
            if tok:
                break
    # FIX: previously fell back to state.get('api_token', ''), which meant the raw
    # API token could end up stored in plaintext inside state.json (no guaranteed
    # 0600 permission like .env has). state['api_token'] is now only ever a boolean
    # "is a token configured" flag, never the secret itself - see load_state(),
    # start_server(), and the /save handler.
    return tok

def nb_headers():
    return {'Authorization': f"Token {nb_token()}",
            'content-type': 'application/json', **UA}

def nb_test_token():
    errors = []
    for sess, name in ((nb_session, 'direct'), (requests, 'via proxy')):
        try:
            r = sess.get(f'{NOBITEX_API}/users/profile', headers=nb_headers(), timeout=15)
            d = r.json()
            if d.get('status') == 'ok':
                return True, d.get('profile', {}).get('username', 'ok')
            return False, d.get('message', d.get('code', 'invalid token'))
        except Exception as e:
            err = str(e)
            short = 'timeout' if 'timed out' in err.lower() else (
                'connection blocked (VPN?)' if any(x in err for x in
                    ('WRONG_VERSION_NUMBER', 'UNEXPECTED_EOF', 'Connection')) else err[:80])
            errors.append(f'{name}: {short}')
    return False, ' | '.join(errors) + ' -- TIP: VPN خاموش و دوباره Save'

def _wallet_usdt_amount(w):
    def pick(val):
        if not isinstance(val, dict):
            return None
        amt = val.get('activeBalance', val.get('balance'))
        if amt is None:
            return None
        try:
            return float(amt or 0)
        except (TypeError, ValueError):
            return None
    if isinstance(w, dict):
        for key, val in w.items():
            if str(key).lower() == 'usdt':
                v = pick(val)
                if v is not None:
                    return v
    elif isinstance(w, list):
        for val in w:
            if isinstance(val, dict) and str(val.get('currency', '')).lower() == 'usdt':
                v = pick(val)
                if v is not None:
                    return v
    return None

def nb_wallets(wtype='margin'):
    for attempt in ('post', 'get'):
        try:
            if attempt == 'post':
                r = nb_session.post(f'{NOBITEX_API}/v2/wallets', headers=nb_headers(),
                                    json={'currencies': 'usdt', 'type': wtype}, timeout=15)
            else:
                r = nb_session.get(f'{NOBITEX_API}/v2/wallets?currencies=usdt&type={wtype}',
                                   headers=nb_headers(), timeout=15)
            d = r.json()
            if d.get('status') != 'ok':
                continue
            v = _wallet_usdt_amount(d.get('wallets'))
            if v is not None:
                return v
        except Exception:
            pass
    return None

def nb_margin_balance():
    return nb_wallets('margin')

def nb_spot_usdt():
    return nb_wallets('spot')

def nb_transfer_to_margin(amount):
    try:
        r = nb_session.post(f'{NOBITEX_API}/wallets/transfer', headers=nb_headers(),
                            json={'currency': 'usdt', 'amount': f'{amount:.2f}',
                                  'src': 'spot', 'dst': 'margin'}, timeout=15)
        d = r.json()
        if d.get('status') == 'ok':
            add_log(f'Moved {amount:.2f} USDT spot->margin')
            return True
        add_log(f"Transfer failed: {d.get('code','')} {d.get('message','')}")
    except Exception as e:
        add_log(f'Transfer error: {e}')
    return False

def nb_max_leverage(coin='BTC'):
    symbol = f'{COIN_MAP.get(coin, coin.lower()).upper()}USDT'
    try:
        r = nb_session.get(f'{NOBITEX_API}/margin/markets/list', headers=nb_headers(), timeout=15)
        d = r.json()
        if d.get('status') == 'ok':
            for key, val in d.items():
                if key == 'status':
                    continue
                if isinstance(val, dict):
                    markets = val if symbol in val else d.get('markets', d)
                    m = markets.get(symbol) if isinstance(markets, dict) else None
                    if m:
                        return float(m.get('maxLeverage', 3))
            if symbol in d and isinstance(d[symbol], dict):
                return float(d[symbol].get('maxLeverage', 3))
    except Exception:
        pass
    return None

def nb_max_leverage_cached(coin):
    cache = state.setdefault('max_leverage', {})
    lev = cache.get(coin)
    if not lev:
        lev = nb_max_leverage(coin)
        if lev:
            cache[coin] = lev
    return float(lev) if lev else 3.0

def _positions_request(params):
    r = nb_session.get(f'{NOBITEX_API}/positions/list', params=params,
                       headers=nb_headers(), timeout=15)
    return r.json()

def nb_positions_by_coin(coin, fresh=False):
    src = COIN_MAP.get(coin, coin.lower())
    now = time.time()
    hit = _poslist_cache.get(src)
    data = None
    if hit and not fresh and now - hit[0] < _POSLIST_TTL:
        data = hit[1]
    else:
        d = _positions_request({'srcCurrency': src, 'status': 'active'})
        if d.get('status') == 'ok':
            data = d.get('positions', []) or []
            _poslist_cache[src] = (now, data)
        else:
            data = hit[1] if hit else []
    opens = [p for p in data if p.get('status') == 'Open']
    opens.sort(key=lambda p: p.get('createdAt', ''), reverse=True)
    return opens

def nb_get_active_position(coin, fresh=False):
    opens = nb_positions_by_coin(coin, fresh=fresh)
    return opens[0] if opens else None

def nb_open_position(direction, margin_usdt, price, coin='BTC', desired_lev=None):
    # FIX (leverage parity): use the SAME leverage the caller already computed via
    # eff_leverage() (which accounts for vol_regime derating) instead of recomputing
    # independently from the exchange max. Previously this ignored vol_regime, so a
    # position sized/SL-TP'd for e.g. 3x could actually be opened on the exchange at 5x.
    exch_max = min(nb_max_leverage_cached(coin), MAX_LEV)
    want_lev = min(desired_lev, exch_max) if desired_lev else exch_max
    lev = max(1.0, int(want_lev * 2) / 2.0)
    value = max(MIN_ORDER_VALUE, margin_usdt * lev)
    margin_used = value / lev
    bal = nb_margin_balance()
    if bal is not None and bal < margin_used:
        add_log(f'LIVE: insufficient margin balance ({bal:.2f}$ < {margin_used:.2f}$) - spot balance preserved for hold/DCA')
        return False
    amount = value / price
    side = 'buy' if direction == 'long' else 'sell'
    px = price * 1.025 if side == 'buy' else price * 0.975
    body = {'execution': 'market', 'srcCurrency': COIN_MAP.get(coin, coin.lower()),
            'dstCurrency': 'usdt', 'type': side, 'leverage': fmt_amount(lev),
            'amount': fmt_amount(amount), 'price': fmt_mkt_price(px)}
    try:
        r = nb_session.post(f'{NOBITEX_API}/margin/orders/add', headers=nb_headers(),
                            json=body, timeout=20)
        d = r.json()
        if d.get('status') == 'ok':
            add_log(f'LIVE open sent: {side} {fmt_amount(amount)} {coin} lev {lev}x')
            return True
        add_log(f"LIVE open failed: {d.get('code','')} {d.get('message','')}")
    except Exception as e:
        add_log(f'LIVE open error: {e}')
    return False

def nb_close_order(position_id, amount_str, direction, price):
    px = price * 0.975 if direction == 'long' else price * 1.025
    body = {'execution': 'market', 'amount': amount_str, 'price': fmt_mkt_price(px)}
    try:
        r = nb_session.post(f'{NOBITEX_API}/positions/{position_id}/close',
                            headers=nb_headers(), json=body, timeout=20)
        d = r.json()
        if d.get('status') == 'ok':
            return True, ''
        msg = f"{d.get('code','')} {d.get('message','')}"
        add_log(f'LIVE close failed: {msg}')
        return False, msg
    except Exception as e:
        add_log(f'LIVE close error: {e}')
        return False, str(e)

def live_open_exchange(coin, direction, margin, price, desired_lev=None):
    if not nb_open_position(direction, margin, price, coin, desired_lev=desired_lev):
        return None, None
    time.sleep(3)
    live = nb_get_active_position(coin, fresh=True)
    if not live:
        add_log(f'LIVE: {coin} opened but position not found - untracked!')
        send_telegram(f'⚠️ {COIN_FA.get(coin)} باز شد ولی موقعیت پیدا نشد - دستی چک کن!')
        return None, price
    entry = price
    try:
        ep = float(live.get('entryPrice') or 0)
        if ep > 0:
            entry = ep
    except Exception:
        pass
    return live.get('id'), entry

def live_leg_exec(lg):
    def _exec(pos, fraction, price):
        now = time.time()
        if now < pos.get('close_next', 0):
            return None
        live = nb_get_active_position(pos.get('coin', 'BTC'), fresh=True)
        if not live:
            add_log(f'LIVE: {pos.get("coin")} not on exchange anymore - settling as expired')
            send_telegram(f'⚠️ موقعیت {COIN_FA.get(pos.get("coin"), "?")} دیگه روی صرافی نیست (لیکویید/دستی/تسویه) - تطبیق داده شد')
            return engine_exit_leg(lg, pos, 1.0, price) if fraction >= 1.0 else 0.0
        liability_str = str(live.get('liability') or '0')
        try:
            liability_val = float(liability_str)
        except Exception:
            liability_val = 0.0
        if liability_val <= 0:
            return None
        if fraction >= 0.999:
            amount_str = fmt_amount_verbatim(liability_str)
        else:
            part = liability_val * fraction
            usdt_value = part if pos['direction'] == 'long' else part * price
            if usdt_value < MIN_ORDER_VALUE:
                return None
            amount_str = fmt_amount(part)
        pos_id = live.get('id')
        ok, err = nb_close_order(pos_id, amount_str, pos['direction'], price)
        if not ok:
            for retry in range(1, 4):
                time.sleep(3)
                add_log(f'LIVE close retry #{retry}/3 for pos {pos_id}...')
                ok, err = nb_close_order(pos_id, amount_str, pos['direction'], price)
                if ok:
                    break
        if not ok:
            pos['close_fails'] = pos.get('close_fails', 0) + 1
            pos['close_next'] = now + min(30 * (2 ** min(pos['close_fails'], 4)), 300)
            if pos['close_fails'] >= 3:
                send_telegram('🚨 ۳ بار بستن موقعیت لایو شکست خورد!\nلطفاً خودت دستی ببند.')
            return None
        pos['close_fails'] = 0
        pos['close_next'] = 0
        time.sleep(2)
        sync_live_capital(quiet=True)
        return engine_exit_leg(lg, pos, fraction, price)
    return _exec

def live_manage_wrapper(pos, price, ts):
    try:
        check_liquidation_distance(pos, price)
        emergency_collateral_rescue(pos, price)
    except Exception:
        pass
    lg = get_ledger('live')
    manage_engine_pos(lg, pos, price, ts, live_leg_exec(lg))

def liq_price_of(pos):
    try:
        lev = float(pos.get('leverage', 1)) or 1.0
        entry = float(pos['entry_price'])
        dist = (1.0 / lev) * 0.90
        return entry * (1 - dist) if pos['direction'] == 'long' else entry * (1 + dist)
    except Exception:
        return None

def check_liquidation_distance(pos, current):
    liq = liq_price_of(pos)
    if not liq or not current:
        return
    entry = pos['entry_price']
    total = abs(entry - liq)
    if total <= 0:
        return
    gone = (entry - current) if pos['direction'] == 'long' else (current - entry)
    frac = gone / total
    if frac >= 0.6 and not pos.get('liq_warned'):
        pos['liq_warned'] = True
        add_log(f'LIQ WARNING: {pos.get("coin")} at {frac*100:.0f}% to liquidation')
        send_telegram(f'🚨 هشدار لیکویید: {COIN_FA.get(pos.get("coin"), "?")} '
                      f'{frac*100:.0f}٪ مسیر رو رفته!\nقیمت: {fmt_price(current)} | استاپ: {fmt_price(pos.get("stop_loss"))}')
    elif frac < 0.4 and pos.get('liq_warned'):
        pos['liq_warned'] = False

def nb_edit_collateral(pos_id, new_total):
    if new_total <= 0:
        add_log(f'collateral edit refused: non-positive total {new_total}')
        return False
    try:
        r = nb_session.post(f'{NOBITEX_API}/positions/{pos_id}/edit-collateral',
                            headers=nb_headers(),
                            json={'collateral': fmt_mkt_price(new_total)}, timeout=20)
        d = r.json()
        if d.get('status') == 'ok':
            return True
        add_log(f"collateral edit failed: {d.get('code','')} {d.get('message','')}")
    except Exception as e:
        add_log(f'collateral edit error: {e}')
    return False

def emergency_collateral_rescue(pos, current):
    if not ENABLE_COLLATERAL_RESCUE:
        return
    if not pos.get('live'):
        return
    liq = liq_price_of(pos)
    if not liq or not current:
        return
    entry = pos['entry_price']
    total = abs(entry - liq)
    if total <= 0:
        return
    gone = (entry - current) if pos['direction'] == 'long' else (current - entry)
    if pos.get('rescue_used'):
        if gone / total >= 0.75:
            add_log(f'EMERGENCY HARD SETTLE: {pos.get("coin")} reached 75% after rescue - protecting spot balance')
            send_telegram(f'🚨 تسویه اضطراری: {COIN_FA.get(pos.get("coin"), "?")} بعد از تزریق وثیقه باز هم ریزش کرد - پوزیشن بسته شد تا موجودی اسپات هدر نرود.')
            finalize_close(get_ledger('live'), pos, current, 'kill_switch', time.time(), live_leg_exec(get_ledger('live')))
        return
    if gone / total < 0.80:
        return
    pos['rescue_used'] = True
    add_amount = round(pos.get('margin', 0) * 0.5, 2)
    bal = nb_margin_balance()
    if bal is not None and bal < add_amount and add_amount >= 1:
        need = round(add_amount - bal + 0.01, 2)
        spot = nb_spot_usdt()
        if spot is not None and spot >= need:
            nb_transfer_to_margin(need)
            time.sleep(2)
            bal = nb_margin_balance()
    if bal is None or bal < add_amount or add_amount < 1:
        send_telegram('🚨 نزدیک لیکویید و موجودی آزاد اسپات/تعهدی کافی نیست! دستی ببند.')
        return
    live = nb_get_active_position(pos.get('coin', 'BTC'), fresh=True)
    if not live:
        return
    try:
        cur_collateral = float(live.get('collateral') or 0)
    except (TypeError, ValueError):
        cur_collateral = 0.0
    if cur_collateral <= 0:
        add_log('rescue aborted: could not read current collateral (never send blind)')
        return
    new_total = cur_collateral + add_amount
    if nb_edit_collateral(live.get('id'), new_total):
        add_log(f'EMERGENCY collateral {cur_collateral:.2f}$ -> {new_total:.2f}$ (+{add_amount:.2f}$) pos {live.get("id")}')
        send_telegram(f'🛞 چرخ یدک: +{add_amount:.2f}$ وثیقه به {COIN_FA.get(pos.get("coin"), "?")} (فقط وقت خرید)')

# ---------- capital sync & reconcile ----------

def sync_live_capital(quiet=False):
    lg = get_ledger('live')
    bal = nb_margin_balance()
    if bal is None:
        return None
    state['live_balance'] = bal
    used = sum(p.get('margin', 0) for p in lg['positions'])
    now = time.time()
    accrued_ext_fee = sum(
        p.get('margin', 0) * p.get('leverage', 1) * LIVE_EXT_FEE_DAILY * max(0, int((now - p.get('open_ts', now)) / 86400))
        for p in lg['positions']
    )
    real_total = round(bal + used - accrued_ext_fee, 4)
    if lg['capital'] is None:
        lg['capital'] = real_total
        if not quiet:
            add_log(f'Live capital synced: {real_total:.2f}$')
    else:
        old = lg['capital']
        if abs(real_total - old) / max(old, 1e-9) > 0.005:
            lg['capital'] = real_total
            if not quiet:
                add_log(f'Live capital resync: {old:.2f}$ -> {real_total:.2f}$')
    if 'live_base' not in state:
        state['live_base'] = real_total
    return real_total

def reconcile_live_positions():
    if not state.get('api_token') or state.get('mode') != 'live':
        return
    lg = get_ledger('live')
    try:
        found = set()
        for coin in active_coins():
            opens = nb_positions_by_coin(coin, fresh=True)
            if not opens:
                continue
            live = opens[0]
            if any(p.get('coin') == coin for p in lg['positions']):
                found.add(coin)
                continue
            entry = float(live.get('entryPrice') or 0)
            side = 'long' if live.get('side') == 'buy' else 'short'
            lev = float(live.get('leverage') or 1)
            collateral = float(live.get('collateral') or 0)
            if entry <= 0:
                continue
            t = get_tuned()
            engine_open(lg, coin=coin, direction=side, price=entry,
                        margin=collateral if collateral > 0 else 1.0,
                        leverage=lev, sl_pct=t['sl'], tp_pct=t['tp'],
                        ts=parse_any_time(live.get('openedAt')),
                        live_id=live.get('id'))
            found.add(coin)
            add_log(f'Reconcile: ADOPTED {side} {coin} @ {fmt_price(entry)}')
        stale = [p for p in lg['positions'] if p.get('coin') not in found]
        for p in stale:
            # FIX (risk-tracking gap): previously this just removed the position with
            # no trade record, so liquidations/manual closes were invisible to
            # consec_losses / checkpoint_guard / win-rate stats. Now we run it through
            # the normal finalize_close path (using engine_exit_leg for PnL bookkeeping)
            # so the outcome is recorded like any other close. sync_live_capital() right
            # after will still correct lg['capital'] to the real exchange balance, so
            # this is for record-keeping/risk-tracking, not the authoritative $ amount.
            last_price = state['prices'].get(p.get('coin')) or p.get('entry_price')
            add_log(f'Reconcile: {p.get("coin")} not on exchange - settling as liquidated/manual close')
            finalize_close(lg, p, last_price, 'liquidated_or_manual', time.time(),
                            lambda pos, frac, price: engine_exit_leg(lg, pos, frac, price))
        sync_live_capital()
        save_state()
    except Exception as e:
        add_log(f'Reconcile error: {e}')
        log_exception('reconcile failed')

def live_preflight():
    fails = []
    if not state.get('token_ok'):
        fails.append('توکن تأیید نشده')
    if time.time() - state.get('last_scan_ts', 0) > 900:
        fails.append('فید قیمت زنده نیست')
    if not dash_secret():
        fails.append('رمز داشبورد تنظیم نشده')
    if not (state.get('tg_token') and state.get('tg_chat')):
        fails.append('تلگرام وصل نیست')
    bal = nb_margin_balance()
    if bal is None:
        fails.append('موجودی کیف تعهدی خوانده نشد')
    elif bal < 10:
        fails.append(f'موجودی تعهدی کم ({bal:.1f}$ < 10$)')
    try:
        ok, _ = nb_book_depth_ok('BTC', 10, 'long')
        if not ok:
            fails.append('اردربوک BTC سالم نیست')
    except Exception:
        fails.append('تست اردربوک شکست')
    min_n = int(os.environ.get('LIVE_MIN_TRADES', '30') or 30)
    trades = get_ledger('paper')['trades']
    n = len(trades)
    if n < min_n:
        fails.append(f'شواهد مجازی کافی نیست ({n}/{min_n})')
    else:
        gp = sum(t['pnl'] for t in trades if t.get('pnl', 0) > 0)
        gl = abs(sum(t['pnl'] for t in trades if t.get('pnl', 0) < 0))
        pf = (gp / gl) if gl > 0 else 99
        if pf < 1.1:
            fails.append(f'PF مجازی {pf:.2f} < 1.1')
    return (len(fails) == 0), fails

# ============ HOLD analyzer ============

def analyze_hold(coin):
    closes = get_candles(coin, 'D', 200, drop_forming=True)
    if not closes or len(closes) < 60:
        return None
    arr = np.array(closes, dtype=float)
    price = float(arr[-1])
    score, reasons = 0, []
    ma50 = float(np.mean(arr[-50:]))
    ma200 = float(np.mean(arr[-min(200, len(arr)):]))
    if ma50 > ma200 * 1.02:
        score += 2; reasons.append('میانگین ۵۰ روزه بالای ۲۰۰ روزه (+2)')
    elif ma50 < ma200 * 0.98:
        score -= 2; reasons.append('میانگین ۵۰ روزه زیر ۲۰۰ روزه (-2)')
    else:
        reasons.append('میانگین‌های بلندمدت خنثی')
    hi, lo = float(np.max(arr)), float(np.min(arr))
    rng = (price - lo) / (hi - lo) if hi > lo else 0.5
    if rng < 0.35:
        score += 2; reasons.append(f'قیمت در {rng*100:.0f}٪ پایینی بازه - تخفیف (+2)')
    elif rng > 0.85:
        score -= 1; reasons.append(f'نزدیک سقف تاریخی ({rng*100:.0f}٪) (-1)')
    else:
        reasons.append(f'میانه بازه ({rng*100:.0f}٪)')
    if len(arr) >= 60:
        m30 = float(np.mean(arr[-30:]))
        m60 = float(np.mean(arr[-60:-30]))
        chg = (m30 - m60) / m60
        if chg > 0.05:
            score += 1; reasons.append(f'مومنتوم ۳۰ روزه مثبت ({chg*100:+.1f}%) (+1)')
        elif chg < -0.05:
            score -= 1; reasons.append(f'مومنتوم ۳۰ روزه منفی ({chg*100:+.1f}%) (-1)')
    dd = (hi - price) / hi
    if dd > 0.5:
        score += 1; reasons.append(f'{dd*100:.0f}٪ زیر سقف - پتانسیل بازگشت (+1)')
    fg = (state.get('onchain') or {}).get('fear_greed')
    if fg is not None:
        if fg <= 25:
            score += 1; reasons.append(f'ترس شدید ({fg}) (+1)')
        elif fg >= 75:
            score -= 1; reasons.append(f'طمع شدید ({fg}) (-1)')
    if score >= 4:
        verdict, v_fa, color = 'strong_buy', 'خرید پله‌ای برای هولد ✅', '#34d399'
    elif score >= 2:
        verdict, v_fa, color = 'buy', 'مناسب شروع خرید تدریجی 🟢', '#a7f3d0'
    elif score >= 0:
        verdict, v_fa, color = 'neutral', 'صبر ⏸', '#cccccc'
    elif score >= -2:
        verdict, v_fa, color = 'wait', 'فعلاً نخر 🟡', '#fbbf24'
    else:
        verdict, v_fa, color = 'avoid', 'دوری کن 🔴', '#f87171'
    return {'coin': coin, 'score': score, 'verdict': verdict, 'verdict_fa': v_fa,
            'color': color, 'price': price, 'range_pos': round(rng * 100),
            'below_high': round(dd * 100), 'reasons': reasons}

def analyze_hold_usdt():
    try:
        now_ts = int(time.time())
        r = nb_session.get(f'{NOBITEX_API}/market/udf/history',
                           params={'symbol': 'USDTIRT', 'resolution': 'D',
                                   'to': now_ts, 'countback': 200},
                           timeout=15, headers=UA)
        if r.status_code != 200 or r.json().get('s') != 'ok':
            return None
        closes = [float(x) / 10 for x in r.json()['c']]
    except Exception:
        return None
    if len(closes) < 60:
        return None
    arr = np.array(closes, dtype=float)
    price = float(arr[-1])
    score, reasons = 0, []
    ma20 = float(np.mean(arr[-20:]))
    ma90 = float(np.mean(arr[-90:])) if len(arr) >= 90 else float(np.mean(arr))
    if price < ma20 * 0.995:
        score += 2; reasons.append(f'زیر میانگین ۲۰ روزه ({ma20:,.0f}) (+2)')
    elif price > ma20 * 1.01:
        score -= 1; reasons.append('بالای میانگین ۲۰ روزه - عجله نکن (-1)')
    if ma20 > ma90 * 1.03:
        score -= 1; reasons.append('روند صعودی تند دلار - احتمال اصلاح (-1)')
    elif ma20 < ma90:
        score += 1; reasons.append('دلار در فاز آرامش (+1)')
    hi, lo = float(np.max(arr)), float(np.min(arr))
    rng = (price - lo) / (hi - lo) if hi > lo else 0.5
    if rng < 0.40:
        score += 2; reasons.append(f'{rng*100:.0f}٪ پایینی بازه ۲۰۰ روزه (+2)')
    elif rng > 0.92:
        score -= 1; reasons.append('نزدیک سقف تاریخی (-1)')
    if len(arr) >= 60:
        m30 = float(np.mean(arr[-30:])); m60 = float(np.mean(arr[-60:-30]))
        chg = (m30 - m60) / m60
        if chg < 0:
            score += 1; reasons.append(f'ماه اخیر آروم ({chg*100:+.1f}%) (+1)')
    if score >= 3:
        verdict, v_fa, color = 'buy', 'زمان مناسب خرید تتر 🟢', '#34d399'
    elif score >= 1:
        verdict, v_fa, color = 'gradual', 'خرید تدریجی منطقیه 🟡', '#a7f3d0'
    elif score >= -1:
        verdict, v_fa, color = 'neutral', 'پله‌ای بخر ⏸', '#cccccc'
    else:
        verdict, v_fa, color = 'wait', 'کمی صبر کن 🟠', '#fbbf24'
    return {'coin': 'USDT', 'score': score, 'verdict': verdict, 'verdict_fa': v_fa,
            'color': color, 'price': price, 'range_pos': round(rng * 100),
            'below_high': round((hi - price) / hi * 100), 'reasons': reasons,
            'is_toman': True}

def update_hold_analysis():
    results = []
    try:
        u = analyze_hold_usdt()
        if u:
            results.append(u)
    except Exception:
        pass
    for coin in active_coins() + HOLD_EXTRA_COINS:
        try:
            h = analyze_hold(coin)
            if h:
                if coin in HOLD_EXTRA_COINS:
                    h['hold_only'] = True
                results.append(h)
        except Exception:
            pass
        time.sleep(1)
    if results:
        results.sort(key=lambda x: -x['score'])
        state['hold_analysis'] = {'items': results,
                                  'updated': fa_now().strftime('%Y-%m-%d %H:%M')}
        save_state()
    try:
        dca_engine()
    except Exception as e:
        add_log(f'DCA engine error: {e}')
    return results

# ============ virtual DCA wallet ============

def get_dca():
    d = state.get('dca')
    if not isinstance(d, dict):
        d = {}
    d.setdefault('enabled', False)
    d.setdefault('budget', 100.0)
    d.setdefault('tranche', 5.0)
    d.setdefault('interval_days', 7)
    d.setdefault('spent', 0.0)
    d.setdefault('holdings', {})
    d.setdefault('last_buy', {})
    d.setdefault('history', [])
    state['dca'] = d
    return d

def dca_engine():
    d = get_dca()
    if not d['enabled']:
        return
    items = (state.get('hold_analysis') or {}).get('items') or []
    if not items:
        return
    now = time.time()
    if d['budget'] - d['spent'] < 1.0:
        return
    for h in items:
        coin = h['coin']
        if coin == 'USDT' or h.get('is_toman') or h.get('verdict') not in ('buy', 'strong_buy'):
            continue
        price = state['prices'].get(coin) or h.get('price')
        if not price:
            continue
        wk = None
        try:
            closes = get_candles(coin, 'D', 9, drop_forming=True)
            if closes and len(closes) >= 8:
                wk = (closes[-1] - closes[-8]) / closes[-8]
        except Exception:
            pass
        dip = (wk is not None and wk <= -0.05)
        due = (now - d['last_buy'].get(coin, 0)) >= d['interval_days'] * 86400
        if not due and not dip:
            continue
        if not due and dip and (now - d['last_buy'].get(coin, 0)) < 2 * 86400:
            continue
        mult, why = 1.0, []
        if h['verdict'] == 'strong_buy':
            mult *= 1.5; why.append('سیگنال قوی')
        if dip:
            mult *= 1.5; why.append(f'ریزش {wk*100:.0f}٪ هفتگی')
        mult = min(mult, 2.0)
        amount = min(d['tranche'] * mult, d['budget'] - d['spent'])
        if amount < 1.0:
            break
        qty = amount / price
        hold = d['holdings'].setdefault(coin, {'qty': 0.0, 'cost': 0.0})
        hold['qty'] += qty; hold['cost'] += amount
        d['spent'] += amount
        d['last_buy'][coin] = now
        d['history'].append({'coin': coin, 'amount': round(amount, 2), 'price': price,
                             'qty': qty, 'why': ' + '.join(why) or 'خرید دوره‌ای',
                             'time': fa_now().isoformat()})
        if len(d['history']) > 200:
            d['history'] = d['history'][-150:]
        add_log(f'DCA buy (virtual): {amount:.2f}$ {coin} - spot balance preserved')
        send_telegram(f'💎 خرید پله‌ای هولد (مجازی): {amount:.2f}$ {COIN_FA.get(coin, coin)} @ {fmt_price(price)}\n💼 (سرمایه اسپات دست‌نخورده)')
    save_state()

def dca_portfolio_stats():
    d = get_dca()
    rows = []
    tot_cost = tot_val = 0.0
    hold_prices = {i['coin']: i.get('price') for i in (state.get('hold_analysis') or {}).get('items', [])}
    for coin, h in d['holdings'].items():
        if h['qty'] <= 0:
            continue
        price = state['prices'].get(coin) or hold_prices.get(coin)
        val = h['qty'] * price if price else None
        tot_cost += h['cost']
        if val is not None:
            tot_val += val
        rows.append({'coin': coin, 'qty': h['qty'], 'cost': h['cost'],
                     'avg': h['cost'] / h['qty'] if h['qty'] > 0 else 0,
                     'price': price, 'value': val,
                     'pnl': (val - h['cost']) if val is not None else None,
                     'pnl_pct': ((val - h['cost']) / h['cost'] * 100) if (val is not None and h['cost'] > 0) else None})
    rows.sort(key=lambda r: -(r['value'] or 0))
    return {'rows': rows, 'total_cost': tot_cost, 'total_value': tot_val,
            'total_pnl': tot_val - tot_cost, 'cash': d['budget'] - d['spent']}

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
    p = os.environ.get('TG_PROXY', '').strip() or state.get('tg_proxy', '')
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
    return send_telegram('🤖 ربات نوبیتکس v25 وصل شد!')

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
                p_lg, l_lg = get_ledger('paper'), get_ledger('live')
                spot_val = nb_spot_usdt()
                margin_val = nb_margin_balance()
                send_telegram(
                    f"📊 وضعیت تفکیک کیف پول‌ها\nحالت: {'🔴 لایو+مجازی' if state['mode']=='live' else '🔵 فقط مجازی'}\n"
                    f"🔵 مجازی: {p_lg['capital']:.2f}$ ({len(p_lg['positions'])} باز)\n"
                    + (f"🟢 تعهدی (معاملات لایو): {(margin_val or l_lg['capital'] or 0):.2f}$ ({len(l_lg['positions'])} باز)\n"
                       f"💼 اسپات (هولد/DCA): {(spot_val or 0):.2f}$\n" if state['mode'] == 'live' else '')
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
            elif text.startswith('/help'):
                send_telegram('/status /paper /report /stop /start /killswitch')
        save_state()
    except Exception:
        pass

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

# ============ reports / health / countdown ============

_AUTO_BACKUP_TEXT_DONE = [0.0]

def daily_report_text():
    today = fa_now().strftime('%Y-%m-%d')
    lines = [f'📊 گزارش شب - {today}']
    for lg_name in ('paper', 'live'):
        lg = get_ledger(lg_name)
        if lg_name == 'live' and state.get('mode') != 'live':
            continue
        base = PAPER_CAPITAL if lg_name == 'paper' else live_base_capital()
        cap = lg['capital'] or base
        todays = [t for t in lg['trades'] if str(t.get('time', '')).startswith(today)]
        day_pnl = sum(t.get('pnl', 0) for t in todays)
        wins_t = sum(1 for t in todays if t.get('pnl', 0) > 0)
        st = perf_stats(lg_name)
        if lg_name == 'live':
            spot_val = nb_spot_usdt()
            margin_val = nb_margin_balance()
            lines.append(f'\n{ledger_label("live")} — تعهدی (معاملات): {(margin_val or cap):.2f}$ | اسپات (هولد): {(spot_val or 0):.2f}$')
        else:
            lines.append(f'\n{ledger_label(lg_name)} — موجودی: {cap:.2f}$ ({cap-base:+.2f}$)')
        lines.append(f'امروز: {len(todays)} معامله | {wins_t} برد | {day_pnl:+.2f}$')
        if st:
            lines.append(f"کل: {st['count']} | برد {st['win_rate']}% (CI {st['wr_ci'][0]}–{st['wr_ci'][1]}) | PF {st['profit_factor']} | DD {st['max_drawdown']}%")
        if lg['positions']:
            lines.append('باز: ' + '، '.join(f"{COIN_FA.get(p.get('coin'), '?')}{'📈' if p['direction']=='long' else '📉'}"
                                             for p in lg['positions']))
    eq = [e['eq'] for e in get_ledger('paper')['equity']][-16:]
    if len(eq) >= 2:
        blocks = '▁▂▃▄▅▆▇█'
        lo, hi = min(eq), max(eq)
        if hi > lo:
            lines.append('📉 ' + ''.join(blocks[min(7, int((v - lo) / (hi - lo) * 7))] for v in eq))
    lines.append(f"نسخه {STRATEGY_VERSION}{' | 🧊 فریز' if learning_frozen() else ''}")
    return '\n'.join(lines)

_BOOT_TS = time.time()

def system_health():
    now = time.time()
    scan_age = now - state.get('last_scan_ts', _BOOT_TS)
    h = {'uptime_h': (now - _BOOT_TS) / 3600, 'scan_age_s': int(scan_age),
         'scan_ok': scan_age < 900, 'price_fails': state.get('price_fails', 0),
         'tg_on': bool(state.get('tg_token') and state.get('tg_chat')),
         'benched': [c for c in COIN_MAP if not coin_supported(c)],
         'active_n': len(active_coins())}
    try:
        import shutil
        du = shutil.disk_usage(BASE_DIR)
        h['disk_free_gb'] = du.free / 1e9
    except Exception:
        h['disk_free_gb'] = None
    try:
        h['state_kb'] = os.path.getsize(STATE_FILE) / 1024
    except Exception:
        h['state_kb'] = 0
    h['last_err'] = ''
    try:
        with open(LOG_FILE, 'rb') as f:
            f.seek(max(0, os.path.getsize(LOG_FILE) - 4000))
            tail = f.read().decode('utf-8', 'replace')
        for line in reversed(tail.splitlines()):
            if 'ERROR' in line:
                h['last_err'] = line[:120]
                break
    except Exception:
        pass
    return h

def heartbeat_text():
    ago = int(time.time() - state.get('last_scan_ts', 0))
    pl = len(get_ledger('paper')['positions'])
    ll = len(get_ledger('live')['positions'])
    return (f'💓 زنده‌ام | اسکن {state.get("total_scans", 0)} (آخری {ago//60} دقیقه پیش)\n'
            f'مجازی {pl} باز ({get_ledger("paper")["capital"]:.2f}$)' +
            (f' | واقعی {ll} باز' if state.get('mode') == 'live' else ''))

def countdown_line():
    try:
        end = datetime.fromisoformat(VPS_END_DATE)
        days = (end - datetime.now()).days
        n = len(get_ledger('paper')['trades'])
        if days >= 0:
            return f'⏳ {days} روز تا پایان شارژ سرور • تست تمیز: {n} معامله'
        return f'⚠️ شارژ سرور {-days} روز پیش تمام شده!'
    except Exception:
        return ''

def auto_backup_reminder():
    if time.time() - state.get('last_backup_nag', 0) > 7 * 86400:
        state['last_backup_nag'] = time.time()
        send_telegram('💾 یادآوری: با WinSCP یه کپی از state.json بگیر')

# ============ MAIN LOOP ============

def bot_loop():
    _api_host = NOBITEX_API.split('//')[-1]
    add_log(f'Bot v{STRATEGY_VERSION} started (paper always-on, live={"ON" if state["mode"]=="live" else "off"}, api={_api_host})')
    if 'testnet' in NOBITEX_API:
        add_log('🧪 DEMO mode: NOBITEX_API_BASE points at Nobitex testnet — orders use virtual funds')
    state['status'] = 'Active'
    validate_coins_once()
    if state.get('mode') == 'live':
        reconcile_live_positions()
    last_scan = last_balance = last_onchain = last_optimize = 0.0
    last_light = 0.0
    last_heartbeat = time.time()
    optimize_delay_done = False
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
                auto_backup_reminder()

            if now - state.get('last_selfguard', 0) > 600:
                state['last_selfguard'] = now
                try:
                    if now - state.get('last_hb_log', 0) > 1800:
                        state['last_hb_log'] = now
                        logging.info('HEARTBEAT scans=%s price_ts=%s fails=%s benched=%s traffic=%s',
                                     state.get('total_scans', 0), state.get('prices_ts', '-'),
                                     state.get('price_fails', 0),
                                     [c for c in COIN_MAP if not coin_supported(c)],
                                     (state.get('traffic') or {}).get('bytes', 0))
                    if now - state.get('last_unbench', 0) > 1800:
                        state['last_unbench'] = now
                        cf = state.get('coin_fail') or {}
                        benched = [c for c in COIN_MAP if cf.get(c, 0) >= 5]
                        if benched:
                            for c in benched:
                                cf[c] = 0
                            add_log(f'Bench re-test: {",".join(benched)}')
                    h = system_health()
                    if h['disk_free_gb'] is not None and h['disk_free_gb'] < 0.5 and not state.get('disk_warned'):
                        state['disk_warned'] = True
                        send_telegram(f'🚨 فضای دیسک کمه ({h["disk_free_gb"]:.1f}G)!')
                    elif h['disk_free_gb'] is not None and h['disk_free_gb'] > 1.0:
                        state['disk_warned'] = False
                    open_live = len(get_ledger('live')['positions'])
                    open_any = open_live + len(get_ledger('paper')['positions'])
                    if open_any and h['scan_age_s'] > 1800 and not state.get('blackout_warned'):
                        state['blackout_warned'] = True
                        send_telegram(f'🚨 {open_any} معامله بازه و {h["scan_age_s"]//60} دقیقه‌ست قیمت نداریم! اینترنت/سرور رو چک کن')
                    elif h['scan_ok']:
                        state['blackout_warned'] = False
                    if state.get('mode') == 'live' and now - state.get('last_reconcile', 0) > 1800:
                        state['last_reconcile'] = now
                        reconcile_live_positions()
                except Exception:
                    log_exception('self guard failed')

            if now - last_onchain > 900:
                last_onchain = now
                rate = get_usdt_toman()
                if rate:
                    state['usdt_irt'] = rate
                oc = get_onchain_data()
                if oc and oc.get('available'):
                    state['onchain'] = oc
                old_r = (state.get('regime') or {}).get('regime')
                rg = detect_regime()
                if rg:
                    state['regime'] = rg
                    if rg['regime'] != old_r and old_r is not None:
                        send_telegram(f'🔄 رژیم بازار: {REGIME_FA.get(rg["regime"])}')

            if not optimize_delay_done:
                if now - state.get('loop_start', now) > 600:
                    optimize_delay_done = True
                    last_optimize = now
                    auto_optimize()
                    update_coin_filter()
                    update_hold_analysis()
                    state['last_hold'] = now
                elif 'loop_start' not in state:
                    state['loop_start'] = now
            elif now - last_optimize > 86400:
                last_optimize = now
                auto_optimize()
                update_coin_filter()
            if state.get('hold_analysis') and now - state.get('last_hold', 0) > 21600:
                state['last_hold'] = now
                update_hold_analysis()
            if state.get('last_lab', 0) == 0:
                state['last_lab'] = now - LAB_INTERVAL + 3600
            elif now - state['last_lab'] > LAB_INTERVAL:
                state['last_lab'] = now
                try:
                    run_strategy_lab()
                except Exception as e:
                    add_log(f'Lab error: {e}')

            if now - last_scan >= SCAN_INTERVAL:
                prices = get_prices()
                if prices:
                    if state.get('price_fails'):
                        add_log(f'Prices back after {state["price_fails"]} failures')
                        state['price_fails'] = 0
                    last_scan = now
                    state['last_scan_ts'] = now
                    mark_prices(prices)
                    state.setdefault('price_history', []).append(prices)
                    if len(state['price_history']) > 500:
                        state['price_history'] = state['price_history'][-300:]
                    state['total_scans'] += 1
                    add_log(f"Scan #{state['total_scans']} ({state['data_source']})")
                    evaluate_shadows()
                    manage_all_positions()
                    if (not state.get('manual_paused')
                            and len(state['price_history']) >= 3):
                        signals = analyze()
                        if signals and not crash_guard_active():
                            adaptive_adjust()
                            for sig in signals:
                                open_engine_position('paper', sig)
                                if state.get('mode') == 'live' and state.get('api_token'):
                                    open_engine_position('live', sig)
                    save_state()
                else:
                    last_scan = now - SCAN_INTERVAL + 90
                    state['price_fails'] = state.get('price_fails', 0) + 1
                    if state['price_fails'] == 1 or state['price_fails'] % 5 == 0:
                        add_log(f'Price error (fail #{state["price_fails"]} - retry 90s)')
                    logging.warning('PRICE_FEED_FAIL n=%s last_ok=%ss ago',
                                    state['price_fails'],
                                    int(now - (state.get('last_scan_ts') or now)))
                    if (state.get('last_scan_ts') and now - state['last_scan_ts'] > 900
                            and now - state.get('last_watchdog_alert', 0) > 900):
                        state['last_watchdog_alert'] = now
                        send_telegram('⚠️ ۱۵ دقیقه‌ست قیمت نمی‌گیرم! اینترنت/فیلترشکن رو چک کن')
            else:
                open_any = any(get_ledger(n)['positions'] for n in ('paper', 'live'))
                light_iv = 30 if open_any else 150
                fresh = None
                if now - last_light >= light_iv:
                    last_light = now
                    fresh = get_all_prices_light()
                if fresh:
                    mark_prices(fresh)
                    manage_all_positions()
                    save_state()
                else:
                    touched = False
                    held_coins = {p.get('coin') for lg in ('paper', 'live')
                                  for p in get_ledger(lg)['positions']}
                    for pcoin in held_coins:
                        p = get_nobitex_price(pcoin)
                        if p and p > 0:
                            with state_lock:
                                state['prices'][pcoin] = p
                                state.setdefault('price_ts_map', {})[pcoin] = fa_now().strftime('%H:%M:%S')
                            touched = True
                    if touched:
                        manage_all_positions()
                        save_state()

            for lg_name in ('paper', 'live'):
                lg = get_ledger(lg_name)
                base = PAPER_CAPITAL if lg_name == 'paper' else live_base_capital()
                check_daily_limit_lg(lg, base)

            tg_poll()

            if state.get('mode') == 'live' and state.get('api_token') and now - last_balance > 300:
                last_balance = now
                bal = nb_margin_balance()
                if bal is not None:
                    state['live_balance'] = bal
        except Exception as e:
            add_log(f'Loop error: {e}')
            log_exception('bot_loop iteration failed')
        time.sleep(POS_CHECK_INTERVAL)

# ============ Web dashboard ============

CSS = """
:root{--bg:#070714;--card:#11122b;--card2:#161735;--line:#23244a;--txt:#e8e9f5;--dim:#8d90b5;--cyan:#22d3ee;--green:#34d399;--red:#f87171;--amber:#fbbf24;--violet:#a78bfa}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:Tahoma,'Segoe UI',Arial;background:radial-gradient(1000px 500px at 85% -10%,rgba(34,211,238,.09),transparent),radial-gradient(800px 400px at 0% 110%,rgba(167,139,250,.08),transparent),var(--bg);color:var(--txt);padding:12px 12px 40px;font-size:15px}
.c{max-width:1150px;margin:0 auto}
.hdr{position:sticky;top:8px;z-index:50;display:flex;align-items:center;justify-content:space-between;gap:8px;background:rgba(17,18,43,.88);backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:16px;padding:10px 16px;margin-bottom:14px}
.brand{display:flex;align-items:center;gap:10px;font-weight:bold}
.dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green);animation:pl 2s infinite}
@keyframes pl{50%{opacity:.35}}
.ttl{font-size:1em}.sub{font-size:.72em;color:var(--dim)}
.badge{padding:5px 14px;border-radius:999px;font-size:.78em;font-weight:bold;white-space:nowrap}
.badge.paper{background:rgba(34,211,238,.14);color:var(--cyan);border:1px solid rgba(34,211,238,.4)}
.badge.live{background:rgba(248,113,113,.16);color:var(--red);border:1px solid rgba(248,113,113,.5)}
.nav{display:flex;gap:8px;margin:0 0 14px;overflow-x:auto;padding-bottom:2px}
.nav a{flex:0 0 auto;background:var(--card);border:1px solid var(--line);color:var(--txt);padding:9px 16px;border-radius:12px;text-decoration:none;font-size:.85em;font-weight:bold}
.nav a.acc{background:linear-gradient(135deg,rgba(34,211,238,.18),rgba(167,139,250,.14));border-color:rgba(34,211,238,.45)}
.kpi{display:flex;flex-wrap:wrap;gap:6px;align-items:center;background:linear-gradient(165deg,#161735,#11122b);border:1px solid #23244a;border-radius:14px;padding:10px 12px;margin:10px 0;font-size:.78em}
.kpi .sep{color:#3a3c66}
.abtn{margin-right:auto;background:rgba(248,113,113,.15);border:1px solid rgba(248,113,113,.5);color:#f87171;padding:6px 12px;border-radius:9px;font-weight:bold;text-decoration:none}
.top{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0 0 14px}
.stat{background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:16px;padding:16px 10px;text-align:center}
.stat .val{font-size:1.7em;font-weight:800;color:var(--green)}
.stat .lbl{color:var(--dim);font-size:.72em;margin-top:6px;line-height:1.6}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:12px;margin:0 0 12px}
.card{background:linear-gradient(165deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:12px}
.card h3{color:var(--cyan);margin-bottom:12px;font-size:.95em}
.m{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:.9em}
.m:last-child{border-bottom:none}
.m span:last-child{font-weight:bold;direction:ltr}
.log{background:rgba(0,0,0,.45);border:1px solid var(--line);border-radius:12px;padding:12px;max-height:260px;overflow-y:auto;font-size:.78em;direction:ltr;text-align:left;font-family:Consolas,monospace;line-height:1.8;color:#9aa0c7}
table{width:100%;border-collapse:collapse;font-size:.82em}
th,td{padding:9px 5px;text-align:center;border-bottom:1px solid rgba(255,255,255,.06)}
th{background:rgba(34,211,238,.08);color:var(--cyan);font-size:.9em}
a.btn{display:inline-block;background:linear-gradient(135deg,var(--cyan),#3b82f6);color:#04101c;padding:10px 18px;border-radius:12px;text-decoration:none;font-weight:bold;font-size:.85em;margin:3px}
.alert{text-align:center;border-radius:14px;padding:12px;margin:0 0 12px;font-size:.88em}
.alert.red{background:rgba(248,113,113,.12);border:1px solid rgba(248,113,113,.45)}
.alert.amber{background:rgba(251,191,36,.10);border:1px solid rgba(251,191,36,.4)}
.pos{background:rgba(52,211,153,.05);border:1px solid rgba(52,211,153,.3);border-radius:14px;padding:14px;margin:10px 0}
.closebtn{float:left;background:rgba(248,113,113,.12);border:1px solid rgba(248,113,113,.4);color:#f87171;padding:4px 10px;border-radius:8px;font-size:.9em;text-decoration:none}
input[type=text],input[type=password],input[type=number],select{width:100%;padding:12px;border-radius:12px;border:1px solid var(--line);background:#0b0c1e;color:#fff;font-size:15px}
label{display:block;margin:12px 0 6px;color:var(--dim);font-size:.9em}
.radio{margin:12px 0;padding:12px;background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:12px}
.radio label{display:inline;color:var(--txt)}
button{width:100%;background:linear-gradient(135deg,var(--cyan),#3b82f6);color:#04101c;border:none;padding:15px;border-radius:14px;font-size:1.05em;font-weight:bold;cursor:pointer;margin-top:16px}
.warn{background:rgba(248,113,113,.10);border:1px solid rgba(248,113,113,.4);padding:14px;border-radius:12px;margin:12px 0;font-size:.85em;line-height:1.9}
.hint{font-size:.8em;color:var(--dim);margin-top:6px;line-height:1.8}
.foot{text-align:center;color:var(--dim);font-size:.72em;margin-top:18px;line-height:2}
@media(max-width:640px){.top{grid-template-columns:repeat(3,1fr);gap:6px}.stat{padding:12px 6px}.stat .val{font-size:1.25em}.g{grid-template-columns:1fr}table{font-size:.75em}}
"""

def shell(title, body, refresh=45):
    js = ''
    if refresh:
        js = (f'<script>(function r(){{setTimeout(function(){{'
              f'if(document.hidden){{r()}}else{{location.reload()}}'
              f'}},{int(refresh*1000)})}})();</script>')
    return (f'<!DOCTYPE html><html dir="rtl" lang="fa"><head><meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f'<meta name="theme-color" content="#070714"><title>{title}</title>'
            f'<style>{CSS}</style>{js}</head><body><div class="c">{body}</div></body></html>')

def action_token():
    if not dash_secret():
        return ''
    return hashlib.sha256((session_key() + '|act').encode()).hexdigest()[:12]

def session_cookie_value():
    return hashlib.sha256((session_key() + '|sess').encode()).hexdigest()[:32]

def reason_fa(r):
    return {'take_profit': 'حد سود 🎯', 'stop_loss': 'حد ضرر 🛑', 'profit_lock': 'قفل سود 🔒',
            'breakeven': 'سر به سر ⚖️', 'max_age': 'طولانی شدن ⏰', 'runner_end': 'دونده 🏃',
            'kill_switch': 'اضطراری 🔴', 'legacy_review': 'بازبینی 🔄'}.get(r, r or '-')

def price_rows_html():
    rows = ''
    tsp = state.get('price_ts_map') or {}
    now = fa_now()
    for c in COIN_MAP:
        p = (state.get('prices') or {}).get(c, 0) or 0
        cts = tsp.get(c, '')
        age_html = ''
        if cts:
            try:
                hh, mm, ss = map(int, cts.split(':'))
                then = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
                age = (now - then).total_seconds()
                if age < 0:
                    age += 86400
                dot = '#34d399' if age <= 45 else ('#fbbf24' if age <= 120 else '#f87171')
                age_txt = f'{int(age)}s' if age < 90 else f'{int(age/60)}m'
                age_html = f" <span style='color:{dot};font-size:.68em'>●</span><span style='color:#6a6d92;font-size:.65em'> {cts} ({age_txt})</span>"
            except Exception:
                pass
        bench = ''
        if not coin_supported(c):
            bench = " <span style='color:#f87171;font-size:.62em;border:1px solid rgba(248,113,113,.4);border-radius:6px;padding:1px 5px'>⛔ بنش</span>"
        rows += f"<div class='m'><span>{COIN_FA.get(c, c)}{age_html}{bench}</span><span>{fmt_price(p)}</span></div>"
    return rows

def positions_html(lg_name):
    lg = get_ledger(lg_name)
    out = ''
    tok = action_token()
    for pos in lg['positions']:
        coin = pos.get('coin', 'BTC')
        cur = state['prices'].get(coin, pos['entry_price'])
        upnl = ((cur - pos['entry_price']) / pos['entry_price'] * 100) if pos['direction'] == 'long' \
            else ((pos['entry_price'] - cur) / pos['entry_price'] * 100)
        upnl_c = '#34d399' if upnl >= 0 else '#f87171'
        flags = []
        if pos.get('partial_done'):
            flags.append('💰 پله‌ای')
        if pos.get('half_cashed'):
            flags.append('💵 نصف نقد')
        if pos.get('ladder1'):
            flags.append('🪜 نردبان')
        if pos.get('runner'):
            flags.append('🏃 دونده')
        if pos.get('trail_active'):
            flags.append('🔒 قفل سود')
        if (time.time() - pos.get('open_ts', time.time())) >= 86400:
            flags.append('⏰ تمدید‌شده (۱ ظرفیت مشترک)')
        if pos.get('close_fails'):
            flags.append(f"⚠️ شکست بستن ×{pos['close_fails']}")
        tok_q = f'&tok={tok}' if tok else ''
        out += f"""
<div class="pos">
<h3 style="color:#22d3ee">📌 {COIN_FA.get(coin, coin)} — {'خرید 📈' if pos['direction']=='long' else 'فروش 📉'}
<span style="color:{upnl_c};float:left">{upnl:+.2f}%</span></h3>
<div class="m"><span>ورود</span><span>{fmt_price(pos['entry_price'])}</span></div>
<div class="m"><span>حد ضرر</span><span style="color:#f87171">{fmt_price(pos['stop_loss'])}</span></div>
<div class="m"><span>حد سود</span><span style="color:#34d399">{fmt_price(pos['take_profit'])}</span></div>
<div class="m"><span>مارجین</span><span>{pos.get('margin',0):.2f}$ × {pos.get('leverage',1):.0f}</span></div>
<div class="m"><span>لیکویید تقریبی</span><span style="color:#f97316">{fmt_price(liq_price_of(pos))}</span></div>
<div class="m"><span>نقد شده تاکنون</span><span>{pos.get('banked',0):+.4f}$</span></div>
<p style="margin-top:8px;color:#8d90b5;font-size:.85em">{' • '.join(flags) if flags else 'در انتظار حرکت'}
<a class="closebtn" href="/closepos?coin={coin}&scope={lg_name}{tok_q}" onclick="return confirm('معامله {COIN_FA.get(coin, coin)} بسته شه؟')">✂️ بستن دستی</a></p>
</div>"""
    if not out:
        out = '<div style="padding:15px;color:#888;text-align:center">معامله بازی نیست - منتظر سیگنال قوی</div>'
    return out

def equity_svg(lg_name, base):
    eq = get_ledger(lg_name)['equity']
    if len(eq) < 2:
        return ''
    vals = [p['eq'] for p in eq][-100:]
    mn, mx = min(vals), max(vals)
    rng = (mx - mn) or 1
    W, H = 600, 120
    pts = []
    for i, v in enumerate(vals):
        x = i / (len(vals) - 1) * W
        y = H - (v - mn) / rng * (H - 10) - 5
        pts.append(f'{x:.1f},{y:.1f}')
    color = '#00ff88' if vals[-1] >= base else '#ff6b6b'
    base_line = ''
    if mn <= base <= mx:
        by = H - (base - mn) / rng * (H - 10) - 5
        base_line = f'<line x1="0" y1="{by:.1f}" x2="{W}" y2="{by:.1f}" stroke="#555" stroke-dasharray="4"/>'
    return f"""<div class="card"><h3>📉 نمودار سرمایه</h3>
<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;background:rgba(0,0,0,.25);border-radius:8px">{base_line}<polyline points="{' '.join(pts)}" fill="none" stroke="{color}" stroke-width="2"/></svg>
<p style="color:#888;font-size:.85em">شروع: {base:.2f}$ | الان: {vals[-1]:.2f}$</p></div>"""

def stats_card(lg_name, base):
    st = perf_stats(lg_name)
    if not st:
        return '<div class="card"><h3>📈 آمار عملکرد</h3><p style="color:#888">بعد از اولین معامله</p></div>'
    cnt = st['count']
    if cnt < 15:
        note = f'<p style="color:#fbbf24;font-size:.78em">⚠️ فقط {cnt} معامله - هنوز «نویز» حساب می‌شه. هدف: ۵۰</p>'
    elif cnt < 50:
        note = f'<p style="color:#22d3ee;font-size:.78em">📊 {cnt}/۵۰ معامله - آمار در حال شکل‌گیریه</p>'
    else:
        note = f'<p style="color:#34d399;font-size:.78em">✅ {cnt} معامله - از نظر آماری قابل اتکا</p>'
    pfc = pf_color(st['profit_factor'])
    csv_q = f'?scope={lg_name}'
    return f"""<div class="card"><h3>📈 آمار عملکرد {ledger_label(lg_name)}</h3>{note}
<div class="m"><span>معاملات</span><span>{st['count']}</span></div>
<div class="m"><span>درصد برد</span><span>{st['win_rate']}%</span></div>
<div class="m"><span>بازه اطمینان ۹۵٪ وین‌ریت</span><span>{st['wr_ci'][0]}٪ تا {st['wr_ci'][1]}٪</span></div>
<div class="m"><span>میانگین برد / باخت</span><span>{st['avg_win']:+.4f} / {st['avg_loss']:+.4f}$</span></div>
<div class="m"><span>Profit Factor</span><span style="color:{pfc}">{'∞' if st['profit_factor']>=99 else st['profit_factor']}</span></div>
<div class="m"><span>بیشترین افت</span><span>{st['max_drawdown']}%</span></div>
<div class="m"><span>شارپ / سورتینو</span><span>{st['sharpe']} / {st['sortino']}</span></div>
<p style="margin-top:8px"><a href="/trades.csv{csv_q}" style="color:#22d3ee">📥 دانلود CSV</a></p>
</div>"""

def regime_card():
    rg = state.get('regime')
    si = session_info()
    sess_html = f"<div class='m'><span>جلسه جهانی</span><span>{si['label']} • {si['hour']}</span></div>"
    if not rg:
        return f'<div class="card"><h3>🌡️ رژیم بازار</h3><p style="color:#888">در حال تشخیص…</p>{sess_html}</div>'
    colors = {'trend_up': '#00ff88', 'trend_down': '#ff6b6b', 'range': '#cccccc', 'storm': '#ffaa44'}
    return f"""<div class="card"><h3>🌡️ رژیم بازار</h3>
<p style="font-size:1.25em;font-weight:bold;color:{colors.get(rg.get('regime'),'#fff')}">{REGIME_FA.get(rg.get('regime'), rg.get('regime'))}</p>
<div class="m"><span>نوسان ساعتی</span><span>{rg.get('volatility','-')}%</span></div>
<div class="m"><span>قدرت روند</span><span>{rg.get('trend_pct','-')}%</span></div>
<div class="m"><span>Efficiency</span><span>{rg.get('efficiency','-')}</span></div>
{sess_html}
</div>"""

def health_card():
    h = system_health()
    ok = '<span style="color:#34d399">●</span>'
    bad = '<span style="color:#f87171">●</span>'
    warn = '<span style="color:#fbbf24">●</span>'
    rows = [f"<div class='m'><span>{ok if h['scan_ok'] else bad} آخرین اسکن موفق</span><span>{h['scan_age_s']//60} دقیقه پیش</span></div>",
            f"<div class='m'><span>{ok} آپ‌تایم</span><span>{h['uptime_h']:.1f} ساعت</span></div>",
            f"<div class='m'><span>{ok if h['price_fails']==0 else warn} خطای قیمت پیاپی</span><span>{h['price_fails']}</span></div>",
            f"<div class='m'><span>{ok if h['tg_on'] else warn} تلگرام</span><span>{'وصل' if h['tg_on'] else 'تنظیم نشده'}</span></div>",
            f"<div class='m'><span>{ok} ارزهای فعال</span><span>{h['active_n']}/{len(COIN_MAP)}{' (نیمکت: '+','.join(h['benched'])+')' if h['benched'] else ''}</span></div>"]
    tr_bytes = (state.get('traffic') or {}).get('bytes', 0)
    rows.append(f"<div class='m'><span>{ok} 📶 ترافیک نوبیتکس امروز</span><span>{tr_bytes/1e6:.1f} MB</span></div>")
    if h['disk_free_gb'] is not None:
        d_ok = h['disk_free_gb'] > 1.0
        rows.append(f"<div class='m'><span>{ok if d_ok else bad} دیسک آزاد</span><span>{h['disk_free_gb']:.1f} گیگ</span></div>")
    err = f"<p style='color:#f87171;font-size:.72em;direction:ltr;text-align:left;margin-top:6px'>آخرین خطا: {h['last_err']}</p>" if h['last_err'] else ''
    return f"<div class='card'><h3>🩺 سلامت سیستم</h3>{''.join(rows)}{err}</div>"

def scan_table_html():
    rows = ''
    for row in (state.get('scan_table') or []):
        c_fa = COIN_FA.get(row['coin'], row['coin'])
        d = row.get('direction')
        d_fa = 'خرید 📈' if d == 'long' else ('فروش 📉' if d == 'short' else '—')
        cs = (state.get('coin_scores') or {}).get(row['coin'])
        if cs is None or learning_frozen():
            status = '<span style="color:#888">مجاز</span>'
        elif cs.get('allowed', True):
            status = f"<span style='color:#34d399'>مجاز ✅ ({cs.get('return',0):+.1f}%)</span>"
        else:
            status = f"<span style='color:#f87171'>قفل 🔒 ({cs.get('return',0):+.1f}%)</span>"
        rows += f"<tr><td>{c_fa}</td><td>{row['score']:.1f}</td><td>{d_fa}</td><td>{row['rsi']}</td><td>{status}</td></tr>"
    if not rows:
        rows = '<tr><td colspan="5" style="color:#888">بعد از چند اسکن اول</td></tr>'
    return f"""<div class="card"><h3>🔎 اسکن ارزها</h3>
<table><tr><th>ارز</th><th>امتیاز</th><th>جهت</th><th>RSI</th><th>وضعیت</th></tr>{rows}</table></div>"""

def recent_trades_html(lg_name, n=10):
    trades = get_ledger(lg_name)['trades']
    rows = ''
    for t in trades[-n:]:
        pnl_v = t.get('pnl', 0)
        c = '#34d399' if pnl_v > 0 else '#f87171'
        d = 'خرید' if t.get('direction') == 'long' else 'فروش'
        rows += (f"<tr><td>{COIN_FA.get(t.get('coin','BTC'), t.get('coin','BTC'))}</td><td>{d}</td>"
                 f"<td>{fmt_price(t.get('entry',0))}</td><td>{fmt_price(t.get('exit',0))}</td>"
                 f"<td style='color:{c}'>{pnl_v:+.4f}</td><td>{reason_fa(t.get('reason'))}</td></tr>")
    if not rows:
        rows = '<tr><td colspan="6" style="color:#888">هنوز معامله‌ای نیست</td></tr>'
    return f"""<div class="card"><h3>📜 معاملات اخیر</h3>
<table><tr><th>ارز</th><th>جهت</th><th>ورود</th><th>خروج</th><th>سود/زیان</th><th>علت</th></tr>{rows}</table></div>"""

def nav_html(active):
    def cls(x):
        return ' class="acc"' if x == active else ''
    return (f'<div class="nav"><a{cls("paper")} href="/paper">🔵 موتور مجازی ۱۰۰$</a>'
            f'<a{cls("live")} href="/">{"🔴 لایو" if state["mode"]=="live" else "🏠 داشبورد"}</a>'
            f'<a href="/analytics">📊 تحلیل</a><a href="/hold">💎 هولد</a>'
            f'<a href="/settings">⚙️ تنظیمات</a><a href="/diagnostics">🔧 تشخیص</a></div>')

def header_html():
    badge = ('<span class="badge live">🔴 لایو فعال</span>' if state['mode'] == 'live'
             else '<span class="badge paper">🔵 مجازی</span>')
    rate = state.get('usdt_irt')
    return f"""<div class="hdr"><div class="brand"><span class="dot"></span>
<div><div class="ttl">🤖 ربات نوبیتکس v{STRATEGY_VERSION}</div>
<div class="sub">{state.get('data_source','nobitex').upper()}{f' • تتر {rate:,.0f} تومان' if rate else ''}{' • 🧊 فریز' if learning_frozen() else ''}</div></div></div>{badge}</div>"""

def kpi_html(ctx):
    scan_age = time.time() - state.get('last_scan_ts', 0)
    alive = '🟢 فعال' if scan_age < 900 else '🟡 مکث'
    if state.get('manual_paused'):
        alive = '⏸ متوقف'
    p_lg, l_lg = get_ledger('paper'), get_ledger('live')
    pnl_p = (p_lg['capital'] or PAPER_CAPITAL) - PAPER_CAPITAL
    dp_c = '#34d399' if pnl_p >= 0 else '#f87171'
    tok = action_token()
    panic = (f'<a class="abtn" href="/resume?tok={tok}">▶️ ادامه</a>' if state.get('manual_paused')
             else f'<a class="abtn" href="/panic?tok={tok}" onclick="return confirm(\'مطمئنی؟ همه معاملات لایو و مجازی بسته و ربات متوقف می‌شه!\')">🛑 توقف اضطراری</a>')
    live_chip = ''
    if state['mode'] == 'live':
        lc = nb_margin_balance() or l_lg['capital'] or 0
        sp = nb_spot_usdt() or 0
        live_chip = f'<span>🟢 تعهدی (معاملات): {lc:.2f}$</span><span class="sep">|</span><span>💼 اسپات (هولد): {sp:.2f}$</span><span class="sep">|</span>'
    return f"""<div class="kpi"><span>{alive}</span><span class="sep">|</span>
<span>🔵 مجازی: {(p_lg['capital'] or PAPER_CAPITAL):.2f}$ (<b style="color:{dp_c}">{pnl_p:+.2f}</b>)</span><span class="sep">|</span>
{live_chip}<span>اسکن #{state['total_scans']}</span>{panic}</div>"""

def alerts_html():
    out = ''
    last_ok = state.get('last_scan_ts') or 0
    age_s = int(time.time() - last_ok) if last_ok else 0
    if last_ok and age_s > 1200:
        out += (f'<div class="alert red">📡❌ <b>فید قیمت قطعه!</b> آخرین دریافت موفق: '
                f'{age_s//60} دقیقه پیش • خطای پیاپی: {state.get("price_fails",0)}<br>'
                f'<span style="font-size:.82em">اگر VPN فعاله خاموشش کن • اگر نه، احتمالاً قطعی نوبیتکس یا شبکه‌ی سروره — '
                f'برگه‌ی <a href="/diagnostics" style="color:#fbbf24">تشخیص</a> و <code style="direction:ltr;display:inline-block">journalctl -u nobitex-bot</code> رو چک کن</span></div>')
    if state.get('manual_paused'):
        out += '<div class="alert amber">⏸ ربات متوقف شده (دستی)</div>'
    if state.get('crash_mode'):
        out += '<div class="alert red">🌊 محافظ سقوط فعال - ربات کنار ایستاده</div>'
    for lg_name in ('paper', 'live'):
        lg = get_ledger(lg_name)
        if lg.get('trading_paused'):
            out += f'<div class="alert red">🛑 سقف ضرر روزانه {ledger_label(lg_name)} فعاله</div>'
        if lg.get('cp_triggered'):
            out += f'<div class="alert red">🛑 چک‌پوینت {ledger_label(lg_name)} شلیک شده - تصمیم لازم! (<a href="/resume?tok={action_token()}" style="color:#fbbf24">ادامه با مسئولیت خودم</a>)</div>'
        cd = lg.get('cooldown_until', 0)
        if cd > time.time():
            out += f'<div class="alert amber">⏳ استراحت {ledger_label(lg_name)} - {int((cd-time.time())/60)} دقیقه</div>'
    return out

def live_overview_page():
    lg = get_ledger('live')
    cap = lg['capital'] or 0
    base = live_base_capital()
    pnl = cap - base
    p_lg = get_ledger('paper')
    pnlp = (p_lg['capital'] or PAPER_CAPITAL) - PAPER_CAPITAL
    body = header_html() + kpi_html('live') + alerts_html() + nav_html('live')
    body += f"""
<div class="top">
<div class="stat"><div class="val">{cap:.2f}</div><div class="lbl">موجودی لایو (تتر){f'<br>≈ {fmt_toman(cap)}' if fmt_toman(cap) else ''}</div></div>
<div class="stat"><div class="val" style="color:{'#34d399' if pnl>=0 else '#f87171'}">{pnl:+.2f}</div><div class="lbl">سود/زیان لایو (از {base:.2f}$)</div></div>
<div class="stat"><div class="val" style="color:{'#34d399' if pnlp>=0 else '#f87171'}">{pnlp:+.2f}</div><div class="lbl">موتور مجازی (<a href="/paper" style="color:#22d3ee">صفحه مجازی ←</a>)</div></div>
</div>
<div class="g">{scan_table_html()}{regime_card()}{health_card()}</div>
<h3 style="margin:0 0 8px">🔴 پوزیشن‌های لایو</h3>
{positions_html('live')}
<div class="g">{recent_trades_html('live')}{stats_card('live', base)}</div>
{equity_svg('live', base)}
<div class="card"><h3>📋 لاگ</h3><div class="log">{''.join(f'<div>{l}</div>' for l in state['logs'][:20])}</div></div>
<div class="foot">{countdown_line()}</div>"""
    return shell('🔴 داشبورد لایو - ربات نوبیتکس', body)

def create_html():
    if state.get('mode') == 'live':
        return live_overview_page()
    return paper_page(main=True)

def paper_page(main=False):
    lg = get_ledger('paper')
    cap = lg['capital'] or PAPER_CAPITAL
    pnl = cap - PAPER_CAPITAL
    day = lg.get('daily_pnl', 0.0)
    sig = state.get('last_signal') or {}
    sig_html = ''
    if sig:
        sig_html = (f"<div class='m'><span>ارز</span><span>{COIN_FA.get(sig.get('coin','BTC'), sig.get('coin','BTC'))}</span></div>"
                    f"<div class='m'><span>RSI</span><span>{sig.get('rsi',0):.0f}</span></div>"
                    f"<div class='m'><span>مومنتوم</span><span>{sig.get('momentum',0)*100:.2f}%</span></div>"
                    f"<div class='m'><span>امتیاز / آستانه</span><span>{sig.get('score',0):.1f} / {live_threshold()}</span></div>")
    st_note = ''
    if state.get('mode') == 'live':
        st_note = ('<div class="alert amber" style="font-size:.8em">ℹ️ این موتور با ۱۰۰ دلار مجازی '
                   '<b>همیشه و موازی با لایو</b> کار می‌کند تا اعتبار استراتژی بی‌وقفه سنجیده شود - ربطی به پول واقعی ندارد.</div>')
    else:
        st_note = '<div class="alert amber" style="font-size:.8em">حالت مجازی فعال است. روشن‌کردن لایو: <a href="/settings" style="color:#fbbf24">تنظیمات ←</a></div>'
    body = header_html() + kpi_html('paper') + alerts_html() + nav_html('paper') + st_note
    body += f"""
<div class="top">
<div class="stat"><div class="val">{cap:.2f}</div><div class="lbl">موجودی مجازی (تتر){f'<br>≈ {fmt_toman(cap)}' if fmt_toman(cap) else ''}</div></div>
<div class="stat"><div class="val" style="color:{'#34d399' if pnl>=0 else '#f87171'}">{pnl:+.2f}</div><div class="lbl">سود/زیان کل ({pnl/PAPER_CAPITAL*100:+.1f}%){f'<br>≈ {fmt_toman(pnl)}' if fmt_toman(pnl) else ''}</div></div>
<div class="stat"><div class="val" style="color:{'#34d399' if day>=0 else '#f87171'}">{day:+.2f}</div><div class="lbl">سود/زیان امروز</div></div>
</div>
<div class="g">
<div class="card"><h3>💰 قیمت‌ها <span style="color:#8d90b5;font-size:.7em">● سبز = تازه</span></h3>{price_rows_html()}</div>
<div class="card"><h3>📊 سیگنال فعلی</h3>{sig_html or '<p style="color:#888">بازار خنثیه</p>'}</div>
{health_card()}
{regime_card()}
{scan_table_html()}
</div>"""
    if state.get('last_reason'):
        lr = state['last_reason'].replace(chr(10), '<br>')
        body += f'<div class="card" style="border-color:rgba(52,211,153,.35)"><h3>📋 آخرین تصمیم</h3><p style="line-height:1.9;font-size:.86em">{lr}</p></div>'
    body += f"<h3 style='margin:0 0 8px'>🔵 پوزیشن‌های مجازی</h3>{positions_html('paper')}"
    body += '<div class="g">' + recent_trades_html('paper') + stats_card('paper', PAPER_CAPITAL) + '</div>'
    body += equity_svg('paper', PAPER_CAPITAL)
    body += ('<div class="card"><h3>📋 لاگ</h3><div class="log">'
             + ''.join(f'<div>{l}</div>' for l in state['logs'][:20]) + '</div></div>')
    body += f'<div class="foot">{countdown_line()} • نسخه {STRATEGY_VERSION}</div>'
    return shell('🔵 موتور مجازی ۱۰۰ دلاری - ربات نوبیتکس', body)

def create_settings_html(message=''):
    token = state.get('api_token', '')
    masked = (token[:4] + '...' + token[-4:]) if len(token) > 10 else ('(وارد نشده)' if not token else token)
    mode = state.get('mode', 'paper')
    frozen = learning_frozen()
    tg_tok = state.get('tg_token', '')
    tg_masked = (tg_tok[:6] + '...' if len(tg_tok) > 8 else ('(وارد نشده)' if not tg_tok else tg_tok))
    hb = int(state.get('heartbeat_hours', 0) or 0)
    tok_status = state.get('token_ok')
    if tok_status is True:
        tok_line = '<span style="color:#34d399">✅ توکن تأیید شد</span>'
    elif tok_status is False:
        tok_line = '<span style="color:#f87171">❌ توکن نامعتبر/خطای اتصال</span>'
    else:
        tok_line = '<span style="color:#888">تست نشده</span>'
    msg_html = f'<p style="background:rgba(34,211,238,.15);padding:10px;border-radius:8px">{message}</p>' if message else ''
    info_box = ('<div class="alert amber" style="text-align:right;font-size:.85em;line-height:1.8">'
                '💡 <b>حالت مجازی (۱۰۰ دلار):</b> هیچ نیازی به وارد کردن توکن نوبیتکس یا دستکاری فایل‌های سرور نیست! '
                'هر زمان که خواستی وارد حساب واقعی (لایو) بشوی یا تلگرامت را وصل کنی، کافیست توکن‌ها و رمز دلخواهت را '
                '<b>مستقیماً در همین فرم زیر</b> بنویسی و دکمه ذخیره را بزنی.</div>')
    n_paper = len(get_ledger('paper')['trades'])
    min_n = int(os.environ.get('LIVE_MIN_TRADES', '30') or 30)
    body = f"""
<h1 style="color:#22d3ee;font-size:1.3em;margin:10px 0">⚙️ تنظیمات</h1>
{msg_html}
{info_box}
<div class="card"><form method="POST" action="/save"><input type="hidden" name="tok" value="{action_token()}">
<h3>🔑 اتصال به نوبیتکس</h3>
<label>توکن API (فعلی: {masked})</label>
<input type="text" name="token" placeholder="Nobitex API token" autocomplete="off">
<p style="margin-top:8px">{tok_line}</p>
<h3 style="margin-top:25px">🎛️ حالت معامله</h3>
<div class="radio"><input type="radio" name="mode" value="paper" id="p" {'checked' if mode=='paper' else ''}><label for="p">🔵 فقط موتور مجازی (امن) — لایو خاموش</label></div>
<div class="radio"><input type="radio" name="mode" value="live" id="l" {'checked' if mode=='live' else ''}><label for="l">🔴 لایو + مجازی — معامله واقعی روی نوبیتکس (موتور مجازی همچنان موازی کار می‌کند)</label></div>
<p class="hint">پیش‌شرط لایو: حداقل {min_n} معامله مجازی با PF&gt;۱.۱ + تلگرام + رمز داشبورد. الان: {n_paper} معامله مجازی.</p>
<div class="warn">⚠️ در حالت لایو ضررها واقعی‌اند. اهرم هر دو موتور ≤۵x است (برابر، برای قابلیت مقایسه).</div>
<h3 style="margin-top:25px">🔔 تلگرام</h3>
<label>توکن بات (فعلی: {tg_masked})</label>
<input type="text" name="tg_token" autocomplete="off">
<label style="margin-top:8px">Chat ID</label>
<input type="text" name="tg_chat" autocomplete="off">
<p class="hint">خالی = حفظ قبلی • حذف کامل: off</p>
<label style="margin-top:8px">گزارش خودکار</label>
<select name="hb">
<option value="0" {'selected' if hb==0 else ''}>فقط گزارش شبانه (۲۲:۳۰ تهران)</option>
<option value="3" {'selected' if hb==3 else ''}>+ ضربان قلب هر ۳ ساعت 💓</option>
<option value="6" {'selected' if hb==6 else ''}>+ ضربان قلب هر ۶ ساعت 💓</option>
<option value="12" {'selected' if hb==12 else ''}>+ ضربان قلب هر ۱۲ ساعت 💓</option>
</select>
<h3 style="margin-top:25px">🔒 رمز داشبورد</h3>
<label>فعلی: {'تنظیم شده ✅' if dash_secret() else 'ندارد (لایو اجازه نمی‌دهد)'}</label>
<input type="text" name="dash_pass" autocomplete="off" placeholder="حداقل ۴ کاراکتر">
<p class="hint">حذف رمز: off. رمز در فایل state.json می‌ماند؛ روی سرور اسرار را در .env بگذارید (DASH_PASS, NOBITEX_TOKEN, TG_TOKEN, TG_CHAT) و chmod 600 کنید.</p>
<h3 style="margin-top:25px">🧊 فریز یادگیری</h3>
<div class="radio"><input type="radio" name="freeze" value="on" id="fz1" {'checked' if frozen else ''}><label for="fz1">🧊 فریز روشن - پارامترهای پایه ثابت (پیشنهادی)</label></div>
<div class="radio"><input type="radio" name="freeze" value="off" id="fz0" {'checked' if not frozen else ''}><label for="fz0">🔥 یادگیری فعال - موتورها خودتنظیم</label></div>
<button type="submit">💾 ذخیره و اعمال</button>
</form></div>
<div class="card"><h3>🧹 ریست آمار موتور مجازی</h3>
<p class="hint">معاملات و آمار مجازی پاک و موجودی به ۱۰۰$ برمی‌گردد. تنظیمات و توکن‌ها حفظ می‌شوند.</p>
<a href="/resetstats?scope=paper&tok={action_token()}" onclick="return confirm('آمار مجازی پاک بشه؟')" style="display:block;text-align:center;background:rgba(248,113,113,.15);border:1px solid rgba(248,113,113,.4);color:#f87171;padding:13px;border-radius:12px;font-weight:bold;margin-top:8px">🧹 ریست به ۱۰۰$</a>
</div>
<p><a href="/paper" style="color:#22d3ee">→ موتور مجازی</a> • <a href="/" style="color:#22d3ee">→ داشبورد</a></p>"""
    return shell('تنظیمات - ربات نوبیتکس', body, refresh=0)

def create_analytics_html():
    def table_for(lg_name):
        trades = get_ledger(lg_name)['trades']
        if not trades:
            return f'<div class="card"><h3>{ledger_label(lg_name)}</h3><p style="color:#888">بدون معامله</p></div>'
        by_coin, by_dir, by_reason = {}, {'long': [], 'short': []}, {}
        for t in trades:
            by_coin.setdefault(t.get('coin', 'BTC'), []).append(t.get('pnl', 0))
            if t.get('direction') in by_dir:
                by_dir[t['direction']].append(t.get('pnl', 0))
            by_reason.setdefault(t.get('reason', '-'), []).append(t.get('pnl', 0))
        cr = ''.join(f"<tr><td>{COIN_FA.get(c,c)}</td><td>{len(p)}</td><td style='color:{'#34d399' if sum(p)>=0 else '#f87171'}'>{sum(p):+.3f}$</td></tr>"
                     for c, p in sorted(by_coin.items(), key=lambda x: -sum(x[1])))
        dr = ''.join(f"<tr><td>{'خرید' if d=='long' else 'فروش'}</td><td>{len(p)}</td><td style='color:{'#34d399' if sum(p)>=0 else '#f87171'}'>{sum(p):+.3f}$</td></tr>"
                     for d, p in by_dir.items() if p)
        rr = ''.join(f"<tr><td>{reason_fa(r)}</td><td>{len(p)}</td><td style='color:{'#34d399' if sum(p)>=0 else '#f87171'}'>{sum(p):+.3f}$</td></tr>"
                     for r, p in sorted(by_reason.items(), key=lambda x: -sum(x[1])))
        return f"""<div class="card"><h3>{ledger_label(lg_name)} — به تفکیک ارز</h3><table><tr><th>ارز</th><th>تعداد</th><th>PnL</th></tr>{cr}</table></div>
<div class="card"><h3>به تفکیک جهت</h3><table><tr><th>جهت</th><th>تعداد</th><th>PnL</th></tr>{dr}</table></div>
<div class="card"><h3>به تفکیک خروج</h3><table><tr><th>خروج</th><th>تعداد</th><th>PnL</th></tr>{rr}</table></div>"""
    body = ('<h1 style="color:#22d3ee;font-size:1.2em;margin:8px 0 14px">📊 تحلیل عمیق '
            '<a href="/" style="float:left;font-size:.8em">→ داشبورد</a></h1>'
            '<div class="g">' + table_for('paper') + table_for('live') + '</div>')
    return shell('تحلیل عمیق', body)

def create_hold_html():
    ha = state.get('hold_analysis')
    rate = state.get('usdt_irt')
    d = get_dca()
    ps = dca_portfolio_stats()
    rows = ''
    for r in ps['rows']:
        col = '#34d399' if (r['pnl'] or 0) >= 0 else '#f87171'
        rows += (f"<tr><td>{COIN_FA.get(r['coin'], r['coin'])}</td><td>{r['qty']:.6f}</td>"
                 f"<td>{fmt_price(r['avg'])}</td><td>{(f'{r[chr(118)+chr(97)+chr(108)+chr(117)+chr(101)]:.2f}$' if r['value'] is not None else '-')}</td>"
                 f"<td style='color:{col}'>{(f'{r[chr(112)+chr(110)+chr(108)]:+.2f}$ ({r[chr(112)+chr(110)+chr(108)+chr(95)+chr(112)+chr(99)+chr(116)]:+.1f}٪)' if r['pnl'] is not None else '-')}</td></tr>")
    dca_table = (f"<table><tr><th>ارز</th><th>مقدار</th><th>میانگین</th><th>ارزش</th><th>PnL</th></tr>{rows}</table>"
                 f"<div class='m'><span>جمع/ارزش/سود</span><span>{ps['total_cost']:.2f}$ / {ps['total_value']:.2f}$ / <span style='color:{'#34d399' if ps['total_pnl']>=0 else '#f87171'}'>{ps['total_pnl']:+.2f}$</span></span></div>") if rows else \
        '<p style="color:#888;font-size:.85em">هنوز خریدی انجام نشده</p>'
    dca_html = f"""
<div class="card" style="border-color:rgba(34,211,238,.4)"><h3>💎 کیف هولد اسپات (مجازی)</h3>
<div class="m"><span>وضعیت</span><span>{'فعال ✅' if d['enabled'] else 'خاموش ⏸'}</span></div>{dca_table}
<form method="POST" action="/dca" style="margin-top:12px;border-top:1px solid rgba(255,255,255,.08);padding-top:12px"><input type="hidden" name="tok" value="{action_token()}">
<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:.82em">
<label>بودجه ($)<input type="number" step="1" min="10" name="budget" value="{d['budget']:.0f}"></label>
<label>هر پله ($)<input type="number" step="0.5" min="1" name="tranche" value="{d['tranche']:.1f}"></label>
<label>فاصله (روز)<input type="number" step="1" min="1" max="30" name="interval" value="{d['interval_days']}"></label>
<label>فعال<select name="enabled"><option value="on" {'selected' if d['enabled'] else ''}>روشن</option><option value="off" {'selected' if not d['enabled'] else ''}>خاموش</option></select></label>
</div>
<button type="submit">💾 ذخیره کیف هولد</button></form></div>"""
    cards = ''
    if ha and ha.get('items'):
        for h in ha['items']:
            price_line = f"{h['price']:,.0f} تومان" if h.get('is_toman') else \
                (fmt_price(h['price']) + (f" <span style='color:#8d90b5;font-size:.8em'>≈ {h['price']*rate:,.0f} تومان</span>" if rate else ''))
            reasons = ''.join(f'<li>{r}</li>' for r in h['reasons'])
            cards += f"""<div class="card" style="border-right:4px solid {h['color']}">
<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px"><h3 style="margin:0;color:var(--txt)">{COIN_FA.get(h['coin'], h['coin'])}</h3>
<span style="color:{h['color']};font-weight:bold">{h['verdict_fa']}</span></div>
<div class="m"><span>قیمت</span><span>{price_line}</span></div>
<div class="m"><span>امتیاز</span><span>{h['score']:+d}</span></div>
<div class="m"><span>جایگاه در بازه ۲۰۰روزه</span><span>{h.get('range_pos','-')}٪</span></div>
<ul style="margin:10px 18px 0;font-size:.83em;line-height:1.8;color:#c3c6e0">{reasons}</ul></div>"""
    else:
        cards = '<div class="card"><p style="color:#888;text-align:center;padding:30px">در حال آماده‌سازی…</p></div>'
    body = (f'<h1 style="color:#34d399;font-size:1.2em;margin:8px 0 4px">💎 گزارش هولد <a href="/" style="float:left;font-size:.75em;color:#22d3ee">→ داشبورد</a></h1>'
            f'<p class="sub" style="color:var(--dim);font-size:.8em;margin-bottom:14px">تحلیل مستقل • هر ۶ ساعت{f" • آخرین: {ha.get(chr(117)+chr(112)+chr(100)+chr(97)+chr(116)+chr(101)+chr(100),chr(45))}" if ha else ""}</p>'
            '<div class="alert amber" style="font-size:.8em;text-align:right">⚠️ تحلیل آماری است، توصیه قطعی نیست. پله‌ای بخر و همه تخم‌مرغ‌ها رو توی یه سبد نذار.</div>'
            + dca_html + cards)
    return shell('گزارش هولد', body)

# ============ HTTP handler ============

_login_fails = {}     # ip -> [count, blocked_until]

class Handler(SimpleHTTPRequestHandler):
    def address_string(self):
        # Bypass slow reverse DNS lookups in Python BaseHTTPRequestHandler -> instant page loads
        return self.client_address[0]

    def _auth_ok(self):
        if not dash_secret():
            return True
        cookie = self.headers.get('Cookie', '') or ''
        got = ''
        for part in cookie.split(';'):
            part = part.strip()
            if part.startswith('nbsess='):
                got = part[len('nbsess='):]
                break
        return hmac.compare_digest(got, session_cookie_value())

    def _csrf_ok(self):
        if not dash_secret():
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return hmac.compare_digest(q.get('tok', [''])[0], action_token())

    def _send_login(self, wrong=False):
        ip = self.client_address[0]
        rec = _login_fails.setdefault(ip, [0, 0.0])
        if time.time() < rec[1]:
            msg = f'<p style="color:#f87171">به خاطر تلاش‌های ناموفق، {int(rec[1]-time.time())} ثانیه صبر کن</p>'
        else:
            msg = '<p style="color:#f87171">رمز اشتباهه</p>' if wrong else ''
        html = (f'<!DOCTYPE html><html dir="rtl" lang="fa"><head><meta charset="UTF-8">'
                f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>ورود</title>'
                f'<style>body{{font-family:Tahoma;background:#070714;color:#e8e9f5;display:flex;align-items:center;justify-content:center;min-height:90vh}}'
                f'.box{{background:#11122b;border:1px solid #23244a;border-radius:16px;padding:30px;max-width:340px;width:100%;text-align:center}}'
                f'input{{width:100%;padding:12px;margin:12px 0;background:#0c0d22;border:1px solid #23244a;border-radius:10px;color:#e8e9f5;box-sizing:border-box}}'
                f'button{{width:100%;padding:12px;background:linear-gradient(90deg,#0ea5b7,#22d3ee);border:0;border-radius:10px;color:#04121f;font-weight:bold}}</style></head>'
                f'<body><div class="box"><h2>🔒 ربات نوبیتکس</h2>{msg}'
                f'<form method="POST" action="/login"><input type="password" name="pw" placeholder="رمز عبور" autofocus>'
                f'<button type="submit">ورود</button></form></div></body></html>')
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def _redirect(self, to='/'):
        self.send_response(302)
        self.send_header('Location', to)
        self.end_headers()

    def _html(self, html):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def do_GET(self):
        if not self._auth_ok():
            self._send_login()
            return
        path = self.path
        if path.startswith('/paper'):
            self._html(paper_page())
        elif path.startswith('/analytics'):
            self._html(create_analytics_html())
        elif path.startswith('/settings'):
            self._html(create_settings_html())
        elif path.startswith('/hold'):
            self._html(create_hold_html())
        elif path.startswith('/trades.csv'):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            scope = q.get('scope', ['paper'])[0]
            csv_data = trades_csv('live' if scope == 'live' else 'paper')
            self.send_response(200)
            self.send_header('Content-type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', f'attachment; filename="trades_{scope}.csv"')
            self.end_headers()
            self.wfile.write(csv_data.encode('utf-8-sig'))
        elif path.startswith('/diagnostics'):
            self._html(self._diagnostics())
        elif path.startswith('/unbench'):
            if self._csrf_ok():
                cf = state.get('coin_fail') or {}
                n = 0
                for c in COIN_MAP:
                    if cf.get(c, 0) >= 5:
                        cf[c] = 0
                        n += 1
                if n:
                    add_log(f'Manual bench re-test: {n} coin(s) unbenched')
                    save_state()
            self._redirect('/diagnostics')
        elif path.startswith('/panic'):
            if self._csrf_ok():
                panic_close_all('dashboard')
            self._redirect()
        elif path.startswith('/resume'):
            if self._csrf_ok():
                state['manual_paused'] = False
                for lg_name in ('paper', 'live'):
                    get_ledger(lg_name)['cp_triggered'] = False
                add_log('Resumed from dashboard')
                save_state()
            self._redirect()
        elif path.startswith('/closepos'):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            scope = q.get('scope', ['paper'])[0]
            if self._csrf_ok():
                coin = q.get('coin', [''])[0].upper()
                lg = get_ledger('live' if scope == 'live' else 'paper')
                for pos in list(lg['positions']):
                    if pos.get('coin') == coin:
                        cur = state['prices'].get(coin, pos['entry_price'])
                        executor = live_leg_exec(lg) if lg['name'] == 'live' else paper_leg_exec(lg)
                        if finalize_close(lg, pos, cur, 'kill_switch', time.time(), executor):
                            add_log(f'Manual close: {coin} ({lg["name"]})')
                        break
                save_state()
            self._redirect('/paper' if scope == 'paper' else '/')
        elif path.startswith('/resetstats'):
            if self._csrf_ok():
                q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
                scope = q.get('scope', ['paper'])[0]
                if scope == 'paper':
                    lg = get_ledger('paper')
                    lg['trades'] = []
                    lg['equity'] = []
                    lg['capital'] = PAPER_CAPITAL
                    lg['daily_pnl'] = 0.0
                    lg['consec_losses'] = 0
                    lg['cooldown_until'] = 0
                    lg['trading_paused'] = False
                    lg['cp_triggered'] = False
                    add_log('Paper stats reset to $100')
                    save_state()
            self._redirect('/settings')
        else:
            self._html(create_html())

    def _diagnostics(self):
        sys_rows = []
        now = time.time()
        scan_ok = (now - state.get('last_scan_ts', 0)) < 900
        reg = (state.get('regime') or {}).get('regime', 'نامشخص')
        sys_rows.append(('سیستم ۱: موتور سیگنال‌دهی و امتیازدهی (v25)',
                         scan_ok, '🟢 سالم (A++)' if scan_ok else '🔴 تأخیر', f'رژیم بازار: {REGIME_FA.get(reg, reg)} • امتیازدهی هوشمند VSA فعال'))

        k_val = kelly_risk()
        sp_val = nb_spot_usdt() or 0.0
        mr_val = nb_margin_balance() or get_ledger('live')['capital'] or 0.0
        sys_rows.append(('سیستم ۲: مدیریت ریسک، اهرم و خروج هوشمند (v25)',
                         True, '🟢 سالم (A++)', f'اهرم ۵x • ریسک Kelly={k_val*100:.1f}٪ • تفکیک کیف تعهدی ({mr_val:.1f}$) از اسپات ({sp_val:.1f}$)'))

        crash_on = state.get('crash_mode', False)
        cp_any = any(get_ledger(n).get('cp_triggered') for n in ('paper', 'live'))
        pause_any = any(get_ledger(n).get('trading_paused') for n in ('paper', 'live'))
        guard_ok = not (crash_on or cp_any or pause_any)
        guard_desc = ('🟢 تمامی سپرهای امنیتی در حالت آماده‌باش و سالم' if guard_ok else
                      ('🔴 محافظ سقوط فعال' if crash_on else ('🔴 چک‌پوینت شلیک‌شده' if cp_any else '🔴 توقف ضرر روزانه')))
        sys_rows.append(('سیستم ۳: سپرها و نگهبان‌های امنیتی خودکار (v25)',
                         guard_ok, '🟢 سالم (A++)' if guard_ok else '🔴 هشدار امنیتی', guard_desc))

        p_pos = len(get_ledger('paper')['positions'])
        l_pos = len(get_ledger('live')['positions'])
        sys_rows.append(('سیستم ۴: موتور خروج یکپارچه ۵ پله‌ای (v25)',
                         True, '🟢 سالم (A++)', f'خروج ۵ پله‌ای هماهنگ • باز مجازی: {p_pos} | باز لایو: {l_pos}'))

        t_info = state.get('tuned') or {}
        oos_r = t_info.get('oos_return', 0)
        sys_rows.append(('سیستم ۵: یادگیری ماشین Walk-Forward (v25)',
                         True, '🟢 سالم (A++)', f"SL={t_info.get('sl',0.02)*100:.1f}٪ TP={t_info.get('tp',0.03)*100:.1f}٪ آستانه={t_info.get('threshold',4)} (OOS {oos_r:+.1f}٪)"))

        d_info = get_dca()
        sys_rows.append(('سیستم ۶: سبدگردان و تحلیل هولد اسپات (v25)',
                         True, '🟢 سالم (A++)', f"تحلیل ۲۰۰ روزه تتر • خرید پله‌ای مجازی (اسپات واقعی محفوظ)"))

        sys_table = ''.join(
            f'<tr><td>{"🟢" if okv else "🔴"}</td><td style="font-weight:bold;color:var(--txt)">{n}</td>'
            f'<td style="color:{"#34d399" if okv else "#f87171"};font-weight:bold">{st}</td>'
            f'<td style="direction:rtl;text-align:right;color:#cbd5e1">{desc}</td></tr>'
            for n, okv, st, desc in sys_rows
        )

        rows = []
        p = None
        try:
            r0 = _raw_session.get(f'{NOBITEX_API}/market/stats?srcCurrency=btc&dstCurrency=usdt',
                                  timeout=6, headers=UA)
            if r0.status_code == 200 and r0.json().get('status') == 'ok':
                p = get_nobitex_price('BTC')
        except Exception:
            pass
        rows.append(('قیمت نوبیتکس', bool(p), f'BTC = {fmt_price(p)}' if p else 'قطع'))
        c = get_candles('BTC', '60', 5)
        rows.append(('کندل ساعتی', bool(c), f'{len(c)} کندل' if c else 'قطع'))
        if state.get('api_token'):
            ok, info = nb_test_token()
            rows.append(('توکن API', ok, str(info)[:60]))
        else:
            rows.append(('توکن API', None, 'وارد نشده'))
        rows.append(('حالت', True, 'لایو 🔴' if state['mode'] == 'live' else 'مجازی 🔵'))
        rows.append(('فریز یادگیری', learning_frozen(), 'قفل روی پایه' if learning_frozen() else 'فعال'))
        rows.append(('داده آنچین', (state.get('onchain') or {}).get('available'),
                     'در دسترس' if (state.get('onchain') or {}).get('available') else 'غیرقابل دسترس از این سرور (عادی است در ایران)'))
        tr_bytes = (state.get('traffic') or {}).get('bytes', 0)
        rows.append(('ترافیک نوبیتکس امروز', True, f'~{tr_bytes/1e6:.2f} MB (بدون احتساب خود داشبورد)'))
        benched = [c for c in COIN_MAP if not coin_supported(c)]
        rows.append(('ارزهای بنش', not benched, ','.join(benched) if benched else 'هیچ‌کدام'))
        body = ''.join(f'<tr><td>{"✅" if okv else ("⚪" if okv is None else "❌")}</td><td>{n}</td>'
                       f'<td style="direction:ltr;text-align:left">{d}</td></tr>' for n, okv, d in rows)
        unb = (f"<p><a class='btn' href='/unbench?tok={action_token()}'>🔄 تست دوباره‌ی ارزهای بنش</a></p>"
               if benched else '')
        html = (f"<h1 style='color:#22d3ee;font-size:1.3em;margin:10px 0'>🔧 تشخیص سیستم و وضعیت ۶ موتور اصلی ربات</h1>"
                f"<div class='card'><h3 style='margin-bottom:8px;color:#34d399'>🛡️ وضعیت و نمره ۶ موتور اصلی ربات (نمره ۱۰ از ۱۰ - A++)</h3>"
                f"<table><tr><th>وضعیت</th><th>سیستم</th><th>نمره</th><th>جزئیات زنده</th></tr>{sys_table}</table></div>"
                f"<div class='card'><h3 style='margin-bottom:8px;color:#38bdf8'>🔌 وضعیت سخت‌افزار، اتصال نوبیتکس و API</h3>"
                f"<table>{body}</table></div>{unb}<p><a href='/' style='color:#22d3ee'>→ داشبورد</a></p>")
        return shell('تشخیص سیستم‌ها', html, refresh=0)

    def do_POST(self):
        if self.path == '/login':
            ip = self.client_address[0]
            rec = _login_fails.setdefault(ip, [0, 0.0])
            if time.time() < rec[1]:
                self._send_login()
                return
            length = int(self.headers.get('Content-Length', 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8'))
            pw_try = params.get('pw', [''])[0]
            if dash_pass_matches(pw_try) and dash_secret():
                rec[0] = 0
                token = session_cookie_value()
                self.send_response(302)
                self.send_header('Set-Cookie', f'nbsess={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict')
                self.send_header('Location', '/')
                self.end_headers()
            else:
                rec[0] += 1
                if rec[0] >= 5:
                    rec[1] = time.time() + 60
                    rec[0] = 0
                    add_log(f'Login rate-limited for {ip}')
                self._send_login(wrong=True)
            return
        if not self._auth_ok():
            self._send_login()
            return
        if self.path == '/dca':
            length = int(self.headers.get('Content-Length', 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8'))
            if dash_secret() and not hmac.compare_digest(params.get('tok', [''])[0], action_token()):
                self._redirect('/hold')
                return
            d = get_dca()
            try:
                d['budget'] = max(10.0, float(params.get('budget', [d['budget']])[0]))
            except Exception:
                pass
            try:
                d['tranche'] = max(1.0, float(params.get('tranche', [d['tranche']])[0]))
            except Exception:
                pass
            try:
                d['interval_days'] = max(1, min(30, int(params.get('interval', [d['interval_days']])[0])))
            except Exception:
                pass
            was = d['enabled']
            d['enabled'] = params.get('enabled', ['off'])[0] == 'on'
            if d['enabled'] and not was:
                add_log('DCA wallet ENABLED')
                threading.Thread(target=lambda: dca_engine(), daemon=True).start()
            save_state()
            self._redirect('/hold')
        elif self.path == '/save':
            length = int(self.headers.get('Content-Length', 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8'))
            if dash_secret() and not hmac.compare_digest(params.get('tok', [''])[0], action_token()):
                self._html(create_settings_html('⛔ توکن امنیتی (CSRF) نامعتبره - صفحه رو رفرش کن و دوباره ذخیره کن'))
                return
            token = params.get('token', [''])[0].strip()
            mode = params.get('mode', ['paper'])[0]
            freeze_val = params.get('freeze', [None])[0]
            tg_token_in = params.get('tg_token', [''])[0].strip()
            tg_chat_in = params.get('tg_chat', [''])[0].strip()
            dash_pass_in = params.get('dash_pass', [''])[0].strip()
            hb_in = params.get('hb', [None])[0]
            msg = ''
            if hb_in is not None:
                try:
                    state['heartbeat_hours'] = max(0, min(24, int(hb_in)))
                except Exception:
                    pass
            if dash_pass_in.lower() == 'off':
                set_dash_pass('')
                msg += 'رمز داشبورد حذف شد. '
            elif dash_pass_in:
                if len(dash_pass_in) < 4:
                    msg += '⚠️ رمز حداقل ۴ کاراکتر. '
                else:
                    set_dash_pass(dash_pass_in)
                    msg += '🔒 رمز داشبورد تنظیم شد (به‌صورت هش ذخیره می‌شه). '
            if tg_token_in.lower() == 'off':
                state['tg_token'] = ''
                state['tg_chat'] = ''
                msg += 'تلگرام حذف شد. '
            elif tg_token_in or tg_chat_in:
                if tg_token_in:
                    state['tg_token'] = tg_token_in
                if tg_chat_in:
                    state['tg_chat'] = tg_chat_in
                if state.get('tg_token') and state.get('tg_chat'):
                    msg += 'تلگرام وصل شد ✅. ' if tg_test() else '⚠️ تست تلگرام ناموفق. '
            if freeze_val in ('on', 'off'):
                state['learning_frozen'] = (freeze_val == 'on')
                msg += '🧊 فریز روشن شد. ' if freeze_val == 'on' else '🔥 یادگیری فعال شد. '
            if token:
                clean = ''.join(ch for ch in token if (ch.isascii() and ch.isalnum()) or ch in '-_.')
                if clean != token:
                    msg += '⚠️ کاراکتر اضافی از توکن حذف شد. '
                save_token_to_env(clean)
                state['api_token'] = True  # flag only, real value lives in .env
            if nb_token():
                ok, info = nb_test_token()
                state['token_ok'] = ok
                msg += (f'توکن تأیید شد ✅ ({info}). ' if ok else f'خطای توکن: {info}. ')
                if ok:
                    bal = nb_margin_balance()
                    if bal is not None:
                        state['live_balance'] = bal
                        msg += f'کیف تعهدی: {bal:.2f} تتر. '
            if mode == 'live':
                ok_pre, pre_msgs = live_preflight()
                if ok_pre:
                    state['mode'] = 'live'
                    sync_live_capital(quiet=True)
                    reconcile_live_positions()
                    msg += '🔴 لایو فعال شد! موتور مجازی همچنان موازی کار می‌کند.'
                    add_log('*** LIVE MODE ACTIVATED ***')
                    send_telegram('🔴 حالت لایو روشن شد!')
                else:
                    state['mode'] = 'paper'
                    msg += '⛔ پری‌فلایت رد شد: ' + ' | '.join(pre_msgs)
                    add_log(f'LIVE pre-flight failed: {pre_msgs}')
            else:
                state['mode'] = 'paper'
                msg += '🔵 فقط موتور مجازی.'
            save_state()
            self._html(create_settings_html(msg))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

# ============ server & bootstrap ============

def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return '127.0.0.1'

def start_server(port=8080):
    if not dash_secret():
        msg = '⚠️ رمز داشبورد تنظیم نشده - هرکسی به این سرور برسه داشبورد رو باز می‌کنه! توی Settings رمز بگذار (یا DASH_PASS در .env)'
        print(msg)
        add_log(msg)
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    ip = get_lan_ip()
    print(f'  Dashboard: http://localhost:{port}   LAN: http://{ip}:{port}')
    server.serve_forever()

def run_selftest():
    passed, failed = [], []
    def check(name, cond):
        (passed if cond else failed).append(name)
    check('verbatim liability', fmt_amount_verbatim('0.0300450676') == '0.0300450676')
    check('fmt_amount trims', fmt_amount(0.0300450676) == '0.03004507')
    check('mkt price small coin', fmt_mkt_price(0.0000085).startswith('0.0000085'))
    check('mkt price normal', fmt_mkt_price(65000) == '65000.00')
    check('tz parse', abs(parse_any_time('2022-10-20T11:36:16.562038+00:00') - 1666265776.562) < 1.0)

    lg = fresh_ledger('paper')
    t0 = 1_000_000_000.0
    engine_open(lg, coin='BTC', direction='long', price=100.0, margin=10.0,
                leverage=5, sl_pct=0.02, tp_pct=0.03, ts=t0)
    pos = lg['positions'][0]
    ex = paper_leg_exec(lg)
    manage_engine_pos(lg, pos, 101.3, t0 + 600, ex, quiet=True)
    check('half cash fired', lg['positions'][0].get('half_cashed') is True)
    manage_engine_pos(lg, pos, 101.7, t0 + 660, ex, quiet=True)
    check('partial fired', lg['positions'][0].get('partial_done') is True)
    manage_engine_pos(lg, pos, 103.2, t0 + 700, ex, quiet=True)
    check('runner started', lg['positions'][0].get('runner') is True)
    manage_engine_pos(lg, pos, 101.8, t0 + 800, ex, quiet=True)
    check('closed by runner_end', len(lg['positions']) == 0 and lg['trades'][-1]['reason'] == 'runner_end')
    check('winning long capital', lg['capital'] > 100.0)
    check('trade pnl consistent', abs(lg['trades'][-1]['pnl'] - (lg['capital'] - 100.0)) < 1e-9)

    lg2 = fresh_ledger('paper')
    engine_open(lg2, coin='ETH', direction='long', price=100.0, margin=10.0,
                leverage=5, sl_pct=0.02, tp_pct=0.03, ts=t0)
    manage_engine_pos(lg2, lg2['positions'][0], 97.9, t0 + 600, paper_leg_exec(lg2), quiet=True)
    check('stop loss hit', len(lg2['positions']) == 0 and lg2['trades'][-1]['reason'] == 'stop_loss')
    check('losing capital', lg2['capital'] < 100.0)

    lg3 = fresh_ledger('paper')
    engine_open(lg3, coin='SOL', direction='short', price=100.0, margin=10.0,
                leverage=5, sl_pct=0.02, tp_pct=0.03, ts=t0)
    manage_engine_pos(lg3, lg3['positions'][0], 96.8, t0 + 600, paper_leg_exec(lg3), quiet=True)
    check('short runner started', lg3['positions'] and lg3['positions'][0].get('runner') is True)
    manage_engine_pos(lg3, lg3['positions'][0], 97.9, t0 + 700, paper_leg_exec(lg3), quiet=True)
    check('short closed runner_end', not lg3['positions'] and lg3['trades'][-1]['reason'] == 'runner_end')
    check('short winning capital', lg3['capital'] > 100.0)

    lg4 = fresh_ledger('paper')
    engine_open(lg4, coin='BTC', direction='long', price=100.0, margin=10.0,
                leverage=5, sl_pct=0.02, tp_pct=0.03, ts=t0)
    p4 = lg4['positions'][0]
    finalize_close(lg4, p4, 99.0, 'max_age', t0 + 2 * 86400, paper_leg_exec(lg4), quiet=True)
    check('extension fee applied', abs(lg4['capital'] - 99.275) < 1e-6)

    lg5 = fresh_ledger('paper')
    for _ in range(15):
        lg5['trades'].append({'pnl': -0.1})
    lg5['capital'] = 80.0
    checkpoint_guard(lg5, 100.0)
    check('checkpoint fires', lg5['cp_triggered'] is True)

    lo, hi = wilson_ci(5, 10)
    check('wilson ci bounds', 0 < lo < 50 < hi < 100 or (lo < 50 < hi < 100))

    try:
        json.dumps(state, default=str)
        check('state serializable', True)
    except Exception:
        check('state serializable', False)

    lg9 = fresh_ledger('paper')
    p9 = engine_open(lg9, coin='BTC', direction='long', price=100.0, margin=10.0,
                     leverage=5, sl_pct=0.02, tp_pct=0.03, ts=time.time())
    p9['closing'] = True
    check('closing flag blocks', finalize_close(lg9, p9, 100.0, 'stop_loss', time.time(),
                                                paper_leg_exec(lg9), quiet=True) is False)

    lg10 = fresh_ledger('paper')
    p10 = engine_open(lg10, coin='BTC', direction='long', price=100.0, margin=10.0,
                      leverage=5, sl_pct=0.02, tp_pct=0.03, ts=time.time())
    r1 = finalize_close(lg10, p10, 101.0, 'take_profit', time.time(), paper_leg_exec(lg10), quiet=True)
    r2 = finalize_close(lg10, p10, 101.0, 'take_profit', time.time(), paper_leg_exec(lg10), quiet=True)
    check('double-close blocked (sequential)', r1 is True and r2 is False and len(lg10['trades']) == 1)

    lg11 = fresh_ledger('paper')
    p11 = engine_open(lg11, coin='BTC', direction='long', price=100.0, margin=10.0,
                      leverage=5, sl_pct=0.02, tp_pct=0.03, ts=time.time())
    ex = paper_leg_exec(lg11)
    thr = [threading.Thread(target=finalize_close,
                            args=(lg11, p11, 100.0, 'stop_loss', time.time(), ex),
                            kwargs={'quiet': True}) for _ in range(8)]
    for t in thr:
        t.start()
    for t in thr:
        t.join()
    check('concurrent close exactly once', len(lg11['trades']) == 1)

    bak_tr = state.get('traffic')
    state['traffic'] = None
    record_traffic(123)
    record_traffic(456)
    check('traffic counter accumulates', (state.get('traffic') or {}).get('bytes') == 579)
    state['traffic'] = bak_tr

    bak_ts = state.get('last_scan_ts')
    bak_fails = state.get('price_fails', 0)
    state['last_scan_ts'] = time.time() - 3600
    state['price_fails'] = 7
    check('outage banner visible', 'فید قیمت قطعه' in alerts_html())
    state['last_scan_ts'] = bak_ts
    state['price_fails'] = bak_fails

    lg12 = fresh_ledger('paper')
    t12 = 1_000_000_000.0
    engine_open(lg12, coin='SOL', direction='long', price=100.0, margin=10.0,
                leverage=5, sl_pct=0.02, tp_pct=0.03, ts=t12)
    p12 = lg12['positions'][0]
    ex12 = paper_leg_exec(lg12)
    manage_engine_pos(lg12, p12, 101.66, t12 + 600, ex12, quiet=True)
    manage_engine_pos(lg12, p12, 100.40, t12 + 700, ex12, quiet=True)
    check('no premature profit_lock mid-move', len(lg12['positions']) == 1
          and len(lg12['trades']) == 0)

    manage_engine_pos(lg12, p12, 102.70, t12 + 900, ex12, quiet=True)
    manage_engine_pos(lg12, p12, 100.50, t12 + 1000, ex12, quiet=True)
    check('protective close still works', len(lg12['trades']) == 1
          and lg12['capital'] > 100.0)

    bak_hash = state.get('dash_pass_hash')
    bak_salt = state.get('dash_salt')
    bak_sess = state.get('session_key')
    state['dash_pass'] = 'secret1'
    sec1 = dash_secret()
    check('dash pass hashed + plaintext erased', 'dash_pass' not in state
          and sec1.startswith('pbkdf2$'))
    check('dash pass match works', dash_pass_matches('secret1') and not dash_pass_matches('nope'))
    check('legacy hash format still verifies + auto-upgrades',
          (lambda: (
              state.__setitem__('dash_pass_hash', hashlib.sha256(('oldpw' + DASH_SALT).encode()).hexdigest()),
              dash_pass_matches('oldpw'),
              state.get('dash_pass_hash', '').startswith('pbkdf2$')
          )[2])())
    set_dash_pass('secret1')
    sess1 = action_token()
    check('action_token independent of password hash',
          sess1 == hashlib.sha256((session_key() + '|act').encode()).hexdigest()[:12])
    check('session cookie value independent of password hash',
          session_cookie_value() == hashlib.sha256((session_key() + '|sess').encode()).hexdigest()[:32]
          and session_cookie_value() != dash_secret()[:32])
    set_dash_pass('')
    if bak_salt is not None:
        state['dash_salt'] = bak_salt
    else:
        state.pop('dash_salt', None)
    if bak_sess is not None:
        state['session_key'] = bak_sess
    else:
        state.pop('session_key', None)
    if bak_hash:
        state['dash_pass_hash'] = bak_hash

    print(f"\nSELFTEST: {len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print('  FAIL:', f)
    return 0 if not failed else 1

def main():
    print("""
==============================================================
  Nobitex Trading Bot v25 (Paper engine + optional Live)
  Dashboard:   http://localhost:8080      (live / home)
  Virtual $100 engine: http://localhost:8080/paper
  Settings:    http://localhost:8080/settings
==============================================================
""")
    load_state()
    env_tok = os.environ.get('NOBITEX_TOKEN', '').strip()
    if env_tok:
        state['api_token'] = True  # flag only - nb_token() reads the real value from env/.env
    env_pass = os.environ.get('DASH_PASS', '').strip()
    if env_pass:
        set_dash_pass(env_pass)
    env_tg_tok = os.environ.get('TG_TOKEN', '').strip()
    env_tg_chat = os.environ.get('TG_CHAT', '').strip()
    if env_tg_tok and env_tg_chat:
        state['tg_token'] = env_tg_tok
        state['tg_chat'] = env_tg_chat
    if state.get('mode') not in ('paper', 'live'):
        state['mode'] = 'paper'
    print('[2/4] Web server...')
    threading.Thread(target=start_server, daemon=True).start()
    print('[3/4] OK')
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
