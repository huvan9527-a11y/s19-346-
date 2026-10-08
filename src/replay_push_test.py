#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manual replay-push test for S19.

This does NOT alter the paper account. It only replays the saved 2026-10-08
morning S19 findings through ServerChan so the user can verify phone delivery.
"""
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")

SIGNALS = [
    {
        "time":"09:31左右",
        "code":"600241",
        "name":"时代万恒",
        "theme":"电池新能源",
        "role":"龙头",
        "streak":"4板",
        "status":"已封板，历史回放 UNFILLED",
    },
    {
        "time":"09:45前已确认",
        "code":"605303",
        "name":"园林股份",
        "theme":"半导体",
        "role":"龙头",
        "streak":"3板",
        "status":"已封板，历史回放 UNFILLED",
    },
    {
        "time":"10:50–10:55左右",
        "code":"002242",
        "name":"九阳股份",
        "theme":"机器人",
        "role":"龙头",
        "streak":"4板",
        "status":"已封板，历史回放 UNFILLED",
    },
]

def send(title, desp):
    key=os.getenv("SERVERCHAN_SENDKEY","").strip()
    if not key:
        raise RuntimeError("SERVERCHAN_SENDKEY is not configured")
    r=requests.post(
        f"https://sctapi.ftqq.com/{key}.send",
        data={"title":title[:32],"desp":desp},
        timeout=20,
    )
    r.raise_for_status()
    try:
        js=r.json()
        if isinstance(js,dict) and js.get("code") not in (None,0):
            raise RuntimeError(f"ServerChan returned: {js}")
    except ValueError:
        pass

def main():
    now=datetime.now(BJ)
    send(
        "S19 方糖推送测试开始",
        f"这是手动回放测试，不会修改模拟仓。\n\n"
        f"触发时间：{now:%Y-%m-%d %H:%M:%S}\n\n"
        "接下来会发送 3 条 2026-10-08 上午历史信号。"
    )
    for s in SIGNALS:
        send(
            f"S19 回放｜{s['name']}",
            f"历史时间：{s['time']}\n\n"
            f"股票：{s['name']} {s['code']}\n\n"
            f"题材：{s['theme']}\n\n"
            f"地位：{s['role']}\n\n"
            f"连板：{s['streak']}\n\n"
            f"状态：{s['status']}\n\n"
            "说明：这是历史回放推送测试，不是实时买入指令。"
        )
    print("Replay push test completed: 4 messages sent.")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
