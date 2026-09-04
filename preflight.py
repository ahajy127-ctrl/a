#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline preflight. No orders are sent and no network calls are made."""
import os, sys, compileall
from pathlib import Path
BASE=Path(__file__).resolve().parent
fail=[]
def chk(name, ok, detail=''):
    print(('PASS' if ok else 'FAIL') + f'  {name}' + (f' — {detail}' if detail else ''))
    (fail if not ok else []).append(name)

def main():
    mode=os.getenv('MODE','paper').lower()
    chk('MODE', mode in ('paper','live'), mode)
    if mode=='live':
        chk('LIVE confirmation', os.getenv('LIVE_CONFIRM','').strip()=='I_UNDERSTAND_LIVE_TRADING', 'LIVE_CONFIRM required')
    chk('No auto install', os.getenv('NO_AUTO_INSTALL','true').lower()=='true')
    req=BASE/'requirements.txt'; chk('requirements.txt',req.exists())
    chk('Bot source', (BASE/'hyperliquid_bot.py').exists())
    chk('risk_guard wired', 'from risk_guard import' in (BASE/'hyperliquid_bot.py').read_text(encoding='utf-8'))
    chk('Agent key absent from source', 'HL_AGENT_PRIVATE_KEY=' not in (BASE/'hyperliquid_bot.py').read_text(encoding='utf-8'))
    chk('Python compile', compileall.compile_dir(str(BASE), quiet=1, maxlevels=1))
    print(f'\nPreflight: {len(fail)} failure(s)')
    return 1 if fail else 0
if __name__=='__main__': sys.exit(main())
