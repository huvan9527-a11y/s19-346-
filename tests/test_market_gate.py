import json
import sys
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import market_gate as g
import s19_paper as s
import run_signal_continuous as signal
DT=datetime(2026,10,12,9,35,tzinfo=g.BJ)
CFG={'max_open_limit_down':10,'market_index_max_age_seconds':90}
def cache(pct=1,count=10):
 return {'date':'2026-10-12','index':{'pct':pct,'time':DT.isoformat()},'opening':{'date':'2026-10-12','count':count}}
class Gate(unittest.TestCase):
 def test_thresholds(self):
  self.assertTrue(g.evaluate(cache(),DT,CFG)['allowed'])
  self.assertFalse(g.evaluate(cache(pct=0),DT,CFG)['allowed'])
  self.assertFalse(g.evaluate(cache(pct=-0.1),DT,CFG)['allowed'])
  self.assertFalse(g.evaluate(cache(count=11),DT,CFG)['allowed'])
 def test_missing_stale_and_previous_day(self):
  self.assertFalse(g.evaluate({},DT,CFG)['allowed'])
  self.assertFalse(g.evaluate(cache(),DT+timedelta(seconds=91),CFG)['allowed'])
  self.assertFalse(g.evaluate(cache(),DT+timedelta(days=1),CFG)['allowed'])
 def test_index_parse(self):
  x={'bk_883958':{'date':'20261012','pre':'100','data':'0935,101,0,0,0'}}
  self.assertAlmostEqual(g.parse_index(x,DT)['pct'],1)
  with self.assertRaises(ValueError):g.parse_index(x,DT+timedelta(days=1))
 def test_complete_universe_and_beijing_exclusion(self):
  pages=[[{'symbol':'sh600001'},{'symbol':'bj920001'}],[{'symbol':'sz000001'}]]
  self.assertEqual(len(g.validate_universe(pages,3)),2)
  with self.assertRaises(ValueError):g.validate_universe(pages,4)
  with self.assertRaises(ValueError):g.validate_universe([[{'symbol':'sh600001'}]*2],2)
 def test_open_at_lower_even_if_rebounded(self):
  arr=['0']*49
  arr[2]='600001';arr[3]='10';arr[5]='9';arr[30]='20261012093500';arr[48]='9'
  text='v_sh600001="'+'~'.join(arr)+'";'
  self.assertTrue(g.parse_open_quotes(text,['sh600001'],'2026-10-12')['sh600001'])
  with self.assertRaises(ValueError):g.parse_open_quotes(text,['sh600001','sz000001'],'2026-10-12')
  with self.assertRaises(ValueError):g.parse_open_quotes(text,['sh600001'],'2026-10-13')
 def test_entries_blocked_before_quote(self):
  st={'cash':200000,'positions':{}}
  gate=g.evaluate(cache(count=11),DT,CFG)
  with patch.object(s.market_gate,'get_gate',return_value=gate),patch.object(s,'execution_quote') as quote:
   events=s.process_entries(st,[{'code':'600001'}],DT)
   quote.assert_not_called()
   self.assertTrue(events[0].startswith('ENTRY_BLOCKED'))
  self.assertEqual(st['cash'],200000)
 def test_signals_unfiltered_and_warning_dedup(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(s,'PUSH_STATE_P',Path(tmp)/'push.json'),patch.object(s,'serverchan_send',return_value=True) as send:
   gate=g.evaluate(cache(pct=-1,count=11),DT,CFG)
   candidate=dict(code='600001',name='demo',theme='theme',role='龙头',theme_sealed=4,streak=2,sealed=True,locked=False)
   s.push_signals([candidate],[],DT,gate=gate)
   self.assertEqual(send.call_count,1)
   self.assertIn('禁止模拟新开仓',send.call_args.args[1])
   s.push_market_gate_warning(gate,DT);s.push_market_gate_warning(gate,DT)
   self.assertEqual(send.call_count,2)
   s.push_signals([candidate],[],DT,gate=gate)
   self.assertEqual(send.call_count,2)
 def test_background_refresh_not_blocking(self):
  with patch.object(g,'read_cache',return_value={}),patch.object(g.threading,'Thread') as worker:
   result=g.get_gate(DT,CFG)
   worker.return_value.start.assert_called_once()
   self.assertFalse(result['allowed'])
  g._WORKER=None
