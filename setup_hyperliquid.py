#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Secure first-run setup for Hyperliquid Final V1.4.
Never asks for or stores a master-wallet private key. Creates an Agent wallet only.
"""
import os, re, stat
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV = BASE / '.env'
DEFAULTS = {
    'MODE':'paper','HL_TESTNET':'true','HL_ACCOUNT_ADDRESS':'','HL_AGENT_PRIVATE_KEY':'',
    'DASH_PASS':'','TG_TOKEN':'','TG_CHAT':'','LIVE_REQUIRE_BACKSTOP':'true',
    'LIVE_MAX_LEVERAGE':'5','LIVE_MAX_TOTAL_RISK':'0.10','LIVE_DAILY_LOSS_LIMIT':'0.05',
    'LIVE_MAX_POSITIONS':'3','LIVE_MAX_BOOK_SHARE':'0.10','LIVE_MIN_ORDER_USD':'10',
    'SCAN_INTERVAL':'300','POS_CHECK_INTERVAL':'15','NO_AUTO_INSTALL':'true'
}

def load():
    d=DEFAULTS.copy()
    if ENV.exists():
        for raw in ENV.read_text(encoding='utf-8').splitlines():
            s=raw.strip()
            if s and not s.startswith('#') and '=' in s:
                k,v=s.split('=',1); d[k.strip()]=v.strip()
    return d

def save(d):
    lines=['# Hyperliquid Final V1.4 configuration','# Agent wallet only; NEVER put a master-wallet seed/private key here.','']
    for k,v in d.items(): lines.append(f'{k}={v}')
    ENV.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    try: os.chmod(ENV,0o600)
    except OSError: pass

def valid_addr(x): return bool(re.fullmatch(r'0x[0-9a-fA-F]{40}',x))

def ask(msg, default=''):
    v=input(f'{msg}' + (f' [{default}]' if default else '') + ': ').strip()
    return v or default

def main():
    print('\n=== Hyperliquid Final V1.4 — Secure Setup ===\n')
    d=load()
    net=ask('Network: 1=Testnet (recommended), 2=Mainnet','1')
    d['HL_TESTNET']='true' if net!='2' else 'false'
    addr=d.get('HL_ACCOUNT_ADDRESS','')
    while not valid_addr(addr):
        addr=ask('Master account public address (0x..., NEVER private key)','')
        if not valid_addr(addr): print('Invalid public address.')
    d['HL_ACCOUNT_ADDRESS']=addr
    if not d.get('HL_AGENT_PRIVATE_KEY'):
        try:
            from eth_account import Account
        except Exception as e:
            raise SystemExit('eth-account is required. Run: python -m pip install -r requirements.txt') from e
        acc=Account.create()
        d['HL_AGENT_PRIVATE_KEY']=acc.key.hex()
        print(f'\nAgent address: {acc.address}')
        print('Approve this Agent address in Hyperliquid API Wallets. The master key is never stored.')
    else:
        try:
            from eth_account import Account
            print(f"Existing Agent: {Account.from_key(d['HL_AGENT_PRIVATE_KEY']).address}")
        except Exception as e: raise SystemExit(f'Existing agent key is invalid: {e}')
    d['MODE']='paper'
    pw=d.get('DASH_PASS','')
    while len(pw)<12:
        pw=ask('Dashboard password (>=12 chars)','')
        if len(pw)<12: print('Password must be at least 12 characters.')
    d['DASH_PASS']=pw
    d['NO_AUTO_INSTALL']='true'
    save(d)
    print('\nSaved .env with restrictive permissions. Default mode remains PAPER.')
    print('Run preflight before any live change: python preflight.py')

if __name__=='__main__': main()
