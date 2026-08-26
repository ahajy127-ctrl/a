#!/usr/bin/env python3
"""TITAN - Multi-Strategy Engine"""
import numpy as np

STRATS = {'rsi': {'n': 'RSI', 'w': 1.0}, 'trend': {'n': 'Trend', 'w': 0.8}, 'breakout': {'n': 'Breakout', 'w': 0.4}}

def detect_regime(gcc, coin='BTC'):
    c = gcc(coin, '60', 48, True)
    if not c or len(c) < 30: return 'ranging'
    a = np.array(c, dtype=float)
    r = np.diff(a) / a[:-1]
    vol = float(np.std(r[-24:]))
    seg = a[-24:]
    slope = float(np.polyfit(np.arange(24), seg, 1)[0])
    tr = slope * 24 / float(np.mean(seg))
    net = abs(seg[-1] - seg[0])
    path = float(np.sum(np.abs(np.diff(seg)))) or 1
    eff = net / path
    if vol > 0.012: return 'volatile'
    if eff > 0.35 and tr > 0.005: return 'trending_up'
    if eff > 0.35 and tr < -0.005: return 'trending_down'
    return 'ranging'

def _rsi(c, coin):
    if not c or len(c) < 20: return None
    p = c[-20:]; g, l = [], []
    for i in range(1, len(p)):
        ch = p[i] - p[i-1]
        g.append(max(0, ch)); l.append(max(0, -ch))
    ag = np.mean(g[-14:]) if g else 0
    al = np.mean(l[-14:]) if l else 0
    rsi = 100 - 100 / (1 + ag / max(al, 1e-9)) if ag + al > 1e-12 else 50
    mom = (p[-1] - p[-5]) / max(p[-5], 1e-9) if len(p) > 5 else 0
    sc, dr = 0, None
    if rsi < 35:
        sc = 3 + (35 - rsi) / 5
        if p[-1] > p[-2]: dr = 'long'
        elif mom > 0: dr = 'long'
    elif rsi > 65:
        sc = 3 + (rsi - 65) / 5
        if p[-1] < p[-2]: dr = 'short'
        elif mom < 0: dr = 'short'
    if dr == 'long' and mom < -0.01: sc -= 2
    if dr == 'short' and mom > 0.01: sc -= 2
    if sc >= 3 and dr: return {'s': round(sc, 1), 'd': dr, 'st': 'rsi', 'c': min(0.8, 0.4 + sc * 0.06)}
    return None

def _trend(c, coin, c4=None):
    if not c or len(c) < 30: return None
    a = np.array(c, dtype=float)
    s20 = np.mean(a[-20:])
    s50 = np.mean(a[-50:]) if len(a) >= 50 else s20
    s100 = np.mean(a[-100:]) if len(a) >= 100 else s20
    sc, dr = 0, None
    c4_bonus = 0.0
    # 4h confirmation bonus (computed BEFORE score so it isn't overwritten)
    if c4 and len(c4) >= 10:
        try:
            c4_arr = np.array(c4[-10:], dtype=float)
            if s20 > s50 and c4_arr[-1] > c4_arr[0]:
                c4_bonus = 0.5
            elif s20 < s50 and c4_arr[-1] < c4_arr[0]:
                c4_bonus = 0.5
        except Exception:
            pass
    if s20 > s50:
        sc = min(5, (s20 - s100) / max(abs(s100), 1) * 1000) + c4_bonus; dr = 'long'
    elif s20 < s50:
        sc = min(5, abs(s20 - s100) / max(abs(s100), 1) * 1000) + c4_bonus; dr = 'short'
    
    if sc >= 3 and dr: return {'s': round(sc, 1), 'd': dr, 'st': 'trend', 'c': min(0.8, 0.3 + sc * 0.08)}
    return None

def _breakout(c, coin):
    if not c or len(c) < 20: return None
    r = c[-20:]; lo, hi = min(r), max(r); cur = r[-1]
    mom = (cur - r[-5]) / max(r[-5], 1e-9) if len(r) > 5 else 0
    sc, dr = 0, None
    if cur >= hi * 1.01:
        sc = min(5, (cur - hi) / hi * 200)
        if mom > 0.01: sc += 2
        dr = 'long'
    elif cur <= lo * 0.99:
        sc = min(5, (lo - cur) / lo * 200)
        if mom < -0.01: sc += 2
        dr = 'short'
    if sc >= 4 and dr: return {'s': round(sc, 1), 'd': dr, 'st': 'breakout', 'c': min(0.75, 0.3 + sc * 0.07)}
    return None

_FNS = [_rsi, _trend, _breakout]
_BOOST = {'rsi': ['ranging', 'volatile'], 'trend': ['trending_up', 'trending_down'], 'breakout': ['volatile']}

def titan_scan(gcc, coin, COIN_FA, regime=None, prefer=None):
    c = gcc(coin, '60', 100, True)
    # Try 4h data for trend confirmation
    c4 = gcc(coin, '240', 30, True)
    if not c: return None
    if not regime: regime = 'ranging'
    sigs = []
    for fn in sorted(_FNS, key=lambda f: 0 if prefer and str(f.__name__).replace('_','') == prefer else 1):
        try:
            if fn.__name__ == '_trend':
                sig = fn(c, coin, c4)
            else:
                sig = fn(c, coin)
            if sig:
                _r = sig.get('regime', regime)
                if _r in _BOOST.get(sig['st'], []): sig['s'] += 1
                sigs.append(sig)
        except: pass
    if not sigs: return None
    sigs.sort(key=lambda x: x['s'], reverse=True)
    b = sigs[0]
    return {'coin': coin, 'direction': b['d'], 'score': b['s'], 'confidence': b['c'],
            'strategy': b['st'], 'reasons': [b['st'] + ' s=' + str(b['s'])], 'factors': [b['st']]}

if __name__ == '__main__':
    print('TITAN OK')
