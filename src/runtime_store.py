#!/usr/bin/env python3
"""Checkpoint account+outbox+evidence on a dedicated Git branch without blocking scans.

Run restore once before the session. watch publishes frozen prefixes every minute.
No force-push; remote failures fail the final persistence step, never reset cash.
"""
import argparse
import gzip
import hashlib
import json
import os
import shutil
import signal
import threading
import subprocess
import tempfile
import time
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parents[1]
BRANCH = 's19-runtime'
WORK = Path(os.environ.get('RUNNER_TEMP', tempfile.gettempdir())) / ('s19-store-' + os.environ.get('GITHUB_RUN_ID', 'local'))


def git(*args, cwd=None, check=True):
    result = subprocess.run(['git', *args], cwd=cwd or ROOT, text=True, capture_output=True, timeout=60)
    if check and result.returncode:
        # git authentication uses checkout's credential helper, never print secrets/headers.
        raise RuntimeError('git %s failed: %s' % (args[0], result.stderr[-600:]))
    return result


def remote():
    result = git('ls-remote', '--exit-code', '--heads', 'origin', 'refs/heads/' + BRANCH, check=False)
    if result.returncode == 2:
        return None
    if result.returncode:
        raise RuntimeError('runtime branch lookup failed; refusing to bootstrap from stale account')
    return result.stdout.split()[0]


def setup():
    if (WORK / '.git').exists():
        return
    sha = remote()
    if sha:
        git('fetch', '--depth=1', 'origin', BRANCH)
        git('worktree', 'add', '--detach', str(WORK), sha)
    else:
        git('worktree', 'add', '--detach', str(WORK), 'HEAD')
        git('checkout', '--orphan', 'runtime-bootstrap', cwd=WORK)
        # Orphan branch has no inherited strategy files in its runtime history.
        git('rm', '-rf', '.', cwd=WORK)
    git('config', 'user.name', 's19-runtime-bot', cwd=WORK)
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com', cwd=WORK)


def restore():
    sha = remote()
    if not sha:
        if not (ROOT / 'state/account.json').exists():
            raise RuntimeError('no seed account; refusing cash initialization')
        print('RUNTIME_BOOTSTRAP tracked account retained', flush=True)
        return
    git('fetch', '--depth=1', 'origin', BRANCH)
    result = git('show', 'FETCH_HEAD:state/account.json')
    state = json.loads(result.stdout)
    if 'cash' not in state or not isinstance(state.get('positions'), dict):
        raise RuntimeError('remote account invalid')
    target = ROOT / 'state/account.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.restore.tmp')
    temp.write_text(result.stdout, encoding='utf-8')
    temp.replace(target)
    # Runtime branch also keeps full fill/valuation exports across sessions.
    for name in ('logs/trades.csv', 'logs/equity.csv'):
        data = git('show', 'FETCH_HEAD:' + name, check=False)
        if data.returncode == 0:
            dest = ROOT / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(data.stdout, encoding='utf-8')
    print('RUNTIME_RESTORED saved_at=%s cash=%s positions=%s' %
          (state.get('saved_at'), state['cash'], list(state['positions'])), flush=True)


def save():
    fp = ROOT / 'state/checkpoint.json'
    if not fp.exists():
        print('RUNTIME_WAIT no frozen checkpoint yet', flush=True)
        return False
    checkpoint = json.loads(fp.read_text(encoding='utf-8'))
    setup()
    # Freeze every journal to the exact prefix that belongs to this account state.
    for entry in checkpoint['journal']:
        rel = Path(entry['path'])
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('invalid checkpoint journal path')
        with (ROOT / rel).open('rb') as stream:
            data = stream.read(entry['size'])
        if hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise RuntimeError('evidence prefix changed: ' + str(rel))
        dest = WORK / (str(rel) + '.gz')
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(gzip.compress(data, mtime=0))
    for name, data in [('state/account.json', json.dumps(checkpoint['account'], ensure_ascii=False, indent=2)),
                       ('state/checkpoint.json', json.dumps(checkpoint, ensure_ascii=False, indent=2)),
                       ('outputs/latest.md', checkpoint['report'])]:
        dest = WORK / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(data, encoding='utf-8')
    for entry in checkpoint.get('exports', []):
        rel = Path(entry['path'])
        if str(rel) not in ('logs/trades.csv', 'logs/equity.csv'):
            raise ValueError('invalid checkpoint export path')
        with (ROOT / rel).open('rb') as stream:
            data = stream.read(entry['size'])
        if hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise RuntimeError('export prefix changed: ' + str(rel))
        dest = WORK / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    # Keep a bounded live audit window; Actions artifacts retain each full session 90 days.
    cutoff = (date.fromisoformat(checkpoint['saved_at'][:10]) - timedelta(days=7)).isoformat()
    history = WORK / 'logs/sessions'
    if history.exists():
        for folder in history.iterdir():
            if folder.is_dir() and len(folder.name) == 10 and folder.name < cutoff:
                shutil.rmtree(folder)
    git('add', '.', cwd=WORK)
    if git('diff', '--cached', '--quiet', cwd=WORK, check=False).returncode == 0:
        return True
    git('commit', '-m', 'S19 checkpoint ' + checkpoint['saved_at'], cwd=WORK)
    git('push', 'origin', 'HEAD:refs/heads/' + BRANCH, cwd=WORK)
    print('RUNTIME_PUBLISHED ' + checkpoint['saved_at'], flush=True)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['restore', 'save', 'watch'])
    args = parser.parse_args()
    if args.command == 'restore':
        restore()
    elif args.command == 'save':
        if not save():
            raise RuntimeError('session produced no checkpoint')
    else:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            try:
                save()
            except Exception as exc:
                print('RUNTIME_CHECKPOINT_ERROR ' + str(exc), flush=True)
            stop.wait(60)


if __name__ == '__main__':
    main()
