#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Independent S19 live-signal scanner. Does not touch the simulated account."""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import s19_paper as s19  # shared market-data and strict S19 selection rules

INTERVAL = 5

def hm(dt):
    return dt.strftime("%H:%M")

def journal(dt, record):
    # Local append only: never delay signal delivery for extra market requests.
    try:
        path=ROOT/"logs"/"signals"/f"{dt:%Y-%m-%d}.jsonl"
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open("a",encoding="utf-8") as f:
            f.write(json.dumps({"time":dt.isoformat(),**record},ensure_ascii=False)+"\n")
    except OSError as exc:
        print(f"JOURNAL_ERROR {type(exc).__name__}",flush=True)

def run_once():
    dt = s19.now_bj()
    if not s19.is_session(dt.date()):
        return True
    if not (s19.CFG["monitor_start"] <= hm(dt) <= s19.CFG["scan_end"]):
        return True
    try:
        health=s19.load_push_state(dt)
        pool = s19.fetch_live_pool(dt,health,timeout=8)
        s19.save_push_state(health)
        cands = s19.select_s19(pool)
        print(
            f"SIGNAL_SCAN_OK {dt:%Y-%m-%d %H:%M:%S} "
            f"pool={len(pool)} candidates={len(cands)}",
            flush=True,
        )
        journal(dt,{"status":"SCAN_OK","pool":pool,"candidates":cands})
        # No simulated BUY/SELL events are passed here: alerts reflect the
        # strict S19 market signal; simulated fills remain in the paper workflow.
        s19.push_signals(cands, [], dt)
        journal(dt,{"status":"PUSH_COMPLETE","sent":s19.load_push_state(dt).get("sent",[])})
        return True
    except Exception as exc:
        event = f"DATA_ERROR {type(exc).__name__}: {exc}"
        print(event, flush=True)
        journal(dt,{"status":"ERROR","error":event})
        try:
            s19.push_signals([], [event], dt)
        except Exception as push_exc:
            print(f"STATUS_PUSH_ERROR {type(push_exc).__name__}: {push_exc}", flush=True)
        return False

def main():
    dt = s19.now_bj()
    if not os.getenv("SERVERCHAN_SENDKEY"):
        raise RuntimeError("SERVERCHAN_SENDKEY missing")
    if not s19.is_session(dt.date()):
        print("non-trading day, skip", flush=True)
        return 0

    print(f"SIGNAL_START {dt.isoformat()} window={s19.CFG['monitor_start']}–{s19.CFG['scan_end']} interval={INTERVAL}s",flush=True)
    start = s19.CFG["monitor_start"]
    end = s19.CFG["scan_end"]

    # Start the runner early and keep it alive; do not rely on cron for each scan.
    while hm(s19.now_bj()) < start:
        time.sleep(min(INTERVAL, 15))

    failed=False
    scans=0
    while hm(s19.now_bj()) <= end:
        scans+=1
        failed=(not run_once()) or failed
        time.sleep(INTERVAL)

    print("S19 signal window closed", flush=True)
    return 1 if failed or scans==0 else 0

if __name__ == "__main__":
    raise SystemExit(main())
