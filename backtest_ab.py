#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B/C research backtest for Hyperliquid bot.
A = current baseline engine.
B = regime-aware entries + volatility-aware risk cap.
C = direction/MTF-aligned research candidate + risk-neutral quality sizing.
No live orders are placed by this file.
"""
import argparse, csv, json, time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'research_data'


def load_csv(path):
    rows=list(csv.DictReader(open(path,encoding='utf-8-sig',newline='')))
    if len(rows)<100: raise ValueError('حداقل 100 کندل لازم است')
    def f(r,k,d=0):
        try:return float(r.get(k,d))
        except:return float(d)
    ts=[]
    for n,r in enumerate(rows):
        raw=r.get('time') or r.get('timestamp') or r.get('datetime')
        try:
            if str(raw).isdigit():
                x=float(raw); ts.append(x/1000 if x>1e12 else x)
            else: ts.append(datetime.fromisoformat(str(raw).replace('Z','+00:00')).timestamp())
        except: ts.append(float(n)*3600)
    return rows,[f(r,'close') for r in rows],[f(r,'high',f(r,'close')) for r in rows],[f(r,'low',f(r,'close')) for r in rows],[f(r,'volume') for r in rows],ts


def simulate_b(bot, closes, highs, lows, vols, ts, coin, threshold=4, sl=None, tp=None):
    """B: keep the baseline signal, but reject obvious counter-trend signals and
    cap risk when volatility/regime quality is poor. Exit mechanics are identical."""
    if len(closes)<40:return None
    sl=bot.STOP_LOSS if sl is None else sl; tp=bot.TAKE_PROFIT if tp is None else tp
    capital=1000.0; peak=capital; maxdd=0.0; trades=[]; i=30; cooldown_until=-1
    fee_rate=bot.HL_FEE; slip=0.0003
    while i < len(closes)-2:
        if i<cooldown_until: i+=1; continue
        ind=bot._bt_indicators(closes,highs,lows,vols,i)
        if not ind: i+=1; continue
        direction=None; score=0.0
        if ind['rsi']<38: direction='long'; score+=3
        elif ind['rsi']>62: direction='short'; score+=3
        else: i+=1; continue
        if ind['mom']>0.005:
            score += 2 if direction=='long' else -3
        elif ind['mom']<-0.005:
            score += 2 if direction=='short' else -3
        trend=(ind['sma7']-ind['sma20'])/max(ind['sma20'],1e-12)
        if trend>0.0015: score += 1 if direction=='long' else -1
        elif trend<-0.0015: score += 1 if direction=='short' else -1
        if ind.get('macd') is not None and ((direction=='long' and ind['macd']>0) or (direction=='short' and ind['macd']<0)): score+=1
        if ind.get('adx') is not None and ind['adx']>30: score+=1
        if ind.get('vwap') is not None and ((direction=='long' and closes[i]>ind['vwap']*1.002) or (direction=='short' and closes[i]<ind['vwap']*0.998)): score+=1
        sess=bot._bt_session_from_ts(ts[i])
        if sess=='quiet': score-=0.5
        elif sess=='overlap': score+=0.75

        # B regime filter: in strong trend, do not fade the trend unless momentum agrees.
        strong_trend=abs(trend)>=0.003 and (ind.get('adx') or 0)>=25
        if strong_trend:
            trend_dir='long' if trend>0 else 'short'
            if direction!=trend_dir: i+=1; continue
        # B also rejects weak signals in quiet/ranging conditions.
        if (ind.get('adx') is not None and ind['adx']<16 and abs(ind['mom'])<0.003):
            i+=1; continue
        if score<threshold: i+=1; continue

        q,factors=bot._bt_quality(ind,direction,score,sess)
        risk=0.0125 if q>=92 else 0.01 if q>=85 else 0.0075 if q>=75 else 0.005 if q>=65 else 0.0035
        if sess=='quiet': risk*=0.8
        # B volatility cap: never exceed 1%, and cut risk in unusually volatile bars.
        tr=[]
        for j in range(max(1,i-14),i+1): tr.append(max(highs[j]-lows[j],abs(highs[j]-closes[j-1]),abs(lows[j]-closes[j-1])))
        atr=(np.mean(tr)/max(closes[i],1e-12)) if tr else sl
        if atr>0.012: risk=min(risk,0.005)
        elif atr>0.009: risk=min(risk,0.0075)
        risk=min(risk,0.01)

        entry=closes[i]*(1+slip if direction=='long' else 1-slip)
        sl_pct=max(sl*0.6,min(sl*2.2,atr*2.5)); tp_pct=max(sl_pct*1.3,sl_pct*(tp/max(sl,1e-9)))
        lev=min(bot.eff_leverage(coin),bot.MAX_LEV)
        risk_dollars=capital*risk; margin=risk_dollars/max(sl_pct*lev,1e-9); notional=margin*lev
        entry_fee=notional*fee_rate; capital-=entry_fee
        exit_reason='max_age'; exit_px=closes[min(i+24,len(closes)-1)]; exit_j=min(i+24,len(closes)-1); funding_cost=0.0
        for j in range(i+1,min(i+25,len(closes))):
            hi,lo=highs[j],lows[j]; sl_px=entry*(1-sl_pct if direction=='long' else 1+sl_pct); tp_px=entry*(1+tp_pct if direction=='long' else 1-tp_pct)
            hit_sl=(lo<=sl_px) if direction=='long' else (hi>=sl_px); hit_tp=(hi>=tp_px) if direction=='long' else (lo<=tp_px)
            if hit_sl: exit_px=sl_px*(1-slip if direction=='long' else 1+slip); exit_reason='stop_loss'; exit_j=j; break
            if hit_tp: exit_px=tp_px*(1-slip if direction=='long' else 1+slip); exit_reason='take_profit'; exit_j=j; break
            exit_px=closes[j]; exit_j=j
        gross=(exit_px-entry)/entry*notional if direction=='long' else (entry-exit_px)/entry*notional
        exit_fee=abs(notional*(exit_px/entry))*fee_rate
        net=gross-entry_fee-exit_fee-funding_cost
        capital+=net; peak=max(peak,capital); maxdd=max(maxdd,(peak-capital)/peak)
        trades.append({'i':i,'direction':direction,'score':round(score,3),'quality':q,'session':sess,'risk_pct':risk,'lev':lev,'entry':entry,'exit':exit_px,'pnl':net,'pnl_pct':net/max(capital-net+1e-9,1e-9)*100,'reason':exit_reason})
        if net<0: cooldown_until=exit_j+2
        i=max(i+1,exit_j+1)
    if not trades:return None
    pnls=np.array([t['pnl'] for t in trades],float); wins=pnls[pnls>0]; losses=pnls[pnls<=0]
    curve=[1000.0]; eq=1000.0
    for p in pnls:eq+=p;curve.append(eq)
    sharpe=float(np.mean(pnls)/np.std(pnls)*np.sqrt(len(pnls))) if len(pnls)>2 and np.std(pnls)>1e-12 else 0
    return {'trades':len(trades),'win_rate':round(len(wins)/len(trades)*100,1),'profit_factor':round(float(wins.sum()/abs(losses.sum())),2) if len(losses) else 99.0,'total_pnl':round(capital-1000,2),'return_pct':round((capital/1000-1)*100,2),'max_drawdown_pct':round(maxdd*100,2),'sharpe':round(sharpe,2),'avg_quality':round(float(np.mean([t['quality'] for t in trades])),1),'avg_risk_pct':round(float(np.mean([t['risk_pct'] for t in trades]))*100,3),'trades_by_session':{s:sum(1 for t in trades if t['session']==s) for s in ('asia','europe','overlap','us','quiet')},'trade_log':trades}


def _htf_direction(closes, i, hours):
    """Historical higher-timeframe direction using only completed bars <= i."""
    if i < hours * 2:
        return None
    # Use completed blocks ending at i; no future candles are referenced.
    block = list(closes[:i+1])
    n = len(block) // hours
    if n < 2:
        return None
    closes_htf = [block[(k+1)*hours-1] for k in range(n)]
    recent = np.mean(closes_htf[-2:])
    older = np.mean(closes_htf[-5:-2]) if len(closes_htf) >= 5 else np.mean(closes_htf[:-2])
    if older <= 0:
        return None
    d = (recent - older) / older
    if d > 0.0015:
        return 'long'
    if d < -0.0015:
        return 'short'
    return 'neutral'


def simulate_c(bot, closes, highs, lows, vols, ts, coin, threshold=4, sl=None, tp=None):
    """C: stricter research candidate.

    Evidence from the report shows the current quality score is not calibrated:
    higher-quality buckets were not consistently more profitable. C therefore
    does NOT increase risk because of quality. It requires higher-timeframe
    direction alignment and only trades RSI pullbacks in that direction.
    """
    if len(closes) < 80:
        return None
    sl=bot.STOP_LOSS if sl is None else sl; tp=bot.TAKE_PROFIT if tp is None else tp
    capital=1000.0; peak=capital; maxdd=0.0; trades=[]; i=50; cooldown_until=-1
    fee_rate=bot.HL_FEE; slip=0.0003
    while i < len(closes)-2:
        if i < cooldown_until:
            i += 1; continue
        ind=bot._bt_indicators(closes,highs,lows,vols,i)
        if not ind:
            i += 1; continue
        direction=None; score=0.0
        if ind['rsi'] < 38: direction='long'; score+=3
        elif ind['rsi'] > 62: direction='short'; score+=3
        else:
            i += 1; continue

        trend=(ind['sma7']-ind['sma20'])/max(ind['sma20'],1e-12)
        if direction=='long':
            if trend < -0.0015: i+=1; continue
            score += 1 if trend > 0.0015 else 0
        else:
            if trend > 0.0015: i+=1; continue
            score += 1 if trend < -0.0015 else 0

        mom=ind['mom']
        # Pullback entry: momentum may be neutral, but must not be strongly
        # against the desired direction at the trigger candle.
        if direction=='long' and mom < -0.008: i+=1; continue
        if direction=='short' and mom > 0.008: i+=1; continue
        if direction=='long' and mom > 0.005: score += 2
        elif direction=='short' and mom < -0.005: score += 2

        if ind.get('macd') is not None and ((direction=='long' and ind['macd']>0) or (direction=='short' and ind['macd']<0)): score+=1
        if ind.get('adx') is not None and ind['adx']>30: score+=1
        if ind.get('vwap') is not None and ((direction=='long' and closes[i]>ind['vwap']*0.998) or (direction=='short' and closes[i]<ind['vwap']*1.002)): score+=1
        sess=bot._bt_session_from_ts(ts[i])
        if sess=='quiet': score-=0.5
        elif sess=='overlap': score+=0.75

        h4=_htf_direction(closes,i,4); d1=_htf_direction(closes,i,24)
        # Strong alignment is mandatory. Neutral higher timeframe is not enough.
        if h4 != direction or d1 != direction:
            i += 1; continue
        score += 1.0
        if score < threshold+0.5:
            i += 1; continue

        q,factors=bot._bt_quality(ind,direction,score,sess)
        # Quality is diagnostic only until it proves predictive OOS.
        risk=0.005
        if sess=='quiet': risk*=0.8
        tr=[]
        for j in range(max(1,i-14),i+1):
            tr.append(max(highs[j]-lows[j],abs(highs[j]-closes[j-1]),abs(lows[j]-closes[j-1])))
        atr=(np.mean(tr)/max(closes[i],1e-12)) if tr else sl
        if atr>0.012: risk=min(risk,0.0035)
        elif atr>0.009: risk=min(risk,0.004)

        entry=closes[i]*(1+slip if direction=='long' else 1-slip)
        sl_pct=max(sl*0.6,min(sl*2.2,atr*2.5)); tp_pct=max(sl_pct*1.3,sl_pct*(tp/max(sl,1e-9)))
        lev=min(bot.eff_leverage(coin),bot.MAX_LEV)
        risk_dollars=capital*risk; margin=risk_dollars/max(sl_pct*lev,1e-9); notional=margin*lev
        entry_fee=notional*fee_rate; capital-=entry_fee
        exit_reason='max_age'; exit_px=closes[min(i+24,len(closes)-1)]; exit_j=min(i+24,len(closes)-1); funding_cost=0.0
        for j in range(i+1,min(i+25,len(closes))):
            hi,lo=highs[j],lows[j]
            sl_px=entry*(1-sl_pct if direction=='long' else 1+sl_pct); tp_px=entry*(1+tp_pct if direction=='long' else 1-tp_pct)
            hit_sl=(lo<=sl_px) if direction=='long' else (hi>=sl_px)
            hit_tp=(hi>=tp_px) if direction=='long' else (lo<=tp_px)
            if hit_sl:
                exit_px=sl_px*(1-slip if direction=='long' else 1+slip); exit_reason='stop_loss'; exit_j=j; break
            if hit_tp:
                exit_px=tp_px*(1-slip if direction=='long' else 1+slip); exit_reason='take_profit'; exit_j=j; break
            exit_px=closes[j]; exit_j=j
        gross=(exit_px-entry)/entry*notional if direction=='long' else (entry-exit_px)/entry*notional
        exit_fee=abs(notional*(exit_px/entry))*fee_rate
        net=gross-entry_fee-exit_fee-funding_cost
        capital+=net; peak=max(peak,capital); maxdd=max(maxdd,(peak-capital)/peak)
        trades.append({'i':i,'direction':direction,'score':round(score,3),'quality':q,'session':sess,'risk_pct':risk,'lev':lev,'entry':entry,'exit':exit_px,'pnl':net,'pnl_pct':net/max(capital-net+1e-9,1e-9)*100,'reason':exit_reason,'h4':h4,'d1':d1})
        if net<0: cooldown_until=exit_j+2
        i=max(i+1,exit_j+1)
    if not trades:return None
    pnls=np.array([t['pnl'] for t in trades],float); wins=pnls[pnls>0]; losses=pnls[pnls<=0]
    sharpe=float(np.mean(pnls)/np.std(pnls)*np.sqrt(len(pnls))) if len(pnls)>2 and np.std(pnls)>1e-12 else 0
    return {'trades':len(trades),'win_rate':round(len(wins)/len(trades)*100,1),'profit_factor':round(float(wins.sum()/abs(losses.sum())),2) if len(losses) else 99.0,'total_pnl':round(capital-1000,2),'return_pct':round((capital/1000-1)*100,2),'max_drawdown_pct':round(maxdd*100,2),'sharpe':round(sharpe,2),'avg_quality':round(float(np.mean([t['quality'] for t in trades])),1),'avg_risk_pct':round(float(np.mean([t['risk_pct'] for t in trades]))*100,3),'trades_by_session':{s:sum(1 for t in trades if t['session']==s) for s in ('asia','europe','overlap','us','quiet')},'trade_log':trades}



def simulate_bc(bot, closes, highs, lows, vols, ts, coin, threshold=4, sl=None, tp=None, strict=True):
    """B+C combined research candidate.
    B is the primary regime/volatility gate; C supplies higher-timeframe direction.
    strict=True requires both 4h and 24h alignment. strict=False allows one neutral HTF,
    but never allows a confirmed opposite HTF direction. No live orders are placed here.
    """
    if len(closes) < 80: return None
    sl=bot.STOP_LOSS if sl is None else sl; tp=bot.TAKE_PROFIT if tp is None else tp
    capital=1000.0; peak=capital; maxdd=0.0; trades=[]; i=50; cooldown_until=-1
    fee_rate=bot.HL_FEE; slip=0.0003
    while i < len(closes)-2:
        if i < cooldown_until: i += 1; continue
        ind=bot._bt_indicators(closes,highs,lows,vols,i)
        if not ind: i += 1; continue
        direction=None; score=0.0
        if ind['rsi'] < 38: direction='long'; score += 3
        elif ind['rsi'] > 62: direction='short'; score += 3
        else: i += 1; continue

        trend=(ind['sma7']-ind['sma20'])/max(ind['sma20'],1e-12)
        strong_trend=abs(trend)>=0.003 and (ind.get('adx') or 0)>=25
        if strong_trend:
            trend_dir='long' if trend>0 else 'short'
            if direction != trend_dir: i += 1; continue
        if (ind.get('adx') is not None and ind['adx']<16 and abs(ind['mom'])<0.003): i += 1; continue

        if trend>0.0015: score += 1 if direction=='long' else -1
        elif trend<-0.0015: score += 1 if direction=='short' else -1
        mom=ind['mom']
        if mom>0.005: score += 2 if direction=='long' else -3
        elif mom<-0.005: score += 2 if direction=='short' else -3
        if ind.get('macd') is not None and ((direction=='long' and ind['macd']>0) or (direction=='short' and ind['macd']<0)): score += 1
        if ind.get('adx') is not None and ind['adx']>30: score += 1
        if ind.get('vwap') is not None and ((direction=='long' and closes[i]>ind['vwap']*1.002) or (direction=='short' and closes[i]<ind['vwap']*0.998)): score += 1
        sess=bot._bt_session_from_ts(ts[i])
        if sess=='quiet': score -= 0.5
        elif sess=='overlap': score += 0.75

        h4=_htf_direction(closes,i,4); d1=_htf_direction(closes,i,24)
        # Never trade against a confirmed higher-timeframe direction.
        if h4 in ('long','short') and h4 != direction: i += 1; continue
        if d1 in ('long','short') and d1 != direction: i += 1; continue
        aligned=sum(x==direction for x in (h4,d1))
        neutral=sum(x=='neutral' for x in (h4,d1))
        if strict:
            if h4 != direction or d1 != direction: i += 1; continue
            score += 1.0
        else:
            if aligned < 1: i += 1; continue
            score += 1.0 if aligned==2 else 0.5
            if neutral: score -= 0.25
        if score < threshold: i += 1; continue

        q,_=bot._bt_quality(ind,direction,score,sess)
        risk=0.0075 if strict else 0.008
        if sess=='quiet': risk*=0.8
        tr=[]
        for j in range(max(1,i-14),i+1): tr.append(max(highs[j]-lows[j],abs(highs[j]-closes[j-1]),abs(lows[j]-closes[j-1])))
        atr=np.mean(tr)/max(closes[i],1e-12) if tr else sl
        if atr>0.012: risk=min(risk,0.004)
        elif atr>0.009: risk=min(risk,0.006)
        risk=min(risk,0.01)

        entry=closes[i]*(1+slip if direction=='long' else 1-slip)
        sl_pct=max(sl*0.6,min(sl*2.2,atr*2.5)); tp_pct=max(sl_pct*1.3,sl_pct*(tp/max(sl,1e-9)))
        lev=min(bot.eff_leverage(coin),bot.MAX_LEV)
        risk_dollars=capital*risk; margin=risk_dollars/max(sl_pct*lev,1e-9); notional=margin*lev
        entry_fee=notional*fee_rate; capital-=entry_fee
        exit_reason='max_age'; exit_px=closes[min(i+24,len(closes)-1)]; exit_j=min(i+24,len(closes)-1)
        for j in range(i+1,min(i+25,len(closes))):
            hi,lo=highs[j],lows[j]; sl_px=entry*(1-sl_pct if direction=='long' else 1+sl_pct); tp_px=entry*(1+tp_pct if direction=='long' else 1-tp_pct)
            hit_sl=(lo<=sl_px) if direction=='long' else (hi>=sl_px); hit_tp=(hi>=tp_px) if direction=='long' else (lo<=tp_px)
            if hit_sl: exit_px=sl_px*(1-slip if direction=='long' else 1+slip); exit_reason='stop_loss'; exit_j=j; break
            if hit_tp: exit_px=tp_px*(1-slip if direction=='long' else 1+slip); exit_reason='take_profit'; exit_j=j; break
            exit_px=closes[j]; exit_j=j
        gross=(exit_px-entry)/entry*notional if direction=='long' else (entry-exit_px)/entry*notional
        exit_fee=abs(notional*(exit_px/entry))*fee_rate; net=gross-entry_fee-exit_fee
        capital+=net; peak=max(peak,capital); maxdd=max(maxdd,(peak-capital)/peak)
        trades.append({'i':i,'direction':direction,'score':round(score,3),'quality':q,'session':sess,'risk_pct':risk,'lev':lev,'entry':entry,'exit':exit_px,'pnl':net,'pnl_pct':net/max(capital-net+1e-9,1e-9)*100,'reason':exit_reason,'h4':h4,'d1':d1,'variant':'BC_STRICT' if strict else 'BC_WEIGHTED'})
        if net<0: cooldown_until=exit_j+2
        i=max(i+1,exit_j+1)
    if not trades: return None
    pnls=np.array([t['pnl'] for t in trades],float); wins=pnls[pnls>0]; losses=pnls[pnls<=0]
    sharpe=float(np.mean(pnls)/np.std(pnls)*np.sqrt(len(pnls))) if len(pnls)>2 and np.std(pnls)>1e-12 else 0
    return {'trades':len(trades),'win_rate':round(len(wins)/len(trades)*100,1),'profit_factor':round(float(wins.sum()/abs(losses.sum())),2) if len(losses) else 99.0,'total_pnl':round(capital-1000,2),'return_pct':round((capital/1000-1)*100,2),'max_drawdown_pct':round(maxdd*100,2),'sharpe':round(sharpe,2),'avg_quality':round(float(np.mean([t['quality'] for t in trades])),1),'avg_risk_pct':round(float(np.mean([t['risk_pct'] for t in trades]))*100,3),'trades_by_session':{s:sum(1 for t in trades if t['session']==s) for s in ('asia','europe','overlap','us','quiet')},'trade_log':trades}

def monte_carlo_bootstrap(trades,runs=5000,seed=42):
    """Bootstrap trades WITH replacement. Permuting fixed trades cannot change final PnL."""
    if not trades or len(trades)<20:return None
    rng=np.random.default_rng(seed); rets=np.array([float(t.get('pnl_pct',0))/100 for t in trades],float)
    rets=np.clip(rets,-0.95,10)
    finals=np.empty(runs); dds=np.empty(runs)
    n=len(rets)
    for k in range(runs):
        sample=rng.choice(rets,size=n,replace=True)
        eq=1000.0; peak=eq; mdd=0.0
        for r in sample:
            eq*=1.0+r; peak=max(peak,eq); mdd=max(mdd,(peak-eq)/peak*100)
        finals[k]=eq; dds[k]=mdd
    return {'method':'bootstrap_with_replacement_compounded','runs':runs,'seed':seed,'median_final':round(float(np.median(finals)),2),'p05_final':round(float(np.percentile(finals,5)),2),'p95_final':round(float(np.percentile(finals,95)),2),'median_return_pct':round(float((np.median(finals)/1000-1)*100),2),'p05_return_pct':round(float((np.percentile(finals,5)/1000-1)*100),2),'p95_return_pct':round(float((np.percentile(finals,95)/1000-1)*100),2),'p95_drawdown':round(float(np.percentile(dds,95)),2),'worst_drawdown':round(float(np.max(dds)),2)}


def summary(bot,coin,rows,closes,highs,lows,vols,ts):
    a=bot.bt_on_data(closes,vols,coin,bot.STOP_LOSS,bot.TAKE_PROFIT,bot.get_tuned()['threshold'],timestamps=ts,highs=highs,lows=lows)
    b=simulate_b(bot,closes,highs,lows,vols,ts,coin,bot.get_tuned()['threshold'])
    c=simulate_c(bot,closes,highs,lows,vols,ts,coin,bot.get_tuned()['threshold'])
    bc_strict=simulate_bc(bot,closes,highs,lows,vols,ts,coin,bot.get_tuned()['threshold'],strict=True)
    bc_weighted=simulate_bc(bot,closes,highs,lows,vols,ts,coin,bot.get_tuned()['threshold'],strict=False)
    for r in (a,b,c,bc_strict,bc_weighted):
        if r:r['monte_carlo']=monte_carlo_bootstrap(r['trade_log'])
    return a,b,c,bc_strict,bc_weighted


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--coins',default='BTC,ETH,SOL'); ap.add_argument('--days',type=int,default=365); ap.add_argument('--walk-forward',action='store_true'); args=ap.parse_args()
    import backtest_lab as lab
    import hyperliquid_bot as bot
    DATA.mkdir(exist_ok=True); report={'generated_utc':datetime.now(timezone.utc).isoformat(),'method':'A=current baseline; B=regime-aware research variant','coins':{}}
    for coin in [x.strip().upper() for x in args.coins.split(',') if x.strip()]:
        path=DATA/f'{coin}_1h.csv'
        if not path.exists():
            try:
                rows=lab.fetch_candles(coin,args.days)
                funding=lab.fetch_funding(coin,int(rows[0]['t']),int(rows[-1]['t'])) if rows else None
                if not rows:
                    raise RuntimeError(f'No candle data returned for {coin}')
                lab.save_csv(coin,rows,funding)
            except Exception as exc:
                print(f'\n### {coin} — DATA DOWNLOAD FAILED')
                print(f'Error: {type(exc).__name__}: {exc}')
                print('The research run will continue to the next coin.')
                report['coins'][coin]={'status':'download_failed','error':f'{type(exc).__name__}: {exc}'}
                continue
        rows,c,h,l,v,ts=load_csv(path); a,b,cres,bc_strict,bc_weighted=summary(bot,coin,rows,c,h,l,v,ts)
        coverage={'start_utc':datetime.fromtimestamp(ts[0],tz=timezone.utc).isoformat(),'end_utc':datetime.fromtimestamp(ts[-1],tz=timezone.utc).isoformat(),'actual_days':round((ts[-1]-ts[0])/86400,2)}
        report['coins'][coin]={'file':str(path),'bars':len(rows),'coverage':coverage,'A':a,'B':b,'C':cres,'BC_STRICT':bc_strict,'BC_WEIGHTED':bc_weighted}
        print('\n###',coin); print(json.dumps({'coverage':coverage,'A':a and {k:v for k,v in a.items() if k!='trade_log'},'B':b and {k:v for k,v in b.items() if k!='trade_log'},'C':cres and {k:v for k,v in cres.items() if k!='trade_log'},'BC_STRICT':bc_strict and {k:v for k,v in bc_strict.items() if k!='trade_log'},'BC_WEIGHTED':bc_weighted and {k:v for k,v in bc_weighted.items() if k!='trade_log'}},ensure_ascii=False,indent=2))
    out=DATA/'ab_backtest_report.json'; out.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8'); print('\nگزارش:',out)

if __name__=='__main__': main()
