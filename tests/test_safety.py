import json
import sys
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import s19_paper as s
import run_signal_continuous as signal
import run_continuous as paper

DT=datetime(2026,10,9,10,0,tzinfo=s.BJ)

def quote(**changes):
    q=dict(price=10,prev_close=10,open=10,volume=10000,bid=9.99,bid_size=1000,
           ask=10.01,ask_size=1000,upper=11,lower=9,timestamp=DT)
    q.update(changes)
    return q

class Safety(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ('TRADES_P','STATE_P','EQUITY_P','OUT_P','PUSH_STATE_P'):
            p=patch.object(s,name,Path(self.tmp.name)/name);p.start();self.addCleanup(p.stop)
        journal_root=patch.object(signal,'ROOT',Path(self.tmp.name));journal_root.start();self.addCleanup(journal_root.stop)
        self.clock=patch.object(s,'now_bj',return_value=DT);self.clock.start();self.addCleanup(self.clock.stop)
    def test_holidays_and_holding_sessions(self):
        self.assertFalse(s.is_session('2026-10-01'))
        self.assertEqual(s.trade_days_held('2026-09-30',DT.date()),2)
    def test_market_hours(self):
        for hour,minute in [(9,15),(12,0),(15,0),(14,58)]:
            self.assertFalse(s.trading_time(DT.replace(hour=hour,minute=minute)))
        self.assertTrue(s.trading_time(DT))
    def test_stale_quote_rejected(self):
        self.assertFalse(s.valid_quote(quote(timestamp=DT-timedelta(seconds=61)),DT))
        self.assertFalse(s.valid_quote(quote(timestamp=DT-timedelta(days=1)),DT))
    def test_locked_book_and_depth(self):
        for q,side in [(quote(price=9,bid=9),'SELL'),(quote(price=11,ask=11),'BUY'),
                       (quote(ask_size=0),'BUY'),(quote(bid_size=1),'SELL')]:
            with patch.object(s,'quote_simple',return_value=q):
                self.assertIsNone(s.execution_quote('600001',side,200,DT))
    def test_execution_slippage_both_sides(self):
        with patch.object(s,'quote_simple',return_value=quote()):
            self.assertGreater(s.execution_quote('600001','BUY',100,DT)[1],10.01)
            self.assertLess(s.execution_quote('600001','SELL',100,DT)[1],9.99)
    def test_t_plus_one_and_preopen(self):
        st={'cash':0,'positions':{'600001':dict(entry_date='2026-10-09',stop=11,shares=100,name='demo')}}
        with patch.object(s,'quote_simple') as fetch:
            s.process_exits(st,DT)
            s.process_exits(st,DT.replace(hour=9,minute=15))
            fetch.assert_not_called()
        self.assertIn('600001',st['positions'])
    def test_no_sell_at_lower_limit(self):
        st={'cash':0,'positions':{'600001':dict(entry_date='2026-09-30',stop=10,shares=100,name='demo')}}
        with patch.object(s,'quote_simple',return_value=quote(price=9,bid=9)):
            s.process_exits(st,DT)
        self.assertEqual(st['cash'],0)
        self.assertIn('600001',st['positions'])
    def test_successful_sell_fee(self):
        st={'cash':0,'positions':{'600001':dict(entry_date='2026-09-30',stop=10.5,shares=100,name='demo')}}
        with patch.object(s,'quote_simple',return_value=quote()):
            events=s.process_exits(st,DT)
        self.assertTrue(events[0].startswith('SELL'))
        self.assertFalse(st['positions'])
        self.assertAlmostEqual(st['cash'],998*(1-8/10000))
    def test_acceptance_missing_rejected(self):
        self.assertFalse(s.acceptance({'preview':[]}))
    def test_preview_percent_is_price_relative(self):
        self.assertEqual(s.parse_preview([0,10,-5]),[1.0,1.1,0.95])
        # A drop from +10% to +9% is <1% in price, not a 10% price retreat.
        self.assertTrue(s.acceptance({'preview':s.parse_preview([0,10,9,9])}))
    def test_closing_valuation_retains_stale_label(self):
        st={'cash':0,'positions':{'600001':dict(entry=10,shares=100)}}
        with patch.object(s,'quote_simple',return_value=None):
            _,eq=s.mark_equity(st,DT)
        self.assertEqual(eq,1000)
        self.assertEqual(st['valuation_stale_codes'],['600001'])
    def test_second_leader(self):
        items=[dict(code=str(600001+i),name='demo',reason_type='theme',high_days=f'{4-i}板',
                    latest=11,limit_up_price=11,change_rate=10,time_preview=[10,10,10,11],first_limit_up_time=str(i)) for i in range(4)]
        self.assertEqual([x['role'] for x in s.select_s19(items)],['龙头','二龙头'])
    def test_twenty_percent_sealed(self):
        self.assertTrue(s.normalize(dict(code='300001',latest=12,change_rate=20))['sealed'])
    def test_business_error_and_missing_schema(self):
        for payload in ({'status_code':1},{'data':{}}):
            with patch.object(s.requests,'get',return_value=Mock(json=lambda:payload)):
                with self.assertRaises(ValueError): s.fetch_pool('20261009')
    def test_pagination(self):
        responses=[Mock(json=lambda:{'data':{'info':[{'code':'600001'}],'page':{'total':2}}}),
                   Mock(json=lambda:{'data':{'info':[{'code':'600002'}],'page':{'total':2}}})]
        with patch.object(s.requests,'get',side_effect=responses):
            self.assertEqual(len(s.fetch_pool('20261009')),2)
    def test_incomplete_pagination(self):
        with patch.object(s.requests,'get',return_value=Mock(json=lambda:{'data':{'info':[],'page':{'total':2}}})):
            with self.assertRaises(ValueError): s.fetch_pool('20261009')
    def test_persistent_dedup_and_retry(self):
        with patch.object(s,'serverchan_send',return_value=True) as send:
            s.push_signals([],[],DT)
            s.push_signals([],[],DT)
            self.assertEqual(send.call_count,1)
        s.PUSH_STATE_P.unlink()
        with patch.object(s,'serverchan_send',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError): s.push_signals([],[],DT)
        self.assertFalse(s.PUSH_STATE_P.exists())
    def test_push_failure_is_scan_failure(self):
        with patch.object(s,'fetch_pool',return_value=[]),patch.object(s,'serverchan_send',side_effect=RuntimeError('failed')):
            self.assertFalse(signal.run_once())
    def test_paper_data_error_nonzero(self):
        with patch.object(s,'fetch_pool',side_effect=ValueError('bad payload')):
            self.assertEqual(s.main(),1)
        self.assertEqual(json.loads(s.STATE_P.read_text())['cash'],200000)
    def test_empty_pool_recovers_before_notification(self):
        rows=[{'code':'600001'}]*24
        health={}
        with patch.object(s,'fetch_pool',side_effect=[rows,[],rows]),patch.object(s.time,'sleep'):
            self.assertEqual(s.fetch_live_pool(DT,health),rows)
            self.assertEqual(s.fetch_live_pool(DT,health),rows)
        self.assertTrue(health['pool_seen_nonempty'])
    def test_persistent_empty_after_nonempty_is_error(self):
        health={'pool_date':'2026-10-09','pool_seen_nonempty':True}
        with patch.object(s,'fetch_pool',return_value=[]),patch.object(s.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'unexpectedly empty'):
                s.fetch_live_pool(DT,health)
    def test_empty_at_new_day_not_previous_day_error(self):
        health={'pool_date':'2026-10-08','pool_seen_nonempty':True}
        with patch.object(s,'fetch_pool',return_value=[]),patch.object(s.time,'sleep'):
            self.assertEqual(s.fetch_live_pool(DT,health),[])
        self.assertFalse(health['pool_seen_nonempty'])
    def test_signal_empty_anomaly_never_sends_no_signal(self):
        s.save_push_state({'date':'2026-10-09','sent':[],
                           'pool_date':'2026-10-09','pool_seen_nonempty':True})
        with patch.object(s,'fetch_pool',return_value=[]),patch.object(s.time,'sleep'),patch.object(s,'serverchan_send',return_value=True) as send:
            self.assertFalse(signal.run_once())
            self.assertEqual(send.call_count,1)
            self.assertEqual(send.call_args.args[0],'S19 数据获取异常')
        self.assertNotIn('STATUS|NO_SIGNAL',s.load_push_state(DT)['sent'])
    def test_paper_empty_anomaly_is_failure_without_old_signals(self):
        s.save_state({'cash':200000,'positions':{},'pool_date':'2026-10-09','pool_seen_nonempty':True})
        with patch.object(s,'fetch_pool',return_value=[]),patch.object(s.time,'sleep'),patch.object(s,'process_entries') as entries:
            self.assertEqual(s.main(),1)
            entries.assert_not_called()
        self.assertEqual(json.loads(s.STATE_P.read_text())['cash'],200000)
    def test_signal_runner_waits_and_scans(self):
        times=[DT.replace(hour=8,minute=45),DT.replace(hour=9,minute=14),
               DT.replace(hour=9,minute=15),DT.replace(hour=9,minute=15),DT.replace(hour=10,minute=58)]
        with patch.object(s,'now_bj',side_effect=times),patch.dict('os.environ',{'SERVERCHAN_SENDKEY':'test'}),patch.object(signal,'run_once',return_value=True) as scan,patch.object(signal.time,'sleep'):
            self.assertEqual(signal.main(),0)
            self.assertEqual(scan.call_count,1)
    def test_signal_runner_late_fails(self):
        with patch.object(s,'now_bj',return_value=DT.replace(hour=11)),patch.dict('os.environ',{'SERVERCHAN_SENDKEY':'test'}),patch.object(signal,'run_once') as scan:
            self.assertEqual(signal.main(),1)
            scan.assert_not_called()
    def test_signal_holiday_skips(self):
        with patch.object(s,'now_bj',return_value=DT.replace(day=1)),patch.dict('os.environ',{'SERVERCHAN_SENDKEY':'test'}),patch.object(signal,'run_once') as scan:
            self.assertEqual(signal.main(),0)
            scan.assert_not_called()
    def test_paper_runner_late_fails(self):
        with patch.object(s,'now_bj',return_value=DT.replace(hour=15,minute=2)),patch.object(paper,'run_once') as scan:
            self.assertEqual(paper.main(),1)
            scan.assert_not_called()
    def test_paper_runner_afternoon_exit_and_close(self):
        times=[DT.replace(hour=13),DT.replace(hour=13),DT.replace(hour=13),
               DT.replace(hour=13),DT.replace(hour=15,minute=1),DT.replace(hour=15,minute=1),
               DT.replace(hour=15,minute=2)]
        with patch.object(s,'now_bj',side_effect=times),patch.object(paper,'run_once',return_value=0) as scan,patch.object(paper.time,'sleep'):
            self.assertEqual(paper.main(),0)
            self.assertEqual(scan.call_count,2)
    def test_signal_snapshots_survive_push_failure(self):
        with patch.object(s,'fetch_pool',return_value=[{'code':'600001'}]),patch.object(s,'select_s19',return_value=[]),patch.object(s,'serverchan_send',side_effect=RuntimeError('failed')):
            self.assertFalse(signal.run_once())
        records=[json.loads(line) for line in (signal.ROOT/'logs/signals/2026-10-09.jsonl').read_text().splitlines()]
        self.assertEqual(records[0]['status'],'SCAN_OK')
        self.assertEqual(records[0]['pool'][0]['code'],'600001')
        self.assertEqual(records[1]['status'],'ERROR')
    def test_serverchan_rejects_bad_payload_and_redacts_url(self):
        with patch.dict('os.environ',{'SERVERCHAN_SENDKEY':'fake-secret'}):
            with patch.object(s.requests,'post',return_value=Mock(json=lambda:{'code':1})):
                with self.assertRaises(RuntimeError): s.serverchan_send('test','test')
            with patch.object(s.requests,'post',side_effect=RuntimeError('https://fake-secret.send')):
                with self.assertRaises(RuntimeError) as error: s.serverchan_send('test','test')
                self.assertNotIn('fake-secret',str(error.exception))
    def test_quote_parser(self):
        arr=['0']*49
        for index,value in {1:'demo',2:'600001',3:'10',4:'10',5:'10',6:'10000',9:'9.99',10:'1000',19:'10.01',20:'1000',30:'20261009100000',47:'11',48:'9'}.items(): arr[index]=value
        with patch.object(s.requests,'get',return_value=Mock(content=('~'.join(arr)).encode('gbk'))):
            self.assertTrue(s.valid_quote(s.quote_simple('600001'),DT))

if __name__=='__main__': unittest.main()
