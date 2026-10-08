#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S19 continuous intraday runner for GitHub Actions paper trading.

Scheduled workflow starts once before the S19 window, then this process stays alive
and re-runs the scanner every N seconds until scan_end. This avoids relying on
GitHub cron to trigger every few minutes.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
BJ = ZoneInfo(CFG.get("timezone","Asia/Shanghai"))
INTERVAL = int(os.getenv("S19_SCAN_INTERVAL_SECONDS","15"))

def now():
    return datetime.now(BJ)

def hm():
    return now().strftime("%H:%M")

def run_once():
    p = subprocess.run(
        [sys.executable, str(ROOT/"src"/"s19_paper.py")],
        cwd=ROOT,
        text=True
    )
    return p.returncode

def main():
    dt=now()
    if dt.weekday()>=5:
        print("weekend, skip")
        return 0

    start=CFG["scan_start"]
    end=CFG["scan_end"]

    # If the scheduled job arrives early, wait in-process until the window opens.
    while hm() < start:
        time.sleep(min(INTERVAL, 15))

    # If GitHub schedules us late but still inside the window, begin immediately.
    # Re-scan continuously until the cutoff.
    while hm() <= end:
        rc=run_once()
        if rc != 0:
            print(f"scan failed with rc={rc}")
        time.sleep(INTERVAL)

    # Final state snapshot after cutoff.
    run_once()
    return 0

if __name__=="__main__":
    raise SystemExit(main())
