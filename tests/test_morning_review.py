import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from morning_review import parse_logs,render

class Review(unittest.TestCase):
    def test_today_empty_glitch_and_push(self):
        log='\n'.join([
          '2026-10-09T02:30:29.000Z SIGNAL_SCAN_OK 2026-10-09 10:30:29 pool=24 candidates=1',
          '2026-10-09T02:30:35.000Z SIGNAL_SCAN_OK 2026-10-09 10:30:35 pool=0 candidates=0',
          '2026-10-09T02:30:44.000Z PUSH_OK STATUS|NO_SIGNAL',
          '2026-10-09T02:30:43.000Z SIGNAL_SCAN_OK 2026-10-09 10:30:43 pool=24 candidates=1'])
        rows=[dict(row,run=1,job=2,workflow='signal') for row in parse_logs(log,'2026-10-09')]
        report=render('2026-10-09',[dict(run=1,url='https://github.com/test',workflow='signal',attempt=1,conclusion='success')],rows,[])
        self.assertIn('疑似短暂数据异常',report)
        self.assertIn('STATUS|NO_SIGNAL',report)
        self.assertEqual(rows[2]['time'],'10:30:44')
    def test_other_day_afternoon_and_code_echo_ignored(self):
        rows=parse_logs('2026-10-08T02:00:00Z PUSH_OK 600001|SIGNAL\n2026-10-09T05:00:00Z PUSH_OK 600001|SIGNAL\n2026-10-09T02:00:00Z print("DATA_ERROR ")','2026-10-09')
        self.assertEqual(rows,[])
    def test_no_data_is_not_no_signal(self):
        report=render('2026-10-09',[],[],[])
        self.assertIn('不能据此认定没有信号',report)
    def test_paper_scan(self):
        rows=parse_logs('2026-10-09T02:00:00Z SCAN_OK 2026-10-09 10:00:00 pool=20 candidates=2','2026-10-09')
        self.assertEqual(rows[0]['candidates'],2)
