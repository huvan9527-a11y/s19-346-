#!/usr/bin/env python3
"""Read-only morning recap from existing Actions logs; never infer fills."""
import argparse
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

BJ=ZoneInfo('Asia/Shanghai')
SCAN=re.compile(r'(?<!SIGNAL_)\b(?:SIGNAL_)?SCAN_OK (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) pool=(\d+) candidates=(\d+)')
PUSH=re.compile(r'PUSH_OK (\S+)')
STAMP=re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z')

def parse_logs(text,day):
    rows=[]
    for line in text.splitlines():
        stamp=STAMP.match(line.lstrip('\ufeff'))
        local=datetime.fromisoformat(stamp.group(1)+'+00:00').astimezone(BJ) if stamp else None
        if local is None or local.date().isoformat()!=day or local.hour>=12:
            continue
        scan=SCAN.search(line)
        if scan and scan.group(1)==day:
            rows.append({'time':scan.group(2),'kind':'scan','pool':int(scan.group(3)),
                         'candidates':int(scan.group(4))})
        push=PUSH.search(line)
        if push: rows.append({'time':local.strftime('%H:%M:%S'),'kind':'push','key':push.group(1)})
        # Ignore source code / command echoes; keep emitted error messages only.
        body=line[stamp.end():].strip()
        if body.startswith(('DATA_ERROR ','STATUS_PUSH_ERROR ','PUSH_ERROR ','POOL_EMPTY_RETRY ')):
            rows.append({'time':local.strftime('%H:%M:%S'),'kind':'error','detail':body})
    return rows

def api(session,path,params=None):
    response=session.get('https://api.github.com/'+path,params=params,timeout=30)
    response.raise_for_status()
    return response.json()

def collect(session,repo,day):
    sources=[];events=[];errors=[]
    # Include both morning scanners; never count manual artificial push tests.
    for workflow in ('s19-signal-push.yml','s19-paper.yml'):
        for page in range(1,101):
            block=api(session,f'repos/{repo}/actions/workflows/{workflow}/runs',
                      {'per_page':100,'page':page,'created':day})
            runs=block.get('workflow_runs',[])
            for run in runs:
                if datetime.fromisoformat(run['created_at'].replace('Z','+00:00')).astimezone(BJ).date().isoformat()!=day:
                    continue
                source={'run':run['id'],'workflow':workflow,'url':run['html_url'],
                        'conclusion':run.get('conclusion'),'attempt':run.get('run_attempt',1)}
                sources.append(source)
                try:
                    for jobs_page in range(1,101):
                        jobs=api(session,f'repos/{repo}/actions/runs/{run["id"]}/jobs',
                                 {'per_page':100,'page':jobs_page}).get('jobs',[])
                        for job in jobs:
                            response=session.get(f'https://api.github.com/repos/{repo}/actions/jobs/{job["id"]}/logs',timeout=45)
                            response.raise_for_status()
                            parsed=parse_logs(response.content.decode('utf-8-sig','replace'),day)
                            events.extend({**row,'run':run['id'],'job':job['id'],'workflow':workflow} for row in parsed)
                        if len(jobs)<100: break
                    else: raise RuntimeError('job pagination limit exceeded')
                except (requests.RequestException,ValueError,RuntimeError) as exc:
                    errors.append(f'run {run["id"]}: 日志不可用（{type(exc).__name__}）')
            if len(runs)<100: break
        else: errors.append(f'{workflow}: 运行列表分页超出上限')
    events.sort(key=lambda row:(row['time'],row['run'],row['job']))
    return sources,events,errors

def render(day,sources,events,errors):
    lines=[f'# {day} 上午回顾','',
           '> 基于实际 Actions 日志。不是完整盘中回放，不计算假设买卖或收益。','',
           '## 运行覆盖','']
    if not sources: lines.append('- 未找到当天相关任务；不能据此认定没有信号。')
    for src in sources:
        scans=[e for e in events if e['run']==src['run'] and e['kind']=='scan']
        coverage=f"{scans[0]['time']}–{scans[-1]['time']}，{len(scans)}轮" if scans else '未提取到扫描记录'
        lines.append(f'- [{src["workflow"]} / {src["run"]}]({src["url"]})：{coverage}；任务结果 {src["conclusion"] or "运行中"}；第 {src["attempt"]} 次尝试。')
    lines+=['','## 已记录推送','']
    pushes=[e for e in events if e['kind']=='push']
    if not pushes: lines.append('- 日志中未找到成功推送记录；不等于全天没有信号。')
    for e in pushes: lines.append(f'- {e["time"]}：`{e["key"]}`（任务 {e["run"]}；服务接受发送不等于用户已收到）')
    lines+=['','## 空池与错误','']
    issues=[e for e in events if e['kind']=='error' or (e['kind']=='scan' and e['pool']==0)]
    if not issues and not errors: lines.append('- 已读取的日志中未发现空池或明确错误。')
    for e in issues:
        detail=e.get('detail','扫描返回空池')
        if e['kind']=='scan':
            same=[x for x in events if x['run']==e['run'] and x['job']==e['job'] and x['kind']=='scan']
            index=same.index(e)
            if index>0 and index+1<len(same) and same[index-1]['pool']>0 and same[index+1]['pool']>0:
                detail+='；前后均为非空池，疑似短暂数据异常'
        lines.append(f'- {e["time"]}：{detail}（任务 {e["run"]}）')
    lines.extend(f'- {error}' for error in errors)
    lines+=['','## 回顾边界','',
            '- 未运行时段没有记录，不能补推或倒推当时候选。',
            '- 旧日志只有候选数量，不能据此确定每轮股票明细或模拟成交。',
            '- 新版逐轮股票池和候选见信号任务的 signal-review 下载文件；本报告不把收盘题材当作上午已知信息。',
            '- 多次运行分别列出，扫描次数和推送数不代表唯一股票数。']
    return '\n'.join(lines)+'\n'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--date',default='')
    args=parser.parse_args()
    day=args.date.strip() or datetime.now(BJ).date().isoformat()
    date.fromisoformat(day)
    repo=os.environ['GITHUB_REPOSITORY']
    session=requests.Session()
    session.headers.update({'Authorization':'Bearer '+os.environ['GH_TOKEN'],
                            'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'})
    sources,events,errors=collect(session,repo,day)
    out=Path('outputs/morning_review');out.mkdir(parents=True,exist_ok=True)
    report=render(day,sources,events,errors)
    (out/f'{day}.md').write_text(report,encoding='utf-8')
    (out/f'{day}.json').write_text(json.dumps({'date':day,'sources':sources,'events':events,'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as f: f.write(report)
    print(report)
    return 1 if errors else 0

if __name__=='__main__': raise SystemExit(main())
