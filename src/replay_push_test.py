#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manual dual-track ServerChan test: sends three clearly marked synthetic examples.
Does not mutate trading or persistent dedup state.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import s19_paper as s19

def signal(code,name,theme,streak,sealed_count):
    return dict(code=code,name=name,theme=theme,role="龙头",streak=streak,
                theme_sealed=sealed_count,sealed=True,locked=True)

def main():
    if not __import__("os").environ.get("SERVERCHAN_SENDKEY"):
        raise RuntimeError("SERVERCHAN_SENDKEY missing")
    # In-memory dedup test: uses the production push_dual_signals implementation.
    state={"date":"TEST","sent":[]}
    s19.load_push_state=lambda dt:state
    s19.save_push_state=lambda ps:None
    original=s19.serverchan_send
    count=[0]
    def send(title,body):
        # Every outgoing test contains a clear synthetic-data disclaimer.
        ok=original("[测试] "+title,
                    "**合成测试数据，不是今天真实选股信号，也不是买入指令。**\n\n"+body)
        if ok:count[0]+=1
        return ok
    s19.serverchan_send=send

    strict=[
        signal("600241","时代万恒","严格示例题材",4,4),
        signal("605303","园林股份","严格半导体",3,4),
    ]
    broad=[
        signal("002242","九阳股份","放宽机器人",4,6),
        signal("605303","园林股份","半导体",3,6),
    ]
    dt=datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=9,minute=35,second=0)
    events=["UNFILLED 002242 九阳股份｜已封板",
            "UNFILLED 605303 园林股份｜已封板",
            "UNFILLED 600241 时代万恒｜已封板"]
    s19.push_dual_signals(strict,broad,events,dt)
    if count[0]!=3:
        raise RuntimeError(f"expected 3 notifications, got {count[0]}")
    before=count[0]
    s19.push_dual_signals(strict,broad,events,dt)
    if count[0]!=before:
        raise RuntimeError("dedup failure: repeated notifications")
    print("PASS: three push requests succeeded; duplicate invocation sent zero additional messages")

if __name__=="__main__":
    main()
