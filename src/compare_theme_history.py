#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S19 strict-vs-broad THEME SIGNAL audit. Not an executable P&L backtest.

Uses historical END-OF-DAY 10jqka limit-up pool only.
Never treats later-identified limit-up stocks as intraday-available or filled.
"""
import argparse
import csv
import json
import re
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CACHE = ROOT / "data" / "historical_limit_pool"
OUTPUT = ROOT / "outputs" / "ab_theme_20251001_20260930"
FIELDS = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

THEMES = [
    ("电池新能源", ("固态电池","半固态","锂电","磷酸铁锂","圆柱锂","镍氢电池","新能源电池","电池材料","电池模组","储能电池")),
    ("机器人", ("人形机器人","养老机器人","机器人","减速器","伺服系统")),
    ("商业航天", ("商业航天","卫星互联网","卫星通信","太空算力")),
    ("可控核聚变", ("可控核聚变","核聚变")),
    ("半导体", ("半导体","芯片","光刻","封测")),
    ("算力AI", ("算力","数据中心","AI服务器","人工智能")),
    ("新能源电力", ("风电","光伏","抽水蓄能","电力主业","新能源")),
    ("消费食品", ("食品","山姆渠道","白酒","葡萄酒")),
]

def labels(raw, broad):
    result = []
    for x in re.split(r"[+＋]", str(raw or "")):
        x = re.sub(r"[（(].*?[)）]", "", x).strip()
        if broad:
            for theme, keys in THEMES:
                if any(k in x for k in keys):
                    x = theme
                    break
        if x and len(x) <= 12 and x not in result:
            result.append(x)
    return result

def get_pool(day, session):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{day}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    url = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
    params = {"page": 1, "limit": 200, "field": FIELDS, "filter": "HS,GEM2STAR",
              "order_field": "330324", "order_type": "0", "date": day}
    for attempt in range(3):
        try:
            r = session.get(url, params=params,
                headers={"User-Agent": UA, "Referer":"https://data.10jqka.com.cn/market/longhu/"},
                timeout=25)
            r.raise_for_status()
            payload = r.json()
            if payload.get("status_code") not in (None, 0):
                raise ValueError(f"THS status_code={payload.get('status_code')}")
            info = (payload.get("data") or {}).get("info")
            if not isinstance(info, list):
                raise ValueError("missing data.info")
            # 0 rows: likely holiday; don't cache to avoid mistaking throttling for zero signals.
            if info:
                path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
            return info
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise RuntimeError(f"{day}: {exc}") from exc
            time.sleep(2 * (attempt + 1))

def normalize(item, broad):
    code = str(item.get("code") or "").zfill(6)
    hd = str(item.get("high_days") or "")
    m = re.search(r"(\\d+)\\s*板", hd)
    streak = int(m.group(1)) if m else 1
    try:
        pct = float(item.get("change_rate") or 0)
    except (TypeError, ValueError):
        pct = 0
    # 10%/20% boards, rough EOD proxy only
    threshold = 19.7 if code.startswith(("300","301","688","689")) else 9.85
    sealed = pct >= threshold
    return {"code":code, "name":item.get("name",""),"themes":labels(item.get("reason_type"),broad),
            "streak":streak, "first":str(item.get("first_limit_up_time") or "9999999999"),
            "sealed":sealed, "reason":item.get("reason_type","")}

def select(items, broad):
    xs = [normalize(x,broad) for x in items]
    group = defaultdict(list)
    for x in xs:
        for t in x["themes"]:
            group[t].append(x)
    candidates = []
    for theme, members in group.items():
        count = sum(x["sealed"] for x in members)
        if count < int(CFG["min_theme_sealed"]):
            continue
        members.sort(key=lambda x:(-x["streak"],x["first"]))
        if members[0]["streak"] >= int(CFG["min_streak"]):
            candidates.append((theme,members[0],"龙头",count))
        if CFG.get("allow_second") and len(members)>1 and members[1]["streak"] >= max(2,int(CFG["min_streak"])-1):
            candidates.append((theme,members[1],"二龙头",count))
    candidates.sort(key=lambda t:(-t[3],-t[1]["streak"],t[1]["first"]))
    out = []
    used_themes=set()
    used_codes=set()
    for theme,x,role,num in candidates:
        if len(out)>=int(CFG["top_n"]): break
        if theme in used_themes or x["code"] in used_codes: continue
        out.append({"code":x["code"],"name":x["name"],"theme":theme,
                    "role":role,"streak":x["streak"],"theme_sealed":num,
                    "eod_sealed":x["sealed"]})
        used_themes.add(theme)
        used_codes.add(x["code"])
    return out

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--start",default="2025-10-01")
    p.add_argument("--end",default="2026-09-30")
    p.add_argument("--sleep",type=float,default=0.35)
    args=p.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    d=date.fromisoformat(args.start)
    end=date.fromisoformat(args.end)
    sessions=0
    missing=[]
    day_rows=[]
    detail=[]
    seen={"strict":set(),"broad":set()}
    s=requests.Session()
    while d<=end:
        if d.weekday()<5:
            day=d.strftime("%Y%m%d")
            try:
                pool=get_pool(day,s)
            except RuntimeError as e:
                print("ERROR",e,flush=True)
                missing.append(day)
                d+=timedelta(days=1)
                continue
            if pool:
                sessions+=1
                a=select(pool,False)
                b=select(pool,True)
                aset={x["code"] for x in a}
                bset={x["code"] for x in b}
                seen["strict"].update(aset)
                seen["broad"].update(bset)
                day_rows.append([d.isoformat(),len(pool),len(a),len(b),
                                 len(bset-aset),len(aset-bset),
                                 "|".join(sorted(aset)), "|".join(sorted(bset))])
                for model,chosen in [("strict",a),("broad",b)]:
                    for x in chosen:
                        detail.append([d.isoformat(),model,x["code"],x["name"],x["theme"],
                                       x["role"],x["streak"],x["theme_sealed"],x["eod_sealed"],
                                       "EOD ONLY: do not assume intraday fill"])
                print(day,len(a),len(b),flush=True)
            time.sleep(args.sleep)
        d+=timedelta(days=1)
    def write(name,header,rows):
        with (OUTPUT/name).open("w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    write("daily_comparison.csv",
          ["date","pool_size","strict_count","broad_count","new_codes","lost_codes","strict_codes","broad_codes"],day_rows)
    write("signal_details.csv",
          ["date","mode","code","name","theme","role","streak","theme_sealed","eod_sealed","warning"],detail)
    summary={
        "period":[args.start,args.end],"covered_sessions":sessions,
        "failed_dates":missing,
        "strict_signal_rows":sum(row[2] for row in day_rows),
        "broad_signal_rows":sum(row[3] for row in day_rows),
        "additional_stock_day_signals":sum(row[4] for row in day_rows),
        "removed_stock_day_signals":sum(row[5] for row in day_rows),
        "unique_strict_stocks":len(seen["strict"]),
        "unique_broad_stocks":len(seen["broad"]),
        "IMPORTANT":"EOD snapshot comparison ONLY. No minute book/auction data, execution, net value or return computed."
    }
    (OUTPUT/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)

if __name__=="__main__":
    main()
