#!/usr/bin/env python3
"""Local root-only account, TLS and health operations. No access through the web helper."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time

STATE = Path('/var/lib/cyrius-panel')
CONFIG = Path('/etc/cyrius')


def account(username, password, initialize=False):
    from werkzeug.security import generate_password_hash
    import pwd
    STATE.mkdir(mode=0o700, exist_ok=True)
    key = STATE / 'secret.key'
    if initialize and not key.exists():key.write_text(secrets.token_hex(32));key.chmod(0o600)
    with sqlite3.connect(STATE / 'panel.db', timeout=10) as db:
        db.execute('pragma journal_mode=WAL')
        db.execute('create table if not exists users(username text primary key,password_hash text not null,version integer not null default 1)')
        db.execute('create table if not exists audit(id integer primary key,at real,user text,ip text,action text,detail text)')
        existing = db.execute('select version from users where username=?', (username,)).fetchone()
        if initialize and db.execute('select 1 from users limit 1').fetchone():
            raise ValueError('Учётная запись уже создана; используйте password')
        if not initialize and not existing:raise ValueError('Пользователь не найден')
        if existing:db.execute('update users set password_hash=?,version=version+1 where username=?', (generate_password_hash(password), username))
        else:db.execute('insert into users(username,password_hash) values(?,?)', (username, generate_password_hash(password)))
        db.execute('insert into audit(at,user,ip,action,detail) values(?,?,?,?,?)', (time.time(), username, 'local-root', 'password_reset', 'Local administration'))
    user = pwd.getpwnam('ffpu')
    for p in STATE.iterdir():os.chown(p, user.pw_uid, user.pw_gid);p.chmod(0o600)
    os.chown(STATE, user.pw_uid, user.pw_gid)
    # Replacing a symlink is safe; never follow it when writing a new credential file.
    path = Path('/root/cyrius-panel-credentials.json')
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as out:json.dump({'username': username, 'password': password}, out)
    os.replace(temporary, path)
    print('Пароль сохранён в /root/cyrius-panel-credentials.json (только root).')


def tls(email):
    from urllib.parse import urlsplit
    import re
    site = json.loads((CONFIG / 'site.json').read_text());domain = urlsplit(site['panel_url']).hostname
    if not domain or not re.fullmatch(r'[a-z0-9.-]+', domain):raise ValueError('Некорректный домен')
    if not email or '@' not in email or email.startswith('-'):raise ValueError('Укажите email для сертификата')
    # Interactive Certbot asks the owner to accept the CA terms; no automatic acceptance.
    subprocess.run(['certbot', 'certonly', '--webroot', '-w', '/var/www/cyrius-acme', '-d', domain, '--email', email], check=True)
    target = Path('/etc/nginx/sites-available/cyrius-panel')
    before = target.read_bytes()
    try:
        target.write_bytes((CONFIG / 'panel-tls.conf').read_bytes())
        subprocess.run(['nginx', '-t'], check=True)
    except Exception:
        target.write_bytes(before)
        raise
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
    hook = Path('/etc/letsencrypt/renewal-hooks/deploy/cyrius-panel-nginx')
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\n/usr/sbin/nginx -t && /usr/bin/systemctl reload nginx\n');hook.chmod(0o755)
    subprocess.run(['systemctl', 'enable', '--now', 'certbot.timer'], check=True)
    print('HTTPS включён: ' + site['panel_url'])


def health():
    import urllib.request
    cfg = json.loads((CONFIG / 'copy-channels.json').read_text());failed = False
    for c in cfg['channels']:
        p = Path('/var/lib/cyrius-copy') / str(c['id']) / 'state.json'
        state = json.loads(p.read_text()) if p.exists() else {}
        manifest = Path(c['public']) / 'index.m3u8'
        ok = time.time()-state.get('updated_at', 0)<20 and manifest.exists() and time.time()-manifest.stat().st_mtime<35
        print(('OK ' if ok else 'STOPPED ') + c['name']);failed |= not ok
    with urllib.request.urlopen('http://127.0.0.1:8890/api/health', timeout=5) as response:
        assert response.status == 200
    print('OK panel; torrent transport disabled')
    return 1 if failed else 0


def main():
    if os.geteuid() != 0:raise ValueError('Нужен sudo')
    parser = argparse.ArgumentParser(description=__doc__);sub = parser.add_subparsers(dest='action', required=True)
    for action in ['init', 'password']:
        p = sub.add_parser(action);p.add_argument('--username', default='admin')
    sub.add_parser('health');p = sub.add_parser('tls');p.add_argument('--email', required=True)
    args = parser.parse_args()
    if args.action == 'health':return health()
    if args.action == 'tls':tls(args.email);return 0
    password = secrets.token_urlsafe(18) if args.action == 'init' else getpass.getpass('Новый пароль (минимум 12 символов): ')
    if not 12 <= len(password) <= 200:raise ValueError('Пароль должен содержать 12–200 символов')
    if args.action == 'password' and getpass.getpass('Повторите пароль: ') != password:raise ValueError('Пароли не совпали')
    account(args.username, password, initialize=args.action == 'init')
    return 0


if __name__ == '__main__':
    try:sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:sys.exit('Ошибка: ' + str(error))
