#!/usr/bin/env python3
"""
ULTIMATE Module - Strategy Matrix + Neural Overlord + War Room
"""
import numpy as np
from collections import defaultdict

STRATEGIES = {
    'rsi_mr':     {'weight': 1.0, 'pf': 1.0, 'trades': 0, 'label': 'بازگشتی RSI'},
    'trend':      {'weight': 0.5, 'pf': 1.0, 'trades': 0, 'label': 'روندی'},
    'breakout':   {'weight': 0.2, 'pf': 1.0, 'trades': 0, 'label': 'شکست'},
}

def matrix_score(coin, get_candles_cached, COIN_FA):
    c = get_candles_cached(coin, '60', 25, drop_forming=True)
    if not c or len(c) < 20:
        return None
    c20 = c[-20:]
    gains, losses = [], []
    for i in range(1, len(c20)):
        ch = c20[i] - c20[i-1]
        gains.append(max(0, ch))
        losses.append(max(0, -ch))
    ag = np.mean(gains[-14:]) if gains else 0
    al = np.mean(losses[-14:]) if losses else 0
    rsi = 100 - 100 / (1 + ag / max(al, 1e-9)) if (ag + al) > 1e-12 else 50
    mom = (c20[-1] - c20[-5]) / max(c20[-5], 1e-9) if len(c20) > 5 else 0
    sma7 = np.mean(c20[-7:])
    sma20 = np.mean(c20[-20:])
    best_score, best_dir, best_strat = 0, None, None
    if rsi < 38:
        s = max(0, (38 - rsi) / 5)
        if s > best_score: best_score, best_dir, best_strat = s, 'long', 'rsi_mr'
    elif rsi > 62:
        s = max(0, (rsi - 62) / 5)
        if s > best_score: best_score, best_dir, best_strat = s, 'short', 'rsi_mr'
    trend = (sma7 - sma20) / max(sma20, 1e-9)
    if trend > 0.003:
        s = min(5, trend * 1000)
        if s > best_score: best_score, best_dir, best_strat = s, 'long', 'trend'
    elif trend < -0.003:
        s = min(5, abs(trend) * 1000)
        if s > best_score: best_score, best_dir, best_strat = s, 'short', 'trend'
    if mom > 0.02:
        s = min(5, mom * 100)
        if s > best_score: best_score, best_dir, best_strat = s, 'long', 'breakout'
    elif mom < -0.02:
        s = min(5, abs(mom) * 100)
        if s > best_score: best_score, best_dir, best_strat = s, 'short', 'breakout'
    if best_score >= 2 and best_dir:
        lbl = STRATEGIES.get(best_strat, {}).get('label', best_strat)
        return {
            'coin': coin, 'direction': best_dir, 'score': round(best_score, 1),
            'strategy': best_strat, 'confidence': min(0.8, 0.3 + best_score * 0.08),
            'reasons': [f'{lbl} score={best_score:.1f}'],
            'factors': [best_strat]
        }
    return None

def overlord_nightly(state, get_ledger, add_log, send_telegram):
    try:
        m = state.get('strategy_matrix')
        if not m:
            state['strategy_matrix'] = {k: dict(v) for k, v in STRATEGIES.items()}
            m = state['strategy_matrix']
        trades = get_ledger('paper').get('trades', [])[-100:]
        perf = defaultdict(list)
        for t in trades:
            for f in t.get('factors', []):
                if f in STRATEGIES:
                    perf[f].append(t.get('pnl', 0))
        msgs = []
        for k, v in m.items():
            pnl_list = perf.get(k, [])
            if len(pnl_list) >= 3:
                wins = sum(1 for x in pnl_list if x > 0)
                total_gain = sum(x for x in pnl_list if x > 0)
                total_loss = abs(sum(x for x in pnl_list if x <= 0))
                pf = total_gain / max(total_loss, 0.001)
                v['pf'] = round(pf, 2)
                v['trades'] = len(pnl_list)
                if pf > 1.3:
                    v['weight'] = min(2.0, v['weight'] * 1.15)
                elif pf < 0.8:
                    v['weight'] = max(0.1, v['weight'] * 0.7)
                msgs.append(f'{k}: PF={pf:.2f} w={v["weight"]:.1f}')
        state['strategy_matrix'] = m
        if msgs:
            add_log('Overlord: ' + ', '.join(msgs))
            try: send_telegram('Overlord update:\n' + '\n'.join(msgs))
            except: pass
    except Exception as e:
        add_log(f'Overlord error: {e}')

def overlord_adjust_threshold(get_candles, bt_on_data, STOP_LOSS, TAKE_PROFIT, get_tuned, state, add_log, send_telegram):
    try:
        closes = get_candles('BTC', '60', 14 * 24, drop_forming=True)
        if not closes: return
        best_th, best_pf = 4, 0
        for th in [2, 3, 4, 5, 6]:
            res = bt_on_data(closes, None, 'BTC', STOP_LOSS, TAKE_PROFIT, th)
            if res and res['profit_factor'] > best_pf:
                best_pf, best_th = res['profit_factor'], th
        tuned = get_tuned()
        if best_th != tuned['threshold']:
            old = tuned['threshold']
            tuned['threshold'] = best_th
            state['tuned'] = tuned
            add_log(f'Threshold optimized: {old} -> {best_th} (PF={best_pf})')
            try: send_telegram(f'Threshold: {best_th} (from {old})')
            except: pass
    except Exception as e:
        add_log(f'Threshold error: {e}')

def warroom_alloc_html(state):
    m = state.get('strategy_matrix', {})
    if not m: return '<div class="small">No data</div>'
    total = sum(v['weight'] for v in m.values()) or 1
    colors = ['#3fb950', '#58a6ff', '#d29922', '#f85149']
    bars = []
    for i, (k, v) in enumerate(m.items()):
        pct = v['weight'] / total * 100
        label = STRATEGIES.get(k, {}).get('label', k)
        c = colors[i % len(colors)]
        bars.append(f'<div style="display:flex;align-items:center;margin:6px 0;font-size:13px">'
                    f'<div style="width:{pct}%;height:22px;background:{c};border-radius:4px;min-width:6px"></div>'
                    f'<span style="margin-right:10px;color:#e6edf3">{label} {pct:.0f}%</span>'
                    f'<span style="color:#8b949e"> PF={v["pf"]:.2f}</span></div>')
    return ''.join(bars)

def warroom_heat_html(state, COIN_FA):
    tbl = state.get('scan_table', [])
    if not tbl: return '<div class="small">-</div>'
    items = []
    for r in tbl[:21]:
        color = '#3fb950' if r.get('direction') == 'long' else '#f85149' if r.get('direction') == 'short' else '#30363d'
        name = COIN_FA.get(r['coin'], r['coin'])
        items.append(f'<span style="display:inline-block;width:30%;padding:4px 6px;margin:3px;background:#161b22;border-radius:6px;border-right:3px solid {color};font-size:12px">{name}<br><span style="color:{color}">{r["score"]:+.1f}</span></span>')
    return '<div style="display:flex;flex-wrap:wrap">' + ''.join(items) + '</div>'
