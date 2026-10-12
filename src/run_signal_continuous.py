#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare a next-session S19 watchlist, then monitor only that list intraday."""
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import market_gate
import s19_paper as s19

INTERVAL = 5
POOL_PATH = ROOT / "state" / "premarket_candidates.json"
QUOTE_MAX_AGE = int(s19.CFG["max_quote_age_seconds"])


def hm(dt):
    return dt.strftime("%H:%M")


def journal(dt, record):
    try:
        path = ROOT / "logs" / "signals" / f"{dt:%Y-%m-%d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": dt.isoformat(), **record}, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"JOURNAL_ERROR {type(exc).__name__}", flush=True)


def previous_session(day):
    for offset in range(1, 15):
        candidate = day - timedelta(days=offset)
        if s19.is_session(candidate):
            return candidate
    raise RuntimeError("could not find previous XSHG session within 14 days")


def prepare_premarket_pool(dt):
    """Select tomorrow's watchlist strictly from the last completed session."""
    source_day = previous_session(dt.date())
    last_error = None
    rows = None
    for attempt in range(3):
        try:
            rows = s19.fetch_pool(source_day.strftime("%Y%m%d"), timeout=8)
            break
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if rows is None:
        raise RuntimeError(f"previous-session pool unavailable: {type(last_error).__name__}")
    candidates = s19.select_s19(rows)
    payload = {
        "session_date": dt.date().isoformat(),
        "source_session_date": source_day.isoformat(),
        "prepared_at": dt.isoformat(),
        "source": "previous_session_close_limit_up_pool",
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    s19.atomic_json(POOL_PATH, payload)
    journal(dt, {"status": "PREMARKET_POOL", **payload})
    print(
        f"PREMARKET_POOL_READY session={payload['session_date']} "
        f"source={payload['source_session_date']} candidates={len(candidates)}",
        flush=True,
    )
    return candidates, source_day.isoformat()


def send_once(key, title, body, dt):
    state = s19.load_push_state(dt)
    sent = set(state.get("sent", []))
    if key in sent:
        return False
    s19.serverchan_send(title, body)
    sent.add(key)
    state["sent"] = sorted(sent)
    s19.save_push_state(state)
    print(f"PUSH_OK {key}", flush=True)
    return True


def preseal_gap_points(quote):
    """Distance from upper limit, in percentage points of previous close."""
    if not quote or quote.get("prev_close", 0) <= 0:
        return None
    return (quote["upper"] - quote["price"]) / quote["prev_close"] * 100


def run_once(candidates=None, source_day=None):
    dt = s19.now_bj()
    if not s19.is_session(dt.date()):
        return True
    if not (s19.CFG["monitor_start"] <= hm(dt) <= s19.CFG["scan_end"]):
        return True
    try:
        if candidates is None:
            candidates, source_day = prepare_premarket_pool(dt)
        if not candidates:
            send_once(
                "STATUS|EMPTY_PREMARKET_POOL",
                "S19 盘前候选池｜暂无候选",
                f"交易日：{dt:%Y-%m-%d}\n候选池来源：前一交易日（{source_day or '未知'}）收盘数据。\n本日没有符合 S19 盘前筛选条件的候选。",
                dt,
            )
            return True

        gate = market_gate.get_gate(dt, s19.CFG)
        s19.push_market_gate_warning(gate, dt)
        evaluated = []
        pushed = []
        for candidate in candidates:
            code = candidate["code"]
            quote = s19.quote_simple(code)
            if not s19.valid_quote(quote, dt, max_age=QUOTE_MAX_AGE):
                evaluated.append({"code": code, "status": "QUOTE_STALE_OR_INVALID"})
                continue

            gap = preseal_gap_points(quote)
            if gap is None:
                evaluated.append({"code": code, "status": "LIMIT_OR_PREVIOUS_CLOSE_MISSING"})
                continue

            in_auction = hm(dt) < s19.CFG["entry_start"]
            sealed = quote["price"] >= quote["upper"] - 0.005
            status = "AUCTION_WATCH" if in_auction else ("UNFILLED" if sealed else "SIGNAL")
            key = f"{code}|{status}"
            quote_age = max(0.0, (dt - quote["timestamp"]).total_seconds())
            if in_auction:
                title = f"S19 竞价观察｜{candidate.get('name', code)}"
                stage_note = "集合竞价参考价可能变化；这是候选观察，不代表买入指令或保证成交。"
            elif sealed:
                title = f"S19 已封板/未确认可成交｜{candidate.get('name', code)}"
                stage_note = "当前行情显示已到涨停价，不代表能够成交。"
            else:
                title = f"S19 候选信号｜{candidate.get('name', code)}"
                stage_note = "这是 S19 候选信号，不代表买入指令或保证成交。"
            body = (
                f"信号发现时间：{dt:%H:%M:%S}（北京时间）\\n\\n"
                f"行情时间：{quote['timestamp']:%H:%M:%S}（北京时间），"
                f"行情年龄：{quote_age:.1f} 秒\\n\\n"
                f"股票：{candidate.get('name', '')} {code}\\n\\n"
                f"盘前题材：{candidate.get('theme', '')}；"
                f"角色：{candidate.get('role', '')}；"
                f"前一交易日连板：{candidate.get('streak', 0)}\\n\\n"
                f"现价：{quote['price']:.2f}；涨停价：{quote['upper']:.2f}\\n\\n"
                f"距涨停：{gap:.2f} 个百分点（仅行情展示，不作为筛选条件）\\n\\n"
                f"盘前池来源：{source_day or '未知'} 收盘数据\\n\\n"
                f"{market_gate.description(gate)}\\n\\n"
                f"{stage_note}"
            )
            if send_once(key, title, body, dt):
                pushed.append(code)

            evaluated.append({
                "code": code, "status": status, "price": quote["price"],
                "upper": quote["upper"], "prev_close": quote["prev_close"],
                "gap_points": round(gap, 4),
                "quote_time": quote["timestamp"].isoformat(),
            })

        journal(dt, {
            "status": "PREMARKET_SCAN",
            "source_session_date": source_day,
            "candidate_count": len(candidates),
            "evaluated": evaluated,
            "pushed": pushed,
        })
        print(
            f"PREMARKET_SCAN_OK {dt:%Y-%m-%d %H:%M:%S} "
            f"watch={len(candidates)} preseal_pushes={len(pushed)}",
            flush=True,
        )
        return True
    except Exception as exc:
        event = f"DATA_ERROR {type(exc).__name__}: {exc}"
        print(event, flush=True)
        journal(dt, {"status": "ERROR", "error": event})
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

    try:
        candidates, source_day = prepare_premarket_pool(dt)
    except Exception as exc:
        event = f"DATA_ERROR {type(exc).__name__}: {exc}"
        print(event, flush=True)
        journal(dt, {"status": "ERROR", "error": event})
        try:
            s19.push_signals([], [event], dt)
        except Exception as push_exc:
            print(f"STATUS_PUSH_ERROR {type(push_exc).__name__}: {push_exc}", flush=True)
        return 1

    print(
        f"SIGNAL_START {dt.isoformat()} window={s19.CFG['monitor_start']}–"
        f"{s19.CFG['scan_end']} interval={INTERVAL}s",
        flush=True,
    )
    while hm(s19.now_bj()) < s19.CFG["monitor_start"]:
        time.sleep(min(INTERVAL, 15))

    failed = False
    scans = 0
    while hm(s19.now_bj()) <= s19.CFG["scan_end"]:
        scans += 1
        failed = (not run_once(candidates, source_day)) or failed
        time.sleep(INTERVAL)

    print("S19 premarket scan window closed", flush=True)
    return 1 if failed or scans == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
