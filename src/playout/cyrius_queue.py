"""Persistent channel queues and acknowledged controls, shared by panel and playout."""
import json
import os
from pathlib import Path
import random
import time

ROOT = Path(os.environ.get('CYRIUS_CONTROL_ROOT', '/var/lib/cyrius-control'))


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def policy(cid):
    return read(ROOT / str(cid) / 'queue.json', {'mode': 'sequential', 'repeat': True, 'use_all': True, 'items': [], 'version': 0})


def arrange(files, cid):
    cfg = policy(cid)
    lookup = {str(p): p for p in files}
    chosen = [lookup[p] for p in cfg.get('items', []) if p in lookup]
    if cfg.get('use_all', True):
        chosen.extend(p for p in files if p not in chosen)
    if not chosen:
        return files  # A stale/deleted queue must not unexpectedly interrupt live TV.
    if cfg.get('mode') == 'random' and not cfg.get('frozen_order'):
        random.Random(str(cfg.get('seed', 0))).shuffle(chosen)
    return chosen


def command(cid):
    cmd = read(ROOT / str(cid) / 'command.json')
    ack = read(ROOT / str(cid) / 'ack.json')
    return cmd if cmd.get('id') and cmd['id'] != ack.get('id') else None


def acknowledge(cid, cmd, ok=True, message=''):
    atomic(ROOT / str(cid) / 'ack.json', {'id': cmd['id'], 'ok': ok, 'message': message, 'at': time.time()})
