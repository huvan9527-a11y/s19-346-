#!/usr/bin/env python3
"""Long-lived unified scanner, split into morning/afternoon Actions sessions."""
import argparse
import os
import time
from datetime import datetime, timedelta

import market_gate
import s19_paper as s
from live_session import Engine, PoolFeed, fetch_quotes


def phase_window(day, phase):
    start, end = ('09:15:00', '11:31:00') if phase == 'morning' else ('12:55:00', '15:01:00')
    return (datetime.fromisoformat(day + 'T' + start).replace(tzinfo=s.BJ),
            datetime.fromisoformat(day + 'T' + end).replace(tzinfo=s.BJ))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=['auto', 'morning', 'afternoon'], default='auto')
    parser.add_argument('--once', action='store_true', help='one real collection cycle; still session/time gated')
    parser.add_argument('--no-notify', action='store_true')
    args = parser.parse_args(argv)
    now = s.now_bj()
    if not s.is_session(now.date()):
        print('SESSION_SKIP non-trading day ' + now.date().isoformat(), flush=True)
        return 0
    phase = ('morning' if now.hour < 12 else 'afternoon') if args.phase == 'auto' else args.phase
    start, end = phase_window(now.date().isoformat(), phase)
    if now > end:
        print('SESSION_MISSED phase=%s received=%s expected_end=%s' % (phase, now.isoformat(), end.isoformat()), flush=True)
        return 1
    while s.now_bj() < start:
        time.sleep(10)
    notify = not args.no_notify and bool(os.getenv('SERVERCHAN_SENDKEY', '').strip())
    engine = Engine(s.ROOT, notify=notify)
    engine.begin(s.now_bj(), phase)
    engine.checkpoint(s.now_bj())
    feed = PoolFeed(s.CFG)
    interval = s.CFG.get('session_interval_seconds', 5)
    checkpoint_interval = s.CFG.get('checkpoint_seconds', 300)
    terminal_error = None
    heartbeat_at = None
    try:
        while s.now_bj() <= end:
            now = s.now_bj()
            if now.date() != start.date():
                raise RuntimeError('session crossed date; refusing to reuse prior-day inputs')
            hm = now.strftime('%H:%M')
            # Persistent gate worker keeps index current into afternoon and at the close.
            gate = market_gate.get_gate(now, s.CFG)
            candidates, pool_ok = [], False
            pool_error = None
            if hm <= s.CFG['scan_end']:
                feed.request(now)
                snapshot, pool_error = feed.snapshot(now)
                candidates, pool_ok = engine.pool(now, snapshot, pool_error)
                if snapshot and snapshot.get('rows') is not None:
                    engine.state['last_pool_size'] = len(snapshot['rows'])
            codes = engine.needed_codes(candidates)
            quote_start = s.now_bj()
            quotes, quote_error = fetch_quotes(codes)
            # Evaluate freshness after network I/O, not at request start.
            now = s.now_bj()
            gate = market_gate.get_gate(now, s.CFG)
            engine.journal.emit(now, 'QUOTE_BATCH', request_at=quote_start,
                                received_at=now, codes=codes, quotes=quotes, error=quote_error)
            # A slow quote request cannot extend the pool's receipt TTL.
            if hm <= s.CFG['scan_end']:
                _, post_error = feed.snapshot(now)
                if post_error:
                    candidates, pool_ok = [], False
                    pool_error = post_error
            engine.step(now, candidates, pool_ok, gate, quotes, quote_error)
            terminal_error = quote_error if codes and not quotes else pool_error if now.strftime('%H:%M') <= s.CFG['scan_end'] else None
            if terminal_error:
                print('DATA_ERROR ' + terminal_error, flush=True)
            if heartbeat_at is None or now >= heartbeat_at:
                engine.journal.emit(now, 'HEARTBEAT', phase=phase, stats=engine.stats,
                                    pending=len(engine.state['pending_intents']), data_error=terminal_error)
                print('SESSION_HEARTBEAT %s %s' % (now.isoformat(), engine.stats), flush=True)
                heartbeat_at = now + timedelta(minutes=5)
            if engine.last_checkpoint is None or (now - engine.last_checkpoint).total_seconds() >= checkpoint_interval:
                engine.checkpoint(now)
            if args.once:
                break
            time.sleep(interval)
    finally:
        if engine.notifications.worker is not None:
            engine.notifications.worker.join(timeout=16)
        now = s.now_bj()
        engine.notifications.drain(now)
        if phase == 'morning':
            engine.state['morning_completed_at'] = now.isoformat()
        engine.state['session_finished_at'] = now.isoformat()
        engine.state.setdefault('session_history', {}).setdefault(now.date().isoformat(), {})[phase] = dict(engine.stats)
        engine.journal.emit(now, 'SESSION_END', phase=phase, stats=engine.stats,
                            terminal_error=terminal_error,
                            status='degraded' if engine.stats['pool_errors'] or engine.stats['quote_errors'] else 'healthy')
        engine.checkpoint(now, final=True)
    print('SESSION_SUMMARY phase=%s stats=%s terminal_error=%s' % (phase, engine.stats, terminal_error), flush=True)
    # Transient recovered failures remain visible in the journal/report rather than
    # turning every otherwise completed day red; unrecovered/no coverage still fails.
    unacknowledged = any(v['status'] != 'sent' for v in engine.state['notifications'].values() if v['created_at'].startswith(start.date().isoformat()))
    missed_entries = phase == 'morning' and engine.stats['entry_coverage_start'] is None
    missing_delivery_config = not args.no_notify and not notify
    return 1 if terminal_error or not engine.stats['cycles'] or engine.state.get('valuation_stale_codes') or missed_entries or unacknowledged or missing_delivery_config else 0


if __name__ == '__main__':
    raise SystemExit(main())
