"""Market entry gate; asynchronous refresh keeps signal delivery independent."""
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

BJ=ZoneInfo('Asia/Shanghai')
CACHE=Path(__file__).resolve().parents[1]/'state/market_gate.json'
_LOCK=threading.Lock()
_WORKER=None
HEADERS={'User-Agent':'Mozilla/5.0','Referer':'https://q.10jqka.com.cn/'}
SINA='https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.'

def finite(value):
    result=float(value)
    if not math.isfinite(result): raise ValueError('nonfinite market value')
    return result

def jsonp(text):
    start=text.find('(');end=text.rfind(')')
    if start<0 or end<=start: raise ValueError('invalid JSONP')
    return json.loads(text[start+1:end])

def parse_index(payload,dt,max_age=90):
    block=payload.get('bk_883958') or {}
    if block.get('date')!=dt.strftime('%Y%m%d'): raise ValueError('index date stale')
    prev=finite(block['pre'])
    if prev<=0: raise ValueError('invalid index previous close')
    samples=[row.split(',') for row in block.get('data','').split(';') if row]
    if not samples: raise ValueError('index intraday data absent')
    minute,price=samples[-1][:2]
    stamp=datetime.strptime(block['date']+minute,'%Y%m%d%H%M').replace(tzinfo=BJ)
    age=(dt-stamp).total_seconds()
    if not -5<=age<=max_age: raise ValueError('index timestamp stale')
    price=finite(price)
    if price<=0: raise ValueError('invalid index level')
    return {'pct':(price/prev-1)*100,'time':stamp.isoformat(),'code':'883958'}

def fetch_index(dt):
    r=requests.get('https://d.10jqka.com.cn/v6/time/bk_883958/last.js',headers=HEADERS,timeout=8)
    r.raise_for_status()
    return parse_index(jsonp(r.text),datetime.now(BJ))

def get_sina(method,params):
    for attempt in range(3):
        try:
            r=requests.get(SINA+method,params=params,headers=HEADERS,timeout=12)
            r.raise_for_status();return r.json()
        except (requests.RequestException,ValueError):
            if attempt==2: raise
            time.sleep(attempt+1)

def validate_universe(pages,total):
    rows=[row for page in pages for row in page]
    if len(rows)!=total: raise ValueError('market universe incomplete')
    symbols=[row.get('symbol') for row in rows]
    if len(set(symbols))!=total: raise ValueError('market universe duplicated')
    return [row for row in rows if re.fullmatch(r'(sh|sz)\d{6}',str(row.get('symbol','')))]

def parse_open_quotes(text,expected,day):
    result={}
    for match in re.finditer(r'v_((?:sh|sz)\d{6})="([^"]*)"',text):
        symbol,raw=match.groups();fields=raw.split('~')
        if len(fields)<49 or fields[2]!=symbol[2:]: raise ValueError('quote schema mismatch')
        stamp=datetime.strptime(fields[30],'%Y%m%d%H%M%S').replace(tzinfo=BJ)
        opening=finite(fields[5]);lower=finite(fields[48])
        if opening>0 and (stamp.date().isoformat()!=day or stamp.strftime('%H:%M')<'09:25'):
            raise ValueError('opening quote date/time stale')
        # zero open means suspended/no auction print; no opening limit-down counted.
        if opening<0 or lower<0: raise ValueError('invalid opening prices')
        if opening>0 and lower<=0: raise ValueError('missing lower-limit price')
        result[symbol]=bool(opening>0 and abs(opening-lower)<0.005)
    if set(result)!=set(expected): raise ValueError('opening quote batch incomplete')
    return result

def fetch_open_count(dt):
    if dt.strftime('%H:%M')<'09:30': raise ValueError('opening prices not final yet')
    total=int(get_sina('getHQNodeStockCount',{'node':'hs_a'}))
    if not 4000<=total<=15000: raise ValueError('implausible market universe size')
    count=(total+99)//100
    def page(number):
        result=get_sina('getHQNodeData',{'node':'hs_a','page':number,'num':100,'sort':'symbol','asc':1})
        if not isinstance(result,list): raise ValueError('invalid market universe page')
        return result
    with ThreadPoolExecutor(max_workers=3) as pool:
        universe=validate_universe(list(pool.map(page,range(1,count+1))),total)
    # Enumerate all mainland Shanghai/Shenzhen stocks including ST. Exclude Beijing.
    # Use exchange lower-limit prices from Tencent rather than infer 5/10/20 percent.
    symbols=[row['symbol'] for row in universe]
    batches=[symbols[start:start+100] for start in range(0,len(symbols),100)]
    def quote_batch(batch):
        r=requests.get('https://qt.gtimg.cn/q='+','.join(batch),headers=HEADERS,timeout=8)
        r.raise_for_status()
        return parse_open_quotes(r.content.decode('gbk','strict'),batch,dt.date().isoformat())
    hits={}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for batch_result in pool.map(quote_batch,batches): hits.update(batch_result)
    return {'count':sum(hits.values()),'symbols':sorted(key for key,value in hits.items() if value),
            'date':dt.date().isoformat(),'universe_count':len(symbols),
            'scope':'沪深A股（含ST，不含北交所）；最终开盘价等于当日跌停价',
            'checked_at':datetime.now(BJ).isoformat()}

def read_cache():
    try:return json.loads(CACHE.read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}

def refresh(dt,cfg):
    old=read_cache();day=dt.date().isoformat()
    cache=old if old.get('date')==day else {'date':day}
    try:
        cache['index']=fetch_index(dt);cache.pop('index_error',None)
    except Exception as exc:
        cache.pop('index',None);cache['index_error']=f'{type(exc).__name__}: {exc}'
    cache['refreshed_at']=datetime.now(BJ).isoformat()
    CACHE.parent.mkdir(parents=True,exist_ok=True)
    tmp=CACHE.with_suffix('.tmp');tmp.write_text(json.dumps(cache,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(CACHE)

def evaluate(cache,dt,cfg):
    reasons=[];pct=None;index_time=None
    if dt.strftime('%H:%M')<'09:30': reasons.append('竞价阶段禁止模拟开仓')
    if cache.get('date')==dt.date().isoformat():
        index=cache.get('index') or {}
        try:
            age=(dt-datetime.fromisoformat(index['time'])).total_seconds()
            if not -5<=age<=cfg.get('market_index_max_age_seconds',90): raise ValueError()
            pct=finite(index['pct']);index_time=index['time']
        except (KeyError,ValueError,TypeError): pass
    if pct is None: reasons.append('883958涨幅不可用/过期')
    elif pct<=0: reasons.append('883958当日涨幅不大于0%')
    return {'allowed':not reasons,'index_pct':pct,'index_time':index_time,'checked_at':dt.isoformat(),'opening_limit_down':None,'opening_limit_down_required':False,
            'reason':'；'.join(reasons) if reasons else '连板指数开仓门槛满足',
            'date':dt.date().isoformat()}

def get_gate(dt,cfg,start_worker=True):
    global _WORKER
    cache=read_cache()
    age=999999
    try:age=(dt-datetime.fromisoformat(cache['refreshed_at'])).total_seconds()
    except (KeyError,TypeError,ValueError):pass
    interval=30
    if start_worker and (cache.get('date')!=dt.date().isoformat() or age>=interval):
        with _LOCK:
            if _WORKER is None or not _WORKER.is_alive():
                _WORKER=threading.Thread(target=refresh,args=(dt,cfg),daemon=True)
                _WORKER.start()
    return evaluate(cache,dt,cfg)

def description(gate):
    pct='不可用' if gate['index_pct'] is None else f"{gate['index_pct']:+.2f}%"
    status='允许模拟开仓' if gate['allowed'] else '禁止模拟新开仓'
    return f"模拟仓门槛：{status}；883958涨幅 {pct}。{gate['reason']}。原选股信号仍有效，不代表成交。"
