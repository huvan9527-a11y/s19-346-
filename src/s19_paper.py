#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S19 20万模拟仓（GitHub Actions 版）。

原则：
1) S19 负责选股：题材宽度、连板高度、龙头/二龙头排序、承接过滤。
2) execution 只负责判断此刻是否还能买到；不把“炸板回封”加入策略条件。
3) GitHub Actions 不是逐笔系统：已经封死的票一律不虚构成交，只记录 UNFILLED。

此版本仅用于模拟仓验证，不用于真实下单。
"""
from __future__ import annotations
import csv, json, os, re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_P = ROOT / "state/account.json"
TRADES_P = ROOT / "logs/trades.csv"
EQUITY_P = ROOT / "logs/equity.csv"
OUT_P = ROOT / "outputs/latest.md"
PUSH_STATE_P = ROOT / "state/push_state.json"
BJ = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
THS_FIELD = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"

def now_bj():
    return datetime.now(BJ)

THEME_RULES = [
    ("电池新能源", ("固态电池","半固态","锂电","磷酸铁锂","圆柱锂","镍氢电池","新能源电池","电池材料","电池模组","储能电池")),
    ("机器人", ("人形机器人","养老机器人","机器人","减速器","伺服系统")),
    ("商业航天", ("商业航天","卫星互联网","卫星通信","太空算力")),
    ("可控核聚变", ("可控核聚变","核聚变")),
    ("半导体", ("半导体","芯片","光刻","封测")),
    ("算力AI", ("算力","数据中心","AI服务器","人工智能")),
    ("新能源电力", ("风电","光伏","抽水蓄能","电力主业","新能源")),
    ("消费食品", ("食品","山姆渠道","白酒","葡萄酒")),
]

def canonical_theme(label):
    """把同花顺细碎涨停原因归并到可统计的大题材；未命中的标签保持原样。"""
    s=(label or "").strip()
    for theme, keys in THEME_RULES:
        if any(k in s for k in keys):
            return theme
    return s

def concepts(raw, broad=True):
    out=[]
    for x in re.split(r"[+＋]", raw or ""):
        x=re.sub(r"[（(].*?[)）]", "", x).strip()
        if broad:
            x=canonical_theme(x)
        if x and len(x)<=12 and x not in out:
            out.append(x)
    return out

def fnum(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def inum(x, default=0):
    try:
        return int(float(x))
    except Exception:
        return default

def parse_preview(raw):
    if isinstance(raw, str):
        try:
            raw=json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    out=[]
    for v in raw:
        if isinstance(v, dict):
            val=v.get("price") if "price" in v else v.get("value", v.get("rate"))
        else:
            val=v
        try:
            out.append(float(val))
        except Exception:
            pass
    return out

def fetch_pool(date_yyyymmdd):
    url="https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
    params={
        "page":1,"limit":200,"field":THS_FIELD,
        "filter":"HS,GEM2STAR","order_field":"330324",
        "order_type":"0","date":date_yyyymmdd
    }
    r=requests.get(
        url, params=params,
        headers={"User-Agent":UA,"Referer":"https://data.10jqka.com.cn/market/longhu/"},
        timeout=20
    )
    r.raise_for_status()
    js=r.json()
    return ((js.get("data") or {}).get("info") or [])

def normalize(item, broad=True):
    code=str(item.get("code") or item.get("stock_code") or "").zfill(6)
    name=str(item.get("name") or item.get("stock_name") or "")
    reason=item.get("reason_type") or item.get("reason") or ""
    streak=inum(item.get("high_days") or item.get("continue_num") or item.get("limit_up_days") or 1, 1)
    hd=str(item.get("high_days") or "")
    m=re.search(r"(\d+)\s*板", hd)
    if m:
        streak=inum(m.group(1), streak)
    cur=fnum(item.get("latest") or item.get("price") or item.get("current_price"))
    lim=fnum(item.get("limit_up_price") or item.get("ztp"))
    pct=fnum(item.get("change_rate") or item.get("zf"))
    first=item.get("first_limit_up_time") or item.get("first_time") or item.get("first_limit_up") or ""
    preview=parse_preview(item.get("time_preview"))
    sealed=bool(lim>0 and cur>0 and cur >= lim-0.011)
    if lim<=0 and pct>=9.85 and not code.startswith(("300","301","688","689")):
        sealed=True
    locked=bool(item.get("is_new") == 1 and sealed and inum(item.get("open_num"))==0)
    return {
        "code":code,"name":name,"themes":concepts(reason, broad),"streak":streak,
        "cur":cur,"lim":lim,"pct":pct,"first":str(first),
        "preview":preview,"sealed":sealed,"locked":locked
    }

def acceptance(x):
    """保留 S19 的承接思想；数据缺失时不额外制造新条件。"""
    tp=x["preview"]
    if len(tp)<4:
        return True
    cur=tp[-1]
    hi=max(tp)
    avg=sum(tp)/len(tp)
    if cur < hi*(1-CFG.get("max_give",0.05)):
        return False
    if cur < avg:
        return False
    return True

def select_s19(items, broad=True):
    """S19 选股层：题材强度 -> 龙头/二龙头 -> 承接。
    注意：这里不包含“炸板回封”条件。
    """
    xs=[normalize(i, broad) for i in items]
    themes=defaultdict(list)
    for x in xs:
        for t in x["themes"]:
            themes[t].append(x)

    strong={
        t:mem for t,mem in themes.items()
        if sum(1 for x in mem if x["sealed"]) >= CFG["min_theme_sealed"]
    }

    cand=[]
    for t,mem in strong.items():
        mem=sorted(mem,key=lambda z:(-z["streak"], z["first"] or "999999"))
        if not mem:
            continue
        leader=mem[0]
        sealed_count=sum(1 for z in mem if z["sealed"])
        if leader["streak"]>=CFG["min_streak"]:
            cand.append((t,leader,"龙头",sealed_count))
        if CFG.get("allow_second") and len(mem)>=2:
            second=mem[1]
            if second["streak"]>=max(2,CFG["min_streak"]-1):
                cand.append((t,second,"二龙头",sealed_count))

    cand.sort(key=lambda z:(-z[3],-z[1]["streak"],z[1]["first"] or "999999"))
    out=[]
    used_theme=set()
    used_code=set()
    for theme,x,role,cnt in cand:
        if len(out)>=CFG["top_n"]:
            break
        if theme in used_theme or x["code"] in used_code:
            continue
        if not acceptance(x):
            continue
        out.append({"theme":theme,"role":role,"theme_sealed":cnt,**x})
        used_theme.add(theme)
        used_code.add(x["code"])
    return out


def load_push_state(dt):
    """Daily de-dup state. Reset automatically on a new Beijing trading date."""
    today=dt.strftime("%Y-%m-%d")
    if PUSH_STATE_P.exists():
        try:
            data=json.loads(PUSH_STATE_P.read_text(encoding="utf-8"))
            if data.get("date")==today:
                return data
        except Exception:
            pass
    return {"date":today,"sent":[]}

def save_push_state(ps):
    PUSH_STATE_P.parent.mkdir(parents=True,exist_ok=True)
    PUSH_STATE_P.write_text(json.dumps(ps,ensure_ascii=False,indent=2),encoding="utf-8")

def serverchan_send(title, desp):
    key=os.getenv("SERVERCHAN_SENDKEY","").strip()
    if not key:
        print("PUSH_SKIP: SERVERCHAN_SENDKEY not configured")
        return False
    try:
        r=requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title":title[:32],"desp":desp},
            timeout=15
        )
        r.raise_for_status()
        try:
            js=r.json()
            if isinstance(js,dict) and js.get("code") not in (None,0):
                print(f"PUSH_ERROR: {js}")
                return False
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"PUSH_ERROR {type(e).__name__}: {e}")
        return False

def push_dual_signals(strict, broad, events, dt):
    """One notification per stock/stage/mode, daily; broad side alone controls paper trades."""
    strict_by={x["code"]:x for x in strict}
    broad_by={x["code"]:x for x in broad}
    by_code={**strict_by,**broad_by}
    ps=load_push_state(dt)
    sent=set(ps.get("sent",[]))
    event_by_code={}
    for event in events:
        match=re.match(r"^(BUY|UNFILLED)\s+(\d{6})\b",event)
        if match:
            event_by_code[match.group(2)]=match.group(1)

    for code,x in by_code.items():
        a=strict_by.get(code)
        b=broad_by.get(code)
        mode="双Top3" if a and b else ("仅严格Top3（放宽未入Top3）" if a else "仅放宽Top3")
        if dt.strftime("%H:%M") < CFG["entry_start"]:
            stage="AUCTION_WATCH"
            stage_cn="竞价观察"
        elif event_by_code.get(code)=="BUY":
            stage="BUY"
            stage_cn="模拟买入"
        elif x["sealed"] or x["locked"] or event_by_code.get(code)=="UNFILLED":
            stage="UNFILLED"
            stage_cn="封板/不可确认成交"
        else:
            stage="SIGNAL"
            stage_cn="候选信号（未模拟成交）"

        key=f"{code}|{stage}|{mode}"
        if key in sent:
            continue
        title=f"S19 [{mode}] {stage_cn} {x['name']}"
        desc=(
            f"信号发现时间：{dt:%H:%M:%S}（北京时间）\n\n"
            f"股票：{x['name']} {code}\n\n"
            f"分类：**{mode}**（按两套最终Top3名单比较）\n\n"
            f"严格Top3：{a['theme'] if a else '未入选'}\n\n"
            f"放宽Top3：{b['theme'] if b else '未入选（不代表题材不合格）'}\n\n"
            f"连板：{x['streak']}，地位：{x['role']}\n\n"
            f"严格题材封板：{a['theme_sealed'] if a else '-'}\n\n"
            f"放宽题材封板：{b['theme_sealed'] if b else '-'}\n\n"
            f"股票当前：{'已封板' if x['sealed'] else '未确认封板'}\n\n"
            f"信号：{stage_cn}\n\n"
            f"模拟账户：仅放宽题材参与买卖；严格题材只观察。"
        )
        if serverchan_send(title,desc):
            sent.add(key)
            ps["sent"]=sorted(sent)
            save_push_state(ps)
            print(f"PUSH_OK {key}")

def load_state():
    if not STATE_P.exists():
        return {"cash":CFG["initial_cash"],"positions":{}}
    return json.loads(STATE_P.read_text(encoding="utf-8"))

def save_state(st):
    STATE_P.parent.mkdir(parents=True,exist_ok=True)
    STATE_P.write_text(json.dumps(st,ensure_ascii=False,indent=2),encoding="utf-8")

def append_csv(path,row):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a",newline="",encoding="utf-8") as f:
        csv.writer(f).writerow(row)

def quote_simple(code):
    sec=("sh" if code.startswith("6") else "sz")+code
    try:
        r=requests.get(
            "https://qt.gtimg.cn/q="+sec,
            headers={"User-Agent":UA,"Referer":"https://gu.qq.com/"},
            timeout=10
        )
        arr=r.content.decode("gbk","ignore").split("~")
        if len(arr)<6:
            return None
        return {
            "name":arr[1],"price":fnum(arr[3]),
            "prev_close":fnum(arr[4]),"open":fnum(arr[5])
        }
    except Exception:
        return None

def trade_days_held(entry_date, now_date):
    a=datetime.strptime(entry_date,"%Y-%m-%d").date()
    d=a
    n=0
    while d<now_date:
        d+=timedelta(days=1)
        if d.weekday()<5:
            n+=1
    return n

def process_exits(st,dt):
    to_del=[]
    for code,p in list(st["positions"].items()):
        q=quote_simple(code)
        if not q or q["price"]<=0:
            continue
        held=trade_days_held(p["entry_date"],dt.date())
        reason=None
        px=None
        if q["price"]<=p["stop"] and held>=1:
            reason="STOP"
            px=q["price"]
        elif held>=CFG["hold_days"]:
            reason="HOLD5"
            px=q["price"]
        if not reason:
            continue
        amount=p["shares"]*px
        fee=amount*CFG["commission_bp_one_way"]/10000
        st["cash"]+=amount-fee
        append_csv(TRADES_P,[
            dt.isoformat(),"SELL",code,p["name"],f"{px:.3f}",p["shares"],
            f"{amount:.2f}",f"{fee:.2f}",reason,p.get("tag",""),
            f"{st['cash']:.2f}"
        ])
        to_del.append(code)
    for c in to_del:
        del st["positions"][c]

def process_entries(st,cands,dt):
    events=[]
    for x in cands:
        if len(st["positions"])>=CFG["max_positions"]:
            break
        if x["code"] in st["positions"]:
            continue

        # 关键：S19 只负责选股。执行层只判断“此刻能否真实买到”。
        # 已经封死的票，不虚构成交，也不要求它“炸板回封”。
        if x["sealed"] or x["locked"] or x["cur"]<=0:
            events.append(
                f"UNFILLED {x['code']} {x['name']}｜{x['role']}｜{x['theme']}｜已封板/无法确认成交"
            )
            continue

        if x["lim"]>0 and x["cur"]>=x["lim"]-0.011:
            events.append(f"UNFILLED {x['code']} {x['name']}｜接近封死")
            continue

        fill=x["cur"]*(1+CFG["slippage_bp"]/10000)
        if x["lim"]>0 and fill>=x["lim"]:
            events.append(f"UNFILLED {x['code']} {x['name']}｜滑点后触及涨停")
            continue

        budget=min(
            CFG["initial_cash"]*CFG["position_fraction"],
            st["cash"]*0.99
        )
        shares=int(budget/fill/100)*100
        if shares<=0:
            continue

        amount=shares*fill
        fee=amount*CFG["commission_bp_one_way"]/10000
        if amount+fee>st["cash"]:
            continue

        st["cash"]-=amount+fee
        tag=f"{x['role']}|{x['theme']}|题材封板{x['theme_sealed']}|S19"
        st["positions"][x["code"]]={
            "name":x["name"],"shares":shares,"entry":fill,
            "entry_date":dt.strftime("%Y-%m-%d"),
            "stop":round(fill*CFG["stop_pct"],3),"tag":tag
        }
        append_csv(TRADES_P,[
            dt.isoformat(),"BUY",x["code"],x["name"],f"{fill:.3f}",shares,
            f"{amount:.2f}",f"{fee:.2f}","S19_ENTRY",tag,
            f"{st['cash']:.2f}"
        ])
        events.append(
            f"BUY {x['code']} {x['name']} {shares}股 @ {fill:.3f}｜{tag}"
        )
    return events

def mark_equity(st,dt):
    mv=0.0
    for code,p in st["positions"].items():
        q=quote_simple(code)
        px=(q or {}).get("price") or p["entry"]
        mv+=p["shares"]*px
    eq=st["cash"]+mv
    append_csv(EQUITY_P,[
        dt.isoformat(),f"{st['cash']:.2f}",f"{mv:.2f}",
        f"{eq:.2f}",len(st["positions"])
    ])
    return mv,eq

def write_report(st,dt,cands,events,mv,eq):
    OUT_P.parent.mkdir(parents=True,exist_ok=True)
    lines=[
        "# S19 20万模拟仓","",
        f"- 更新时间：{dt:%Y-%m-%d %H:%M:%S} 北京时间",
        f"- 初始资金：¥{CFG['initial_cash']:,.0f}",
        f"- 现金：¥{st['cash']:,.2f}",
        f"- 持仓市值：¥{mv:,.2f}",
        f"- 总权益：¥{eq:,.2f}",
        f"- 收益率：{(eq/CFG['initial_cash']-1)*100:.2f}%","",
        "## 本次 S19 候选"
    ]
    if cands:
        for x in cands:
            lines.append(
                f"- {x['code']} {x['name']}｜{x['role']}｜题材 {x['theme']}｜"
                f"题材封板 {x['theme_sealed']}｜连板 {x['streak']}｜"
                f"当前 {'封板' if x['sealed'] else '可交易'}"
            )
    else:
        lines.append("- 无")

    lines+=["","## 本次执行"]
    if events:
        lines += [f"- {e}" for e in events]
    else:
        lines.append("- 无成交/无需要执行的订单")

    lines+=["","## 当前持仓"]
    if st["positions"]:
        for c,p in st["positions"].items():
            lines.append(
                f"- {c} {p['name']}｜{p['shares']}股｜成本 {p['entry']:.3f}｜"
                f"止损 {p['stop']:.3f}｜{p['tag']}"
            )
    else:
        lines.append("- 空仓")

    lines += [
        "",
        "> 说明：S19 只负责选股；GitHub Actions 执行层对已经封死的股票不假设能成交，"
        "因此可能漏掉强板，但不会把“炸板回封”加入 S19。"
    ]
    OUT_P.write_text("\n".join(lines)+"\n",encoding="utf-8")

def main():
    dt=now_bj()
    st=load_state()
    events=[]

    if dt.weekday()>=5:
        print("weekend, skip")
        return 0

    process_exits(st,dt)
    cands=[]
    strict_cands=[]
    hm=dt.strftime("%H:%M")

    if CFG["monitor_start"]<=hm<=CFG["scan_end"]:
        try:
            pool=fetch_pool(dt.strftime("%Y%m%d"))
            cands=select_s19(pool, broad=True)
            strict_cands=select_s19(pool, broad=False)
            if hm >= CFG["entry_start"]:
                events=process_entries(st,cands,dt)
            else:
                events=[
                    f"AUCTION_WATCH {x['code']} {x['name']}｜{x['role']}｜{x['theme']}｜"
                    f"{'已封板' if x['sealed'] else '竞价观察'}"
                    for x in cands
                ]
        except Exception as e:
            events=[f"DATA_ERROR {type(e).__name__}: {e}"]

    st["last_scan_date"]=dt.strftime("%Y-%m-%d")
    st["last_scan_time"]=dt.strftime("%H:%M:%S")
    mv,eq=mark_equity(st,dt)
    save_state(st)
    push_dual_signals(strict_cands,cands,events,dt)
    write_report(st,dt,cands,events,mv,eq)

    print("\n".join(events) if events else "no trade")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
