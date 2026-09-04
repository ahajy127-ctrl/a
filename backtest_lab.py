#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""آزمایشگاه بک‌تست واقعی Hyperliquid.

- دریافت کندل‌های واقعی از API عمومی Hyperliquid
- ذخیره CSV برای تکرارپذیری
- بک‌تست درون‌ساعتی با OHLC، کارمزد، لغزش، جلسه بازار، ریسک تطبیقی و Funding اختیاری
- Walk-Forward 90/30
- Monte Carlo روی معاملات

هیچ کلید یا کیف پولی لازم نیست.
"""
import argparse, csv, json, os, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'research_data'
API = 'https://api.hyperliquid.xyz/info'

# Public API can occasionally reset long-lived HTTPS connections on Windows.
# Use a small retry policy so a transient WinError 10054 does not kill the whole research run.
_SESSION = requests.Session()
_RETRY = Retry(total=5, connect=5, read=5, status=5, backoff_factor=1.0,
               status_forcelist=(429, 500, 502, 503, 504),
               allowed_methods=frozenset(['POST']),
               raise_on_status=False)
_SESSION.mount('https://', HTTPAdapter(max_retries=_RETRY))


def post(payload):
    last = None
    for attempt in range(1, 4):
        try:
            r = _SESSION.post(API, json=payload, timeout=(10, 45))
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt < 3:
                time.sleep(1.5 * attempt)
    raise last


def fetch_candles(coin, days=180, interval='1h'):
    step = {'1m':60,'5m':300,'15m':900,'30m':1800,'1h':3600,'4h':14400,'1d':86400}[interval]
    end = int(time.time()*1000)
    start = end - days*86400*1000
    rows=[]
    # API limits each snapshot; walk forward in windows.
    cur=start
    while cur < end:
        window_end=min(end, cur+5000*step*1000)
        data=post({'type':'candleSnapshot','req':{'coin':coin,'interval':interval,'startTime':cur,'endTime':window_end}}) or []
        if not data: break
        rows.extend(data)
        last=int(data[-1]['t'])
        nxt=last+step*1000
        if nxt<=cur: break
        cur=nxt
        if len(data)<100: break
        # Be gentle with the public endpoint; this also reduces connection resets.
        time.sleep(0.35)
    seen={int(x['t']):x for x in rows}
    rows=[seen[k] for k in sorted(seen)]
    return rows


def fetch_funding(coin, start_ms, end_ms):
    # Funding history is paginated; normalize to timestamp -> rate.
    out=[]; cur=start_ms
    while cur < end_ms:
        data=post({'type':'fundingHistory','coin':coin,'startTime':cur,'endTime':end_ms}) or []
        if not data: break
        out.extend(data)
        last=int(data[-1].get('time',cur))
        nxt=last+1
        if nxt<=cur: break
        cur=nxt
        if len(data)<500: break
        time.sleep(0.35)
    return {int(x['time']):float(x['fundingRate']) for x in out}


def save_csv(coin, rows, funding=None):
    DATA.mkdir(exist_ok=True)
    path=DATA/f'{coin}_1h.csv'
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f); w.writerow(['time','open','high','low','close','volume','funding'])
        for x in rows:
            ts=int(x['t']); fr=''
            if funding:
                # latest settled funding at or before candle timestamp.
                keys=[k for k in funding.keys() if k<=ts]
                if keys: fr=funding[max(keys)]
            w.writerow([datetime.fromtimestamp(ts/1000,tz=timezone.utc).isoformat(),x['o'],x['h'],x['l'],x['c'],x['v'],fr])
    return path


def run_coin(coin, path):
    import hyperliquid_bot as bot
    result=bot.run_research_backtest_from_csv(path,coin)
    if result and result.get('trade_log'):
        result['monte_carlo']=bot.monte_carlo_trade_shuffle(result['trade_log'],runs=5000,seed=42)
    return result


def walk_forward_csv(path, coin, train=90, test=30):
    rows=list(csv.DictReader(open(path,encoding='utf-8')))
    bars_per_day=24
    step=test*bars_per_day; train_n=train*bars_per_day
    import hyperliquid_bot as bot
    windows=[]
    start=0
    while start+train_n+step<=len(rows):
        train_rows=rows[start:start+train_n]; test_rows=rows[start+train_n:start+train_n+step]
        tmp=DATA/f'._{coin}_train.csv'; tmp2=DATA/f'._{coin}_test.csv'
        for p,rs in ((tmp,train_rows),(tmp2,test_rows)):
            with p.open('w',newline='',encoding='utf-8') as f:
                w=csv.DictWriter(f,fieldnames=['time','open','high','low','close','volume','funding']);w.writeheader();w.writerows(rs)
        tr=bot.run_research_backtest_from_csv(tmp,coin)
        te=bot.run_research_backtest_from_csv(tmp2,coin)
        windows.append({'train':tr,'test':te})
        tmp.unlink(missing_ok=True);tmp2.unlink(missing_ok=True)
        start += step
    return windows


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--coins',default='BTC,ETH,SOL',help='مثلاً BTC,ETH,SOL')
    ap.add_argument('--days',type=int,default=180)
    ap.add_argument('--data-only',action='store_true')
    ap.add_argument('--walk-forward',action='store_true')
    ap.add_argument('--no-funding',action='store_true')
    args=ap.parse_args()
    coins=[x.strip().upper() for x in args.coins.split(',') if x.strip()]
    DATA.mkdir(exist_ok=True)
    report={'generated_utc':datetime.now(timezone.utc).isoformat(),'days':args.days,'coins':{}}
    for coin in coins:
        path=DATA/f'{coin}_1h.csv'
        rows=fetch_candles(coin,args.days)
        funding=None
        if rows and not args.no_funding:
            funding=fetch_funding(coin,int(rows[0]['t']),int(rows[-1]['t']))
        save_csv(coin,rows,funding)
        result=run_coin(coin,path)
        item={'file':str(path),'bars':len(rows),'result':result}
        if args.walk_forward:
            item['walk_forward']=walk_forward_csv(path,coin)
        report['coins'][coin]=item
        print('\n###',coin)
        print(json.dumps({k:v for k,v in item.items() if k!='walk_forward'},ensure_ascii=False,indent=2,default=str))
    out=DATA/'backtest_report.json';out.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print('\nگزارش:',out)

if __name__=='__main__': main()
