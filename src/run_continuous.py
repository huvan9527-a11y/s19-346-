#!/usr/bin/env python3
"""Morning selection, full-session exit monitoring and closing valuation."""
import os
import subprocess
import sys
import time
import s19_paper as s19
import market_gate

INTERVAL=int(os.getenv('S19_SCAN_INTERVAL_SECONDS','30'))

def run_once():
    return subprocess.run([sys.executable,str(s19.ROOT/'src/s19_paper.py')],cwd=s19.ROOT).returncode

def main():
    if not s19.is_session(s19.now_bj().date()):
        print('non-trading day, skip',flush=True)
        return 0
    failed=False
    scans=0
    while s19.now_bj().strftime('%H:%M')<s19.CFG['monitor_start']:
        time.sleep(min(INTERVAL,15))
    while s19.now_bj().strftime('%H:%M')<=s19.CFG['paper_end']:
        dt=s19.now_bj()
        hm=dt.strftime('%H:%M')
        if hm<=s19.CFG['scan_end']:
            market_gate.get_gate(dt,s19.CFG)
        if hm<=s19.CFG['scan_end'] or s19.trading_time(dt) or hm>='15:00':
            scans+=1
            failed=(run_once()!=0) or failed
        time.sleep(INTERVAL)
    print(f"PAPER_SUMMARY scans={scans} failed={failed}",flush=True)
    return 1 if failed or scans==0 else 0

if __name__=='__main__':
    from run_session import main as unified_main
    raise SystemExit(unified_main())
