"""One evidence stream for S19 selection, notifications and next-snapshot paper fills.

No broker orders. Selection remains s19_paper.select_s19 on the CURRENT pool.
All source timestamps, intents, fills and delivery attempts are journalled.
"""
import csv
import hashlib
import json
import math
import os
import queue
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import requests
import market_gate
import s19_paper as s

SCHEMA = 's19-session-v2'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=lambda x: x.isoformat() if isinstance(x, datetime) else str(x), allow_nan=False)


def iso(dt):
    return dt.isoformat()


class Journal:
    def __init__(self, root, run_id=None):
        self.root = Path(root)
        self.run_id = run_id or ((os.environ['GITHUB_RUN_ID'] + '-' + os.getenv('GITHUB_RUN_ATTEMPT', '1')) if os.getenv('GITHUB_RUN_ID') else uuid.uuid4().hex)
        self.seq = 0
        self.files = set()
        self.last_time = None

    def emit(self, dt, kind, **payload):
        if self.last_time is not None and dt < self.last_time:
            raise RuntimeError('journal clock moved backwards; refusing historical/live mix')
        self.last_time = dt
        self.seq += 1
        # Append-only five-minute chunks. State checkpoints freeze byte prefixes.
        name = '%02d%02d-%s.jsonl' % (dt.hour, dt.minute // 5 * 5, self.run_id)
        path = self.root / 'logs' / 'sessions' / dt.date().isoformat() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {'schema': SCHEMA, 'event_id': '%s:%s' % (self.run_id, self.seq),
                  'time': iso(dt), 'kind': kind, **payload}
        with path.open('a', encoding='utf-8') as stream:
            stream.write(encoded(record) + '\n')
        self.files.add(path)
        return record['event_id']

    def manifest(self):
        out = []
        for path in sorted(self.files):
            data = path.read_bytes()
            out.append({'path': str(path.relative_to(self.root)), 'size': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()})
        return out


class PoolFeed:
    """Network pool reads cannot block exit monitoring or process initialization."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.worker = None
        self.result = None
        self.started = None
        self.request_number = 0

    def request(self, now):
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                return
            if self.result and (now - self.result['completed_at']).total_seconds() < self.cfg.get('pool_refresh_seconds', 15):
                return
            self.request_number += 1
            number = self.request_number
            self.started = now
            self.worker = threading.Thread(target=self._fetch, args=(now, number), daemon=True)
            self.worker.start()

    def _fetch(self, started, number):
        try:
            rows = s.fetch_pool(started.strftime('%Y%m%d'), timeout=5)
            error = None
        except Exception as exc:
            rows = None
            # No credential-bearing URLs are used by this read-only data source.
            error = '%s: %s' % (type(exc).__name__, exc)
        result = {'request_id': number, 'started_at': started, 'completed_at': s.now_bj(),
                  'date': started.date().isoformat(), 'rows': rows, 'error': error}
        with self.lock:
            self.result = result

    def snapshot(self, now):
        with self.lock:
            result = dict(self.result) if self.result else None
            started = self.started
            running = self.worker is not None and self.worker.is_alive()
        if result is None:
            return None, 'pool request pending'
        if result['date'] != now.date().isoformat():
            return result, 'pool date mismatch'
        if result['error']:
            return result, result['error']
        if (now - result['completed_at']).total_seconds() > self.cfg.get('pool_max_age_seconds', 30):
            return result, 'pool receipt expired'
        if running and started and (now - started).total_seconds() > self.cfg.get('pool_max_age_seconds', 30):
            return result, 'pool refresh stalled'
        return result, None


def parse_quotes(text, codes):
    result = {}
    expected = set(codes)
    for match in re.finditer(r'v_(?:sh|sz)(\d{6})="([^"\n]*)"', text):
        code, raw = match.groups()
        if code not in expected:
            continue
        fields = raw.split('~')
        if len(fields) < 49 or fields[2] != code:
            continue
        try:
            stamp = datetime.strptime(fields[30], '%Y%m%d%H%M%S').replace(tzinfo=s.BJ)
            result[code] = {'name': fields[1], 'price': s.fnum(fields[3]),
                            'prev_close': s.fnum(fields[4]), 'open': s.fnum(fields[5]),
                            'volume': s.fnum(fields[6]), 'bid': s.fnum(fields[9]),
                            'bid_size': s.fnum(fields[10]), 'ask': s.fnum(fields[19]),
                            'ask_size': s.fnum(fields[20]), 'upper': s.fnum(fields[47]),
                            'lower': s.fnum(fields[48]), 'timestamp': stamp}
        except ValueError:
            continue
    return result


def fetch_quotes(codes):
    codes = sorted(set(codes))
    if not codes:
        return {}, None
    # A single batch shares a receipt time; each symbol retains its source timestamp.
    symbols = [('sh' if c.startswith('6') else 'sz') + c for c in codes]
    try:
        response = requests.get('https://qt.gtimg.cn/q=' + ','.join(symbols),
                                headers={'User-Agent': s.UA, 'Referer': 'https://gu.qq.com/'}, timeout=(2, 4))
        response.raise_for_status()
        result = parse_quotes(response.content.decode('gbk', 'strict'), codes)
        missing = sorted(set(codes) - set(result))
        return result, ('quote batch missing: ' + ','.join(missing)) if missing else None
    except Exception as exc:
        return {}, 'quote batch %s' % type(exc).__name__


class Notifications:
    """Durable outbox; delivery network errors never interrupt the trading scan."""
    def __init__(self, state, journal, cfg, enabled=True):
        self.state = state
        self.journal = journal
        self.cfg = cfg
        self.enabled = enabled
        self.results = queue.Queue()
        self.worker = None
        self.active = None
        self.state.setdefault('notifications', {})

    def add(self, now, key, title, body, expires_seconds=900):
        if not self.enabled:
            return
        fullkey = now.date().isoformat() + '|' + key
        if fullkey in self.state['notifications']:
            return
        # Keep old acknowledgements across phase restarts but not unbounded history.
        self.state['notifications'][fullkey] = {'key': fullkey, 'title': title,
            'body': body + '\n\n记录编号：' + fullkey, 'status': 'pending',
            'created_at': iso(now), 'expires_at': iso(now + timedelta(seconds=expires_seconds)),
            'attempts': 0, 'next_attempt': iso(now), 'live_signal': expires_seconds <= 900}
        self.journal.emit(now, 'NOTIFICATION_QUEUED', key=fullkey)

    def drain(self, now):
        while True:
            try:
                key, error = self.results.get_nowait()
            except queue.Empty:
                break
            item = self.state['notifications'][key]
            item['attempts'] += 1
            if error is None:
                item.update(status='sent', sent_at=iso(now))
                print('PUSH_OK ' + key, flush=True)
                self.journal.emit(now, 'NOTIFICATION_SENT', key=key, attempt=item['attempts'])
            else:
                delay = min(900, 30 * 2 ** min(item['attempts'], 5))
                item['next_attempt'] = iso(now + timedelta(seconds=delay))
                item['last_error'] = error
                self.journal.emit(now, 'NOTIFICATION_FAILED', key=key, attempt=item['attempts'], error=error)
            self.active = None

    def start(self, now):
        self.drain(now)
        if not self.enabled or (self.worker is not None and self.worker.is_alive()):
            return
        for key, item in self.state['notifications'].items():
            if item['status'] != 'pending':
                continue
            if datetime.fromisoformat(item['expires_at']) < now:
                item['status'] = 'expired'
                self.journal.emit(now, 'NOTIFICATION_EXPIRED', key=key)
                continue
            if datetime.fromisoformat(item['next_attempt']) > now:
                continue
            self.active = key
            def send(key=key, title=item['title'], body=item['body']):
                try:
                    s.serverchan_send(title, body)
                    error = None
                except Exception as exc:
                    # serverchan_send already redacts the credential-bearing URL.
                    error = '%s: %s' % (type(exc).__name__, exc)
                self.results.put((key, error))
            self.worker = threading.Thread(target=send, daemon=True)
            self.worker.start()
            return


def fill_price(quote, side, shares, now, cfg):
    if not s.trading_time(now) or not s.valid_quote(quote, now, cfg['max_quote_age_seconds']):
        return None, 'QUOTE_STALE_OR_INVALID'
    book = 'ask' if side == 'BUY' else 'bid'
    price = quote.get(book, 0)
    if price <= 0 or quote.get(book + '_size', 0) * 100 < shares:
        return None, 'BOOK_INSUFFICIENT'
    if side == 'BUY' and (quote['price'] >= quote['upper'] - .005 or price >= quote['upper'] - .005):
        return None, 'UPPER_LIMIT'
    if side == 'SELL' and (quote['price'] <= quote['lower'] + .005 or price <= quote['lower'] + .005):
        return None, 'LOWER_LIMIT'
    slipped = price * (1 + (1 if side == 'BUY' else -1) * cfg['slippage_bp'] / 10000)
    px = (math.ceil(slipped * 100) if side == 'BUY' else math.floor(slipped * 100)) / 100
    if not quote['lower'] < px < quote['upper']:
        return None, 'SLIPPAGE_OUTSIDE_LIMIT'
    return px, None


class Engine:
    def __init__(self, root, cfg=None, state=None, notify=True, run_id=None):
        self.root = Path(root)
        self.cfg = dict(cfg or s.CFG)
        self.journal = Journal(root, run_id)
        self.state = state if state is not None else json.loads((self.root / 'state/account.json').read_text())
        if not isinstance(self.state.get('positions'), dict) or self.state.get('cash', -1) < 0:
            raise ValueError('account schema invalid; refusing cash reset')
        self.state.setdefault('pending_intents', {})
        self.state.setdefault('notifications', {})
        self.state.setdefault('fill_ids', [])
        self.state['schema'] = SCHEMA
        # If a legacy instant-fill account has positions, retain them verbatim.
        self.notifications = Notifications(self.state, self.journal, self.cfg, notify)
        self.last_pool_id = None
        self.candidates = []
        self.last_checkpoint = None
        self.error = None
        self.stats = {'cycles': 0, 'pool_errors': 0, 'quote_errors': 0, 'fills': 0,
                      'first_cycle': None, 'last_cycle': None, 'entry_coverage_start': None}

    def begin(self, now, phase):
        self.phase = phase
        self.state['engine_day'] = now.date().isoformat()
        self.state['phase'] = phase
        self.state.setdefault('day_activity', {})
        self.state['day_activity'] = {k: v for k, v in self.state['day_activity'].items() if k >= (now - timedelta(days=7)).date().isoformat()}
        self.journal.emit(now, 'SESSION_START', phase=phase, strategy='CURRENT_LIVE_POOL',
                          runtime=SCHEMA, previous_account_at=self.state.get('last_scan_time'),
                          notification_enabled=self.notifications.enabled,
                          opening_limit_down_required=False,
                          note='Stock/theme/signal rules unchanged; next-source-snapshot paper execution')
        # Undelivered old alerts must not be presented as new live signals.
        for item in self.state['notifications'].values():
            if item.get('status') == 'pending' and item.get('live_signal', True) and not item['created_at'].startswith(now.date().isoformat()):
                item['status'] = 'expired'
        self.state['notifications'] = {k: v for k, v in self.state['notifications'].items()
            if v['created_at'][:10] >= (now - timedelta(days=7)).date().isoformat()}

    def pool(self, now, snapshot, error):
        if snapshot and snapshot['request_id'] != self.last_pool_id:
            self.last_pool_id = snapshot['request_id']
            self.pool_event = self.journal.emit(now, 'POOL_SNAPSHOT', **snapshot)
        if error:
            if error != 'pool request pending':
                self.stats['pool_errors'] += 1
            self.journal.emit(now, 'POOL_UNAVAILABLE', error=error)
            return [], False
        if snapshot is None:
            return [], False
        rows = snapshot['rows']
        day = now.date().isoformat()
        if self.state.get('pool_date') != day:
            self.state.update(pool_date=day, pool_seen_nonempty=False)
        if rows:
            self.state['pool_seen_nonempty'] = True
        elif self.state['pool_seen_nonempty']:
            self.stats['pool_errors'] += 1
            self.journal.emit(now, 'POOL_UNAVAILABLE', error='unexpected empty pool after known nonempty')
            return [], False
        return s.select_s19(rows), True

    def needed_codes(self, candidates):
        return sorted(set(x['code'] for x in candidates) | set(self.state['positions']) |
                      set(x['code'] for x in self.state['pending_intents'].values()))

    def activity(self, now, code):
        return self.state.setdefault('day_activity', {}).setdefault(now.date().isoformat(), {}).setdefault(code, {})

    def cancel(self, now, intent_id, reason):
        intent = self.state['pending_intents'].pop(intent_id)
        self.journal.emit(now, 'INTENT_CANCELLED', intent=intent, reason=reason)
        row = self.activity(now, intent['code'])
        row.update(last_state='CANCELLED', last_reason=reason)

    def create_intent(self, now, side, code, shares, quote, reason, candidate=None):
        intent_id = uuid.uuid4().hex
        event = self.journal.emit(now, 'INTENT_CREATED', intent_id=intent_id, side=side,
            code=code, shares=shares, reason=reason, quote=quote, candidate=candidate)
        cap = min(quote['upper'] - .01, math.floor(quote['ask'] * 1.002 * 100) / 100) if side == 'BUY' else None
        self.state['pending_intents'][intent_id] = {'id': intent_id, 'signal_event': event,
            'side': side, 'code': code, 'shares': shares, 'reason': reason,
            'created_at': iso(now), 'signal_quote_time': iso(quote['timestamp']),
            'limit_price': cap, 'candidate': candidate}
        row = self.activity(now, code)
        row.update(last_state='PENDING_' + side, last_reason=reason)
        row.setdefault('first_intent_at', iso(now))
        return intent_id

    def commit_fill(self, now, intent_id, quote, price):
        intent = self.state['pending_intents'][intent_id]
        if intent_id in self.state['fill_ids']:
            self.cancel(now, intent_id, 'ALREADY_FILLED')
            return
        code, shares, side = intent['code'], intent['shares'], intent['side']
        amount = shares * price
        fee = amount * (self.cfg['commission_bp_one_way'] + (self.cfg['sell_tax_bp'] if side == 'SELL' else 0)) / 10000
        candidate = intent.get('candidate') or {}
        if side == 'BUY':
            if amount + fee > self.state['cash'] or code in self.state['positions']:
                self.cancel(now, intent_id, 'CASH_OR_POSITION_CHANGED')
                return
        elif code not in self.state['positions']:
            self.cancel(now, intent_id, 'POSITION_MISSING')
            return
        # Evidence first, then one atomic account transition including fill-id deduplication.
        fill_event = self.journal.emit(now, 'PAPER_FILL', fill_id=intent_id, signal_event=intent['signal_event'],
            signal_quote_time=intent['signal_quote_time'], fill_quote=quote, side=side, code=code,
            shares=shares, price=price, amount=amount, fee=fee,
            approximation='next strictly newer source L1 quote; not exchange execution')
        if side == 'BUY':
            self.state['cash'] -= amount + fee
            tag = '%s|%s|题材封板%s|S19' % (candidate['role'], candidate['theme'], candidate['theme_sealed'])
            self.state['positions'][code] = {'name': candidate['name'], 'shares': shares,
                'entry': price, 'entry_date': now.date().isoformat(), 'stop': round(price * self.cfg['stop_pct'], 3),
                'tag': tag, 'fill_id': intent_id, 'fill_event': fill_event}
        else:
            self.state['cash'] += amount - fee
            tag = self.state['positions'].pop(code).get('tag', '')
        self.state['fill_ids'].append(intent_id)
        self.state['pending_intents'].pop(intent_id)
        self.stats['fills'] += 1
        row = self.activity(now, code)
        row.update(last_state='FILLED_' + side, last_reason=intent['reason'])
        row.setdefault('fills', []).append({'side': side, 'time': iso(now), 'price': price, 'shares': shares, 'fill_id': intent_id})
        self.notifications.add(now, code + '|' + side + '|' + intent_id,
            'S19 模拟%s｜%s' % ('买入' if side == 'BUY' else '卖出', quote['name']),
            '%s\n股票：%s\n数量：%s；模拟价：%.2f\n成交依据：下一次更新的买卖一盘口；非实盘成交。\n原因：%s' %
            (iso(now), code, shares, price, intent['reason']), expires_seconds=86400)
        # This account is authoritative; CSV is a human-readable export recoverable from journal fills.
        self.save_account(now)
        trade_path = self.root / 'logs/trades.csv'
        trade_path.parent.mkdir(parents=True, exist_ok=True)
        with trade_path.open('a', newline='', encoding='utf-8') as stream:
            csv.writer(stream).writerow([iso(now), side, code, quote['name'], '%.2f' % price,
                shares, '%.2f' % amount, '%.2f' % fee, intent['reason'], tag, '%.2f' % self.state['cash']])

    def step(self, now, candidates, pool_ok, gate, quotes, quote_error=None):
        self.notifications.drain(now)
        self.stats['cycles'] += 1
        self.stats['first_cycle'] = self.stats['first_cycle'] or iso(now)
        self.stats['last_cycle'] = iso(now)
        hm = now.strftime('%H:%M')
        entry_window = self.cfg['entry_start'] <= hm <= self.cfg['scan_end'] and s.trading_time(now)
        if entry_window and pool_ok and self.stats['entry_coverage_start'] is None:
            self.stats['entry_coverage_start'] = iso(now)
            expected = now.replace(hour=9, minute=30, second=0, microsecond=0)
            self.stats['opening_delay_seconds'] = max(0, (now - expected).total_seconds())
        self.candidates = candidates
        self.state['market_gate'] = gate
        if gate['index_pct'] is not None:
            self.state['last_valid_gate'] = dict(gate)
        evidence = self.journal.emit(now, 'SCAN', candidates=candidates,
            pool_event=getattr(self, 'pool_event', None), pool_ok=pool_ok,
            gate=gate, gate_cache=market_gate.read_cache(), quotes=quotes, quote_error=quote_error)
        if quote_error:
            self.stats['quote_errors'] += 1
        by_code = {x['code']: x for x in candidates}
        for code, item in by_code.items():
            row = self.activity(now, code)
            row.setdefault('first_selected_at', iso(now))
            row.setdefault('first_evidence', evidence)
            row.update(name=item['name'], theme=item['theme'], role=item['role'])
            row['selection_scans'] = row.get('selection_scans', 0) + 1
        # Existing intents only. Newly generated signals cannot fill from their own source snapshot.
        for intent_id, intent in list(self.state['pending_intents'].items()):
            code, side = intent['code'], intent['side']
            if side == 'BUY':
                if not entry_window or not pool_ok or not gate['allowed'] or code not in by_code:
                    self.cancel(now, intent_id, 'SIGNAL_OR_GATE_INVALID')
                    continue
                if by_code[code]['sealed'] or by_code[code]['locked']:
                    self.cancel(now, intent_id, 'CANDIDATE_SEALED')
                    continue
                if (now - datetime.fromisoformat(intent['created_at'])).total_seconds() >= self.cfg.get('intent_timeout_seconds', 300):
                    self.cancel(now, intent_id, 'TIMEOUT')
                    continue
            quote = quotes.get(code)
            if not quote:
                self.journal.emit(now, 'INTENT_WAIT', intent_id=intent_id, reason='QUOTE_MISSING')
                continue
            if quote['timestamp'] <= datetime.fromisoformat(intent['signal_quote_time']):
                self.journal.emit(now, 'INTENT_WAIT', intent_id=intent_id, reason='SOURCE_TIMESTAMP_NOT_ADVANCED')
                continue
            price, reason = fill_price(quote, side, intent['shares'], now, self.cfg)
            if reason:
                self.journal.emit(now, 'INTENT_WAIT', intent_id=intent_id, reason=reason)
                continue
            if side == 'BUY' and price > intent['limit_price']:
                self.cancel(now, intent_id, 'PRICE_ABOVE_SIGNAL_CAP')
                continue
            if side == 'SELL':
                pos = self.state['positions'].get(code)
                if not pos or s.trade_days_held(pos['entry_date'], now.date()) < 1:
                    continue
            self.commit_fill(now, intent_id, quote, price)
        pending_sides = {(x['code'], x['side']) for x in self.state['pending_intents'].values()}
        for code, pos in list(self.state['positions'].items()):
            held = s.trade_days_held(pos['entry_date'], now.date())
            quote = quotes.get(code)
            if held < 1 or (code, 'SELL') in pending_sides or not s.valid_quote(quote, now):
                continue
            reason = 'STOP' if quote['price'] <= pos['stop'] else ('HOLD5' if held >= self.cfg['hold_days'] else None)
            if reason and s.trading_time(now):
                self.create_intent(now, 'SELL', code, pos['shares'], quote, reason)
        if entry_window and pool_ok:
            if gate['index_pct'] is not None and gate['index_pct'] <= 0:
                self.notifications.add(now, 'STATUS|MARKET_GATE|INDEX_NONPOSITIVE',
                    'S19 风险提醒｜禁止模拟新开仓', self.gate_text(gate), expires_seconds=600)
            for x in candidates:
                code = x['code']
                quote = quotes.get(code)
                if not s.valid_quote(quote, now):
                    continue
                stage = 'UNFILLED' if x['sealed'] or x['locked'] or quote['price'] >= quote['upper'] - .005 else 'SIGNAL'
                self.notifications.add(now, code + '|' + stage, 'S19 %s｜%s' % ('已封板/未成交' if stage == 'UNFILLED' else '候选信号', x['name']),
                    '%s\n%s %s｜%s｜%s\n题材封板%s只；连板%s\n%s\n候选来自当日实时池；提醒不等于模拟买入。\n扫描证据：%s' %
                    (iso(now), code, x['name'], x['theme'], x['role'], x['theme_sealed'], x['streak'], self.gate_text(gate), evidence), expires_seconds=120)
                if not gate['allowed'] or stage == 'UNFILLED' or x.get('cur', 0) <= 0 or code in self.state['positions']:
                    continue
                pending_buy = [v for v in self.state['pending_intents'].values() if v['side'] == 'BUY']
                if any(v['code'] == code for v in pending_buy) or len(self.state['positions']) + len(pending_buy) >= self.cfg['max_positions']:
                    continue
                reserved = sum(v['shares'] * v['limit_price'] * (1 + self.cfg['commission_bp_one_way'] / 10000) for v in pending_buy)
                budget = min(self.cfg['initial_cash'] * self.cfg['position_fraction'], max(0, self.state['cash'] - reserved) * .99)
                # Same L1/slippage and full-size checks as the former instant fill path.
                indicative, reason = fill_price(quote, 'BUY', 100, now, self.cfg)
                if reason:
                    continue
                shares = int(budget / indicative / 100) * 100
                if shares <= 0 or quote['ask_size'] * 100 < shares:
                    continue
                self.create_intent(now, 'BUY', code, shares, quote, 'S19_ENTRY', x)
            if not candidates and hm >= '09:35':
                self.notifications.add(now, 'STATUS|NO_SIGNAL', 'S19 扫描正常｜本轮暂无候选',
                    iso(now) + '\n当日实时池已成功获取，本轮没有通过全部S19条件的股票。后续继续监控。', expires_seconds=120)
        self.mark(now, quotes)
        self.state['last_scan_date'] = now.date().isoformat()
        self.state['last_scan_time'] = now.strftime('%H:%M:%S')
        self.state['health'] = dict(self.stats, pool_ok=pool_ok, quote_error=quote_error)
        self.save_account(now)
        self.report(now)
        # State/outbox are durable locally before any network send is dispatched.
        self.notifications.start(now)
        print('SCAN_OK %s pool=%s candidates=%s' % (now.strftime('%Y-%m-%d %H:%M:%S'), self.state.get('last_pool_size', -1), len(candidates)), flush=True)

    @staticmethod
    def gate_text(gate):
        return market_gate.description(gate) + '\n指数行情时间：%s；这是当时读数，不是收盘值。' % gate.get('index_time', '不可用')

    def mark(self, now, quotes):
        mv = 0
        stale = []
        for code, pos in self.state['positions'].items():
            quote = quotes.get(code)
            if s.valid_quote(quote, now, 180 if now.strftime('%H:%M') >= '15:00' else self.cfg['max_quote_age_seconds']):
                pos.update(last_mark=quote['price'], last_mark_time=iso(quote['timestamp']))
            else:
                stale.append(code)
            mv += pos['shares'] * pos.get('last_mark', pos['entry'])
        self.state['valuation_stale_codes'] = stale
        self.state['equity'] = self.state['cash'] + mv
        self.state['valuation_at'] = iso(now)
        path = self.root / 'logs/equity.csv'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', newline='', encoding='utf-8') as stream:
            csv.writer(stream).writerow([iso(now), '%.2f' % self.state['cash'], '%.2f' % mv,
                                        '%.2f' % self.state['equity'], len(self.state['positions'])])

    def save_account(self, now):
        self.state['saved_at'] = iso(now)
        s.atomic_json(self.root / 'state/account.json', self.state)

    def report(self, now):
        state = self.state
        gate = state.get('market_gate') or {}
        notifications = state['notifications'].values()
        lines = ['# S19 统一实时模拟仓', '', '- 更新时间：' + iso(now),
            '- 候选口径：当日实时涨停池；推送与模拟仓共享本轮候选和行情证据。',
            '- 现金：%.2f；总权益：%.2f；持仓：%s；待确认意向：%s。' %
            (state['cash'], state.get('equity', state['cash']), len(state['positions']), len(state['pending_intents'])),
            '- 估值状态：' + ('含旧估值：' + ','.join(state['valuation_stale_codes']) if state.get('valuation_stale_codes') else '有效'),
            '- ' + self.gate_text(gate) if gate else '- 门控尚未采集。',
            '- 开盘跌停家数限制：当前未启用，保持本次修复前实际执行口径。',
            '- 通知：%s；成功%s，待重试%s，已过期%s。' %
            ('启用' if self.notifications.enabled else '关闭/密钥未配置',
             sum(x['status'] == 'sent' for x in notifications), sum(x['status'] == 'pending' for x in notifications),
             sum(x['status'] == 'expired' for x in notifications)),
            '- 运行覆盖与错误：`' + encoded(self.stats) + '`', '', '## 本轮候选', '']
        lines += ['- %s %s｜%s｜%s｜题材封板%s｜连板%s' %
            (x['code'], x['name'], x['theme'], x['role'], x['theme_sealed'], x['streak']) for x in self.candidates] or ['- 无；需结合上面的数据状态判断，不能把数据失败当成无信号。']
        lines += ['', '## 今日首次候选与模拟执行', '', '| 股票 | 首次入选 | 题材/角色 | 模拟成交 | 最近状态/原因 |', '|---|---|---|---|---|']
        for code, row in sorted(state.get('day_activity', {}).get(now.date().isoformat(), {}).items()):
            fills = '；'.join('%s %s股@%.2f' % (x['side'], x['shares'], x['price']) for x in row.get('fills', [])) or '无'
            cells = [code + ' ' + row.get('name', ''), row.get('first_selected_at', '持仓退出'),
                     row.get('theme', '') + '/' + row.get('role', ''), fills,
                     row.get('last_state', '观察') + '/' + row.get('last_reason', '')]
            lines.append('| ' + ' | '.join(str(x).replace('|', '/') for x in cells) + ' |')
        lines += ['', '- 分时段覆盖：`' + encoded(state.get('session_history', {})) + '`']
        lines += ['', '## 持仓与待确认意向', '', '```json', encoded({'positions': state['positions'], 'pending_intents': state['pending_intents']}), '```', '',
            '> 仅模拟：信号后必须出现源时间严格更新的下一次盘口才能记成交。盘口快照不能证明实盘排队/成交。']
        path = self.root / 'outputs/latest.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def checkpoint(self, now, final=False):
        self.notifications.drain(now)
        self.save_account(now)
        self.report(now)
        exports = []
        for name in ('logs/trades.csv', 'logs/equity.csv'):
            path = self.root / name
            if path.exists():
                data = path.read_bytes()
                exports.append({'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        s.atomic_json(self.root / 'state/checkpoint.json', {'schema': SCHEMA, 'saved_at': iso(now),
            'run_id': self.journal.run_id, 'final': final, 'account': self.state,
            'report': (self.root / 'outputs/latest.md').read_text(encoding='utf-8'),
            'journal': self.journal.manifest(), 'exports': exports})
        self.last_checkpoint = now
