import copy
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import s19_paper as s
import market_gate
import live_session as live
import runtime_store as store
import run_session

DT = datetime(2026, 10, 23, 9, 35, tzinfo=s.BJ)

def quote(dt=DT, **kw):
    q = dict(name='demo', price=10., prev_close=10., open=10., volume=10000,
             bid=9.99, bid_size=10000, ask=10.01, ask_size=10000,
             upper=11., lower=9., timestamp=dt)
    q.update(kw)
    return q


def candidate(code='600001', **kw):
    x = dict(code=code, name='demo', cur=10., lim=11., sealed=False, locked=False,
             theme='theme', role='龙头', theme_sealed=4, streak=2)
    x.update(kw)
    return x


def gate(allowed=True):
    return dict(allowed=allowed, index_pct=1. if allowed else -1.,
                index_time=DT.isoformat(), reason='ok', opening_limit_down=None, date='2026-10-23')


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.object(market_gate, 'read_cache', return_value={})
        self.patch.start();self.addCleanup(self.patch.stop)
        self.engine = live.Engine(self.root, state={'cash':200000.,'positions':{}}, notify=False, run_id='test')
        self.engine.begin(DT, 'morning')

    def step(self, dt=DT, cand=None, q=None, allowed=True, pool_ok=True):
        self.engine.step(dt, [candidate()] if cand is None else cand, pool_ok, gate(allowed),
                         {'600001':quote(dt) if q is None else q})

    def events(self):
        return [json.loads(line) for path in sorted(self.engine.journal.files) for line in path.read_text().splitlines()]

    def test_no_same_snapshot_fill_then_strictly_new_source_fills(self):
        self.step()
        self.assertEqual(self.engine.state['cash'],200000)
        self.assertEqual(len(self.engine.state['pending_intents']),1)
        self.step(DT+timedelta(seconds=5),q=quote(DT))
        self.assertFalse(self.engine.state['positions'])
        self.step(DT+timedelta(seconds=10))
        state=self.engine.state
        self.assertEqual(len(state['positions']),1)
        self.assertFalse(state['pending_intents'])
        fill=next(x for x in self.events() if x['kind']=='PAPER_FILL')
        self.assertGreater(fill['fill_quote']['timestamp'],fill['signal_quote_time'])
        self.assertIn(fill['fill_id'],state['fill_ids'])
        self.assertAlmostEqual(state['cash'],200000-fill['amount']-fill['fee'])


    def test_intents_and_fills_checkpoint_immediately_before_periodic_save(self):
        self.step()
        checkpoint=json.loads((self.root/'state/checkpoint.json').read_text())
        self.assertEqual(len(checkpoint['account']['pending_intents']),1)
        self.step(DT+timedelta(seconds=5))
        checkpoint=json.loads((self.root/'state/checkpoint.json').read_text())
        self.assertTrue(checkpoint['account']['positions'])
        self.assertEqual(checkpoint['account']['fill_ids'],self.engine.state['fill_ids'])
        self.assertEqual(checkpoint['account']['cash'],self.engine.state['cash'])
        for item in checkpoint['journal']+checkpoint['exports']:
            data=(self.root/item['path']).read_bytes()[:item['size']]
            self.assertEqual(hashlib.sha256(data).hexdigest(),item['sha256'])

    def test_failed_pool_cancels_buy_but_sell_continues(self):
        self.step()
        self.step(DT+timedelta(seconds=5),pool_ok=False,cand=[])
        self.assertFalse(self.engine.state['pending_intents'])
        self.engine.state['positions']['600001']=dict(name='demo',entry=11,stop=10.5,shares=100,entry_date='2026-10-22')
        self.step(DT+timedelta(seconds=10),pool_ok=False,cand=[],allowed=False)
        pending=list(self.engine.state['pending_intents'].values())
        self.assertEqual(pending[0]['side'],'SELL')
        self.step(DT+timedelta(seconds=15),pool_ok=False,cand=[],allowed=False)
        self.assertFalse(self.engine.state['positions'])
        self.assertTrue(any(e['kind']=='PAPER_FILL' and e['side']=='SELL' for e in self.events()))

    def test_gate_blocks_before_order_and_source_checks(self):
        self.step(allowed=False)
        self.assertFalse(self.engine.state['pending_intents'])
        self.step(DT+timedelta(seconds=5),q=quote(DT-timedelta(seconds=100)))
        self.assertFalse(self.engine.state['pending_intents'])

    def test_sealed_and_depth_never_assumed_full_fill(self):
        self.step(cand=[candidate(sealed=True)],q=quote(price=11,ask=11))
        self.assertFalse(self.engine.state['pending_intents'])
        self.step(DT+timedelta(seconds=5),q=quote(DT+timedelta(seconds=5),ask_size=1))
        self.assertFalse(self.engine.state['pending_intents'])

    def test_t_plus_one_even_if_stop_hit(self):
        self.engine.state['positions']['600001']=dict(name='demo',entry=11,stop=10.5,shares=100,entry_date='2026-10-23')
        self.step(cand=[],allowed=False)
        self.assertFalse(self.engine.state['pending_intents'])

    def test_order_expiry_and_signal_price_cap(self):
        self.step()
        first=list(self.engine.state['pending_intents'])[0]
        self.step(DT+timedelta(seconds=5),q=quote(DT+timedelta(seconds=5),ask=10.5))
        self.assertNotIn(first,self.engine.state['pending_intents'])
        self.assertTrue(any(e['kind']=='INTENT_CANCELLED' and e['reason']=='PRICE_ABOVE_SIGNAL_CAP' for e in self.events()))
        self.assertFalse(self.engine.state['positions'])

    def test_empty_after_nonempty_never_becomes_no_signal(self):
        rows=[dict(code=str(600001+i),reason_type='theme',high_days='2板',latest=11,limit_up_price=11,time_preview=[0,1,5,10]) for i in range(4)]
        snap=dict(request_id=1,started_at=DT,completed_at=DT,date='2026-10-23',rows=rows,error=None)
        _,ok=self.engine.pool(DT,snap,None);self.assertTrue(ok)
        snap.update(request_id=2,rows=[])
        _,ok=self.engine.pool(DT+timedelta(seconds=5),snap,None);self.assertFalse(ok)

    def test_checkpoint_account_evidence_and_export_prefixes_are_coherent(self):
        self.step();self.engine.checkpoint(DT)
        cp=json.loads((self.root/'state/checkpoint.json').read_text())
        self.step(DT+timedelta(seconds=5))
        # The old checkpoint remains valid even after subsequent journal/CSV appends.
        for item in cp['journal']+cp['exports']:
            data=(self.root/item['path']).read_bytes()[:item['size']]
            self.assertEqual(hashlib.sha256(data).hexdigest(),item['sha256'])
        self.assertFalse(cp['account']['positions'])
        self.assertTrue(self.engine.state['positions'])

    def test_cash_reservation_caps_three_positions(self):
        cs=[candidate(str(600001+i)) for i in range(5)]
        qs={x['code']:quote() for x in cs}
        self.engine.step(DT,cs,True,gate(),qs)
        self.assertEqual(len(self.engine.state['pending_intents']),3)
        reserved=sum(x['shares']*x['limit_price']*1.0003 for x in self.engine.state['pending_intents'].values())
        self.assertLessEqual(reserved,200000)

    def test_stale_gate_never_reported_as_current(self):
        cache={'date':'2026-10-23','index':{'pct':-4.55,'time':DT.isoformat()}}
        result=market_gate.evaluate(cache,DT+timedelta(hours=5),s.CFG)
        self.assertIsNone(result['index_pct'])
        self.assertIsNone(result['index_time'])


class DeliveryTests(unittest.TestCase):
    def test_retry_backoff_no_false_ack_and_no_duplicate_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state={};j=live.Journal(tmp,'delivery')
            out=live.Notifications(state,j,s.CFG)
            out.add(DT,'600001|SIGNAL','title','body')
            with patch.object(s,'serverchan_send',side_effect=RuntimeError('HTTPError')) as send:
                out.start(DT);out.worker.join(1);out.drain(DT)
                self.assertEqual(send.call_count,1)
                self.assertEqual(next(iter(state['notifications'].values()))['status'],'pending')
                out.start(DT+timedelta(seconds=5));self.assertEqual(send.call_count,1)
            later=DT+timedelta(seconds=61)
            with patch.object(s,'serverchan_send',return_value=True) as send:
                out.start(later);out.worker.join(1);out.drain(later)
                restored=json.loads(json.dumps(state))
                newer=live.Notifications(restored,j,s.CFG)
                newer.add(later,'600001|SIGNAL','title','body');newer.start(later)
                self.assertEqual(send.call_count,1)
                self.assertEqual(next(iter(restored['notifications'].values()))['status'],'sent')

    def test_expired_signals_are_not_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=live.Notifications({},live.Journal(tmp,'expiry'),s.CFG)
            out.add(DT,'SIGNAL','title','body',expires_seconds=10)
            with patch.object(s,'serverchan_send') as send:
                out.start(DT+timedelta(seconds=11));send.assert_not_called()
            self.assertEqual(next(iter(out.state['notifications'].values()))['status'],'expired')


class FeedAndRunnerTests(unittest.TestCase):
    def test_stalled_pool_returns_without_blocking_exit_loop(self):
        feed=live.PoolFeed(s.CFG)
        feed.result=dict(request_id=1,completed_at=DT-timedelta(seconds=40),date='2026-10-23',rows=[],error=None)
        _,error=feed.snapshot(DT)
        self.assertIn('expired',error)

    def test_batch_quote_identity_and_missing_codes(self):
        arr=['0']*49
        for i,v in {1:'demo',2:'600001',3:'10',4:'10',5:'10',6:'10000',9:'9.99',10:'100',19:'10.01',20:'200',30:'20261023093500',47:'11',48:'9'}.items():arr[i]=v
        q=live.parse_quotes('v_sh600001="'+'~'.join(arr)+'";', ['600001'])
        self.assertEqual(q['600001']['ask_size'],200)
        self.assertTrue(s.valid_quote(q['600001'],DT))
        arr[2]='600002'
        self.assertEqual(live.parse_quotes('v_sh600001="'+'~'.join(arr)+'";', ['600001']),{})

    def test_holiday_and_missed_session_do_not_send_or_reset(self):
        with patch.object(s,'now_bj',return_value=DT.replace(day=24)),patch.object(run_session,'Engine') as eng:
            self.assertEqual(run_session.main([]),0);eng.assert_not_called()
        with patch.object(s,'now_bj',return_value=DT.replace(hour=12)),patch.object(run_session,'Engine') as eng:
            self.assertEqual(run_session.main(['--phase','morning']),1);eng.assert_not_called()

    def test_windows_fit_under_hosted_job_limit(self):
        for phase in ('morning','afternoon'):
            start,end=run_session.phase_window('2026-10-23',phase)
            self.assertLess((end-start).total_seconds(),4*3600)


class RuntimeStoreTests(unittest.TestCase):
    def test_atomic_checkpoint_publish_restore_with_local_git_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);repo=tmp/'code';bare=tmp/'remote.git';work=tmp/'runtime'
            subprocess.run(['git','init','--bare',str(bare)],check=True,capture_output=True)
            subprocess.run(['git','init','-b','main',str(repo)],check=True,capture_output=True)
            def git(*args):return subprocess.run(['git',*args],cwd=repo,check=True,text=True,capture_output=True)
            git('config','user.name','test');git('config','user.email','test@example.com')
            (repo/'state').mkdir();(repo/'state/account.json').write_text(json.dumps({'cash':200000,'positions':{}}))
            git('add','.');git('commit','-m','seed');git('remote','add','origin',str(bare));git('push','origin','main')
            engine=live.Engine(repo,state={'cash':123456,'positions':{}},notify=False,run_id='restore')
            engine.begin(DT,'morning');engine.checkpoint(DT)
            with patch.object(store,'ROOT',repo),patch.object(store,'WORK',work):
                self.assertTrue(store.save())
                # Simulate stale main checkout on afternoon startup.
                (repo/'state/account.json').write_text(json.dumps({'cash':200000,'positions':{}}))
                store.restore()
                restored=json.loads((repo/'state/account.json').read_text())
                self.assertEqual(restored['cash'],123456)
                gz=list((work/'logs/sessions').rglob('*.gz'))
                self.assertTrue(gz)
                self.assertEqual(json.loads(gzip.decompress(gz[0].read_bytes()).splitlines()[0])['kind'],'SESSION_START')


if __name__=='__main__':unittest.main()

class SessionIntegrationTests(unittest.TestCase):
    def test_complete_shared_session_does_not_start_subprocess_scans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'state').mkdir()
            (root/'state/account.json').write_text(json.dumps({'cash':200000,'positions':{}}))
            clock=[DT]
            class Feed:
                def __init__(self,*args):pass
                def request(self,now):pass
                def snapshot(self,now):
                    return dict(request_id=1,started_at=DT,completed_at=DT,date='2026-10-23',rows=[],error=None),None
            def advance(seconds):clock[0]+=timedelta(seconds=seconds)
            with patch.object(s,'ROOT',root),patch.object(s,'now_bj',side_effect=lambda:clock[0]),\
                 patch.object(run_session,'phase_window',return_value=(DT-timedelta(seconds=1),DT+timedelta(seconds=12))),\
                 patch.object(run_session,'PoolFeed',Feed),patch.object(run_session,'fetch_quotes',return_value=({},None)),\
                 patch.object(market_gate,'get_gate',return_value=gate()),patch.object(market_gate,'read_cache',return_value={}),\
                 patch.object(run_session.time,'sleep',side_effect=advance):
                self.assertEqual(run_session.main(['--phase','morning','--no-notify']),0)
            cp=json.loads((root/'state/checkpoint.json').read_text())
            self.assertTrue(cp['final'])
            self.assertEqual(cp['account']['health']['cycles'],3)
            self.assertEqual(cp['account']['cash'],200000)
            self.assertIn('morning',cp['account']['session_history']['2026-10-23'])
            self.assertTrue(cp['journal'])
