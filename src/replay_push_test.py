#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manual STRICT S19 push format/dedup test; no trading or account changes."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import s19_paper as s19

def main():
    if not os.getenv("SERVERCHAN_SENDKEY"):
        raise RuntimeError("SERVERCHAN_SENDKEY missing")
    dt=datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=9,minute=35,second=0)
    candidates=[
        dict(code="600241",name="时代万恒",theme="新能源电池",role="龙头",
             theme_sealed=4,streak=4,sealed=True,locked=True),
        dict(code="605303",name="园林股份",theme="半导体",role="龙头",
             theme_sealed=4,streak=3,sealed=True,locked=True),
    ]
    events=[
        "UNFILLED 600241 时代万恒｜已封板",
        "UNFILLED 605303 园林股份｜已封板",
    ]
    mem={"date":dt.strftime("%Y-%m-%d"),"sent":[]}
    s19.load_push_state=lambda _:mem
    s19.save_push_state=lambda _:None
    actual_send=s19.serverchan_send
    delivered=[0]
    def send(title,body):
        ok=actual_send("[测试] "+title,
                       "**人工推送测试；股票、题材、封板数量和09:35时间是示例，不是历史或实时信号。**\\n\\n"+body)
        if ok:delivered[0]+=1
        return ok
    s19.serverchan_send=send
    s19.push_signals(candidates,events,dt)
    if delivered[0]!=2:
        raise RuntimeError(f"expected 2 successful sends, got {delivered[0]}")
    s19.push_signals(candidates,events,dt)
    if delivered[0]!=2:
        raise RuntimeError("dedup failed")
    print("PASS: strict S19 push format and in-memory dedup (2 sends, 0 repeats)")

if __name__=="__main__":
    main()
