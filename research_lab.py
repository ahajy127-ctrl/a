"""Offline research helpers inspired by the strongest research pieces of the source projects.
No exchange calls. Safe to run against a CSV containing a `pnl` column.
"""
import argparse, csv, math, random, statistics
from pathlib import Path


def load_pnls(path):
    with open(path, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    vals = [float(r['pnl']) for r in rows if r.get('pnl') not in (None, '')]
    if len(vals) < 10:
        raise ValueError('Need at least 10 pnl observations')
    return vals


def max_drawdown(curve):
    peak = curve[0]
    mdd = 0.0
    for x in curve:
        peak = max(peak, x)
        mdd = min(mdd, x - peak)
    return -mdd


def monte_carlo(pnls, runs=5000, seed=42):
    rng = random.Random(seed)
    worst_dd = 0.0
    final_values = []
    for _ in range(runs):
        seq = pnls[:]
        rng.shuffle(seq)
        equity = 0.0
        curve = [equity]
        for p in seq:
            equity += p
            curve.append(equity)
        worst_dd = max(worst_dd, max_drawdown(curve))
        final_values.append(equity)
    final_values.sort()
    return {
        'runs': runs,
        'worst_drawdown': worst_dd,
        'p05_final': final_values[max(0, int(runs*0.05)-1)],
        'median_final': statistics.median(final_values),
        'p95_final': final_values[min(runs-1, int(runs*0.95))],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('csv')
    ap.add_argument('--runs', type=int, default=5000)
    args = ap.parse_args()
    result = monte_carlo(load_pnls(args.csv), args.runs)
    for k, v in result.items():
        print(f'{k}: {v}')

if __name__ == '__main__':
    main()
