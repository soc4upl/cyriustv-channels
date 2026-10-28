#!/usr/bin/env python3
"""Fresh Ubuntu installation. No disk formatting and no overwrite of existing CYRIUS."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CHANNELS = [
    (1, 'Cyrius Боевики', 'Boevik', 'cyrius-boeviki', 'boeviki'),
    (2, 'Cyrius Comedy', 'Comedy', 'cyrius-comedy', 'comedy'),
    (3, 'Cyrius Kids HD', 'KidsHD', 'cyrius-kids-hd', 'kids-hd'),
    (4, 'Cyrius Horror', 'Horror', 'cyrius-horror', 'horror'),
    (5, 'Cyrius Cinema HD', 'CinemaHD', 'cyrius-cinema-hd', 'cinema-hd'),
    (6, 'Cyrius Ретро HD', 'Retro', 'cyrius-retro-hd', 'retro-hd'),
    (7, 'Cyrius Романтика HD', 'Romance', 'cyrius-romance-hd', 'romance-hd'),
]


def validate(raw):
    expected = {'panel_domain', 'origin_base_url', 'timezone', 'secondary_storage', 'origin_allow', 'admin_username'}
    if set(raw) != expected:
        raise ValueError('Нужны ровно поля из settings.example.json')
    domain = raw['panel_domain']
    if not isinstance(domain, str) or len(domain) > 253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain):
        raise ValueError('panel_domain: нужен домен без https://, порта и пути')
    origin = urlsplit(raw['origin_base_url'])
    try:
        safe_host = bool(origin.hostname) and bool(re.fullmatch(r'[a-zA-Z0-9.-]+', origin.hostname))
        port = origin.port
    except ValueError:
        raise ValueError('Некорректный origin_base_url')
    if origin.scheme not in ('http', 'https') or not safe_host or origin.username or origin.password or origin.path not in ('', '/') or origin.query or origin.fragment or (port is not None and not 1 <= port <= 65535):
        raise ValueError('origin_base_url: нужен http(s)://имя-или-IPv4[:порт] без пути и пароля')
    if type(raw['secondary_storage']) is not bool:
        raise ValueError('secondary_storage должен быть true или false')
    ZoneInfo(raw['timezone'])
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', raw['admin_username']):
        raise ValueError('Некорректный admin_username')
    if not isinstance(raw['origin_allow'], list) or not raw['origin_allow']:
        raise ValueError('origin_allow: укажите IP/CIDR вашего Flussonic; all открывает источник всем')
    for item in raw['origin_allow']:
        if item != 'all':
            ipaddress.ip_network(item, strict=False)
    return {**raw, 'origin_base_url': raw['origin_base_url'].rstrip('/')}


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def render(cfg):
    """Return reviewed text templates; no filesystem, network or system changes."""
    domain = cfg['panel_domain']
    site = {'panel_url': 'https://' + domain, 'origin_base_url': cfg['origin_base_url']}
    channels = []
    for cid, name, genre, slug, logo in CHANNELS:
        c = {'id': cid, 'name': name, 'slug': slug, 'media': '/srv/cyrius/media/' + genre,
             'public': f'/srv/cyrius/public-copy/{cid}/live', 'segment_type': 'mpegts',
             'allowed_video_codecs': ['h264', 'hevc', 'mpeg2video', 'mpeg4'],
             'target_duration': 15, 'window_seconds': 90, 'retain_seconds': 600,
             'logo': {'path': '/srv/cyrius/assets/logos/' + logo + '.pam',
                      'crop': [0, 0, 1, 1], 'url': 'https://' + domain + '/icons/' + str(cid) + '.png',
                      'width': 250, 'x': 48, 'y': 32, 'corner': 'top_right'},
             'video_standard': {'width': 1920, 'height': 1080, 'mode': 'crop', 'scale_flags': 'bicubic'},
             'title_overlay': {'font': '/srv/cyrius/assets/fonts/Montserrat-Light.ttf', 'weight': 300,
                               'color': '#ffffff', 'size': 36, 'x': 48, 'y': 32, 'max_width': 1300}}
        if cid == 5:
            c.update(media_roots=['/srv/cyrius/media/' + g for i, _, g, _, _ in CHANNELS if i != 5],
                     deduplicate_titles=True, exclude_series=True, library_mode='thematic_mix')
        channels.append(c)
    runtime = (f'CYRIUS_ORIGIN_URL={cfg["origin_base_url"]}\nCYRIUS_TIMEZONE={cfg["timezone"]}\n'
               f'PANEL_ALLOWED_HOSTS={domain},127.0.0.1,localhost\n'
               'CYRIUS_FFMPEG=/usr/bin/ffmpeg\nCYRIUS_FFPROBE=/usr/bin/ffprobe\n')
    common = ('NoNewPrivileges=yes\nPrivateTmp=yes\nPrivateDevices=yes\nProtectSystem=strict\n'
              'ProtectHome=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n')
    worker = ('[Unit]\nDescription=CYRIUS channel %i\nAfter=network.target\nRequiresMountsFor=/srv/cyrius/media\n'
              '[Service]\nType=simple\nUser=ffpu\nSupplementaryGroups=cyrius-media\n'
              'EnvironmentFile=/etc/cyrius/runtime.env\n'
              'ExecStart=/usr/bin/python3 /usr/local/libexec/cyrius-copy-playout.py %i\n'
              'Restart=on-failure\nRestartSec=5\nKillMode=mixed\nTimeoutStopSec=15\n'
              'StateDirectory=cyrius-copy/%i\nStateDirectoryMode=0700\nUMask=0022\n'
              'Nice=5\nIOSchedulingClass=best-effort\nIOSchedulingPriority=2\nMemoryMax=2G\nCPUQuota=150%\n'
              + common + 'CapabilityBoundingSet=\nRestrictAddressFamilies=AF_UNIX\n'
              'ReadWritePaths=/srv/cyrius/public-copy/%i /var/lib/cyrius-copy/%i /var/lib/cyrius-control/%i\n'
              '[Install]\nWantedBy=multi-user.target\n')
    panel = ('[Unit]\nDescription=CYRIUS management panel\nAfter=network.target\nRequiresMountsFor=/srv/cyrius/media\n'
             '[Service]\nUser=ffpu\nSupplementaryGroups=cyrius-media\nWorkingDirectory=/opt/cyrius-panel\n'
             'EnvironmentFile=/etc/cyrius/runtime.env\nEnvironment=PYTHONPATH=/usr/local/libexec\nEnvironment=PANEL_ACCEL=1\n'
             'ExecStart=/usr/bin/gunicorn --bind 127.0.0.1:8890 --workers 1 --threads 4 --timeout 60 --worker-tmp-dir /run/cyrius-panel app:app\n'
             'Restart=on-failure\nRestartSec=3\nStateDirectory=cyrius-panel\nStateDirectoryMode=0700\n'
             'RuntimeDirectory=cyrius-panel\nRuntimeDirectoryMode=0700\nUMask=0027\nNice=10\nCPUQuota=25%\nMemoryMax=384M\n'
             'PrivateTmp=yes\nPrivateDevices=yes\nProtectSystem=strict\nProtectHome=yes\n'
             'ProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n'
             'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n'
             'ReadWritePaths=/var/lib/cyrius-panel /var/lib/cyrius-control /etc/cyrius /var/backups/cyrius-panel-config /srv/cyrius/media\n'
             '[Install]\nWantedBy=multi-user.target\n')
    if cfg['secondary_storage']:
        worker = worker.replace('RequiresMountsFor=/srv/cyrius/media', 'RequiresMountsFor=/srv/cyrius/media /srv/cyrius/disk2')
        panel = panel.replace('RequiresMountsFor=/srv/cyrius/media', 'RequiresMountsFor=/srv/cyrius/media /srv/cyrius/disk2').replace('/var/backups/cyrius-panel-config /srv/cyrius/media', '/var/backups/cyrius-panel-config /srv/cyrius/media /srv/cyrius/disk2/media')
    programme = ('[Unit]\nDescription=CYRIUS EPG\n[Service]\nType=oneshot\nUser=ffpu\nSupplementaryGroups=cyrius-media\n'
                 'EnvironmentFile=/etc/cyrius/runtime.env\nExecStart=/usr/bin/python3 /usr/local/libexec/cyrius-programme.py\n'
                 'UMask=0022\nNice=10\nMemoryMax=128M\nCPUQuota=25%\n' + common +
                 'RestrictAddressFamilies=AF_UNIX\nReadWritePaths=/srv/cyrius/public-copy\n')
    timer = '[Unit]\nDescription=CYRIUS EPG refresh\n[Timer]\nOnBootSec=45\nOnUnitActiveSec=30\nUnit=cyrius-programme.service\n[Install]\nWantedBy=timers.target\n'
    access = ''.join(f'    allow {address};\n' for address in cfg['origin_allow'])
    if 'all' not in cfg['origin_allow']:
        access += '    deny all;\n'
    routes = ''.join(f'    location /{slug}/ {{ alias /srv/cyrius/public-copy/{cid}/live/; }}\n' for cid, _, _, slug, _ in CHANNELS)
    origin = ('server {\n    listen 18080;\n    listen [::]:18080;\n    server_name _;\n'
              '    types { application/vnd.apple.mpegurl m3u8; video/mp2t ts; application/xml xml; application/json json; }\n'
              '    default_type application/octet-stream;\n    add_header Cache-Control "no-cache" always;\n' + access + routes +
              ''.join(f'    location = /{n} {{ alias /srv/cyrius/public-copy/{n}; }}\n' for n in ['epg.xml', 'programme.json', 'channels.m3u']) +
              '    location / { return 404; }\n}\n')
    http = ('server {\n    listen 80;\n    listen [::]:80;\n    server_name ' + domain + ';\n'
            '    location /.well-known/acme-challenge/ { root /var/www/cyrius-acme; }\n'
            '    location / { return 503; }\n}\n')
    tls = (ROOT / 'deploy/panel-tls.conf.template').read_text().replace('@PANEL_DOMAIN@', domain)
    files = {'etc/cyrius/copy-channels.json': encode({'enabled': True, 'channels': channels}),
             'etc/cyrius/site.json': encode(site), 'etc/cyrius/runtime.env': runtime,
             'etc/cyrius/movie-titles-ru.json': encode({'titles': {}}),
             'etc/cyrius/title-rules-ru.json': encode({}),
             'etc/cyrius/media-exclusions.json': encode({'filenames': []}),
             'etc/cyrius/torrent-disabled.json': encode({'disabled': True, 'reason': 'Not installed in portable release'}),
             'etc/cyrius/auto-download.json': encode({'enabled': False, 'daily_limit': 10, 'max_active': 2, 'max_torrent_bytes': 12_000_000_000}),
             'etc/systemd/system/cyrius-copy@.service': worker,
             'etc/systemd/system/cyrius-panel.service': panel,
             'etc/systemd/system/cyrius-programme.service': programme,
             'etc/systemd/system/cyrius-programme.timer': timer,
             'etc/nginx/sites-available/cyrius-origin': origin,
             'etc/nginx/sites-available/cyrius-panel': http,
             'etc/cyrius/panel-tls.conf': tls,
             'etc/sudoers.d/cyrius-panel': 'ffpu ALL=(root) NOPASSWD: /usr/local/sbin/cyrius-panel-admin ""\n'}
    if cfg['secondary_storage']:
        for _, _, genre, _, _ in CHANNELS:
            files[f'etc/systemd/system/srv-cyrius-media-{genre}-Disk2.mount'] = (
                f'[Unit]\nDescription=CYRIUS {genre} second disk\nRequiresMountsFor=/srv/cyrius/disk2\n'
                f'[Mount]\nWhat=/srv/cyrius/disk2/media/{genre}\nWhere=/srv/cyrius/media/{genre}/Disk2\nType=none\nOptions=bind\n'
                '[Install]\nWantedBy=multi-user.target\n')
    return files


def check_fresh():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise RuntimeError('--apply запускается от root на Ubuntu 24.04')
    release = Path('/etc/os-release').read_text()
    if 'ID=ubuntu' not in release or 'VERSION_ID="24.04"' not in release:
        raise RuntimeError('Поддерживается Ubuntu 24.04; другие ОС требуют отдельной проверки')
    for name in ['/etc/cyrius', '/opt/cyrius-panel', '/srv/cyrius/assets', '/srv/cyrius/media', '/srv/cyrius/public-copy', '/var/lib/cyrius-panel', '/var/lib/cyrius-control', '/etc/nginx/sites-available/cyrius-origin', '/etc/nginx/sites-available/cyrius-panel', '/etc/sudoers.d/cyrius-panel', '/usr/local/sbin/cyrius-panel-admin', '/usr/local/sbin/cyrius-admin']:
        if Path(name).exists():
            raise RuntimeError('Установка остановлена: уже существует ' + name + '. См. docs/MIGRATION.md')
    if any(Path('/usr/local/libexec').glob('cyrius*')):
        raise RuntimeError('Найдены существующие исходники CYRIUS в /usr/local/libexec')
    for name in ['cyrius-panel.service', 'cyrius-copy@.service', 'cyrius-programme.service']:
        if Path('/etc/systemd/system', name).exists():
            raise RuntimeError('Уже существует служба ' + name)
    for port in [80, 443, 8890, 18080]:
        with socket.socket() as sock:
            try: sock.bind(('0.0.0.0', port))
            except OSError: raise RuntimeError(f'Порт {port} занят. Нужна ручная интеграция с существующими службами')


def run(*args):
    subprocess.run(list(args), check=True)


def install(cfg):
    check_fresh()
    if cfg['secondary_storage'] and (not Path('/srv/cyrius/disk2').is_mount() or Path('/srv/cyrius/disk2').stat().st_dev == Path('/').stat().st_dev):
        raise RuntimeError('Сначала смонтируйте отдельный диск в /srv/cyrius/disk2; установка диски не размечает')
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'python3-flask', 'python3-pil', 'python3-werkzeug', 'gunicorn', 'ffmpeg', 'nginx', 'certbot', 'sudo')
    import pwd, grp
    try: grp.getgrnam('cyrius-media')
    except KeyError: run('groupadd', '--system', 'cyrius-media')
    try: pwd.getpwnam('ffpu')
    except KeyError: run('useradd', '--system', '--home-dir', '/var/lib/cyrius-panel', '--shell', '/usr/sbin/nologin', 'ffpu')
    run('usermod', '-a', '-G', 'cyrius-media', 'ffpu')
    user = pwd.getpwnam('ffpu'); media_gid = grp.getgrnam('cyrius-media').gr_gid
    for value in render(cfg).items():
        rel, content = value; p = Path('/') / rel; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content); p.chmod(0o440 if '/sudoers.d/' in rel else 0o600 if rel.endswith('auto-download.json') else 0o644)
    os.chmod('/etc/cyrius', 0o755)
    def owned(path, mode=0o700):
        p = Path(path);p.mkdir(parents=True, exist_ok=True);os.chown(p, user.pw_uid, user.pw_gid);p.chmod(mode)
    for p in ['/var/lib/cyrius-panel', '/var/lib/cyrius-control', '/var/lib/cyrius-copy']:
        owned(p)
    for cid, _, genre, _, _ in CHANNELS:
        owned(f'/var/lib/cyrius-control/{cid}')
        owned(f'/var/lib/cyrius-copy/{cid}')
        owned(f'/srv/cyrius/media/{genre}', 0o750)
        os.chown(f'/srv/cyrius/media/{genre}', user.pw_uid, media_gid)
        owned(f'/srv/cyrius/public-copy/{cid}', 0o755)
        owned(f'/srv/cyrius/public-copy/{cid}/live', 0o755)
        if cfg['secondary_storage']:
            owned(f'/srv/cyrius/disk2/media/{genre}', 0o750)
            owned(f'/srv/cyrius/media/{genre}/Disk2', 0o750)
    Path('/srv/cyrius/public-copy').chmod(0o755);os.chown('/srv/cyrius/public-copy', user.pw_uid, user.pw_gid)
    owned('/srv/cyrius/media/00-assets', 0o755)
    Path('/etc/cyrius/panel-logos').mkdir(mode=0o750);os.chown('/etc/cyrius/panel-logos', 0, media_gid)
    Path('/var/backups/cyrius-panel-config').mkdir(mode=0o700, exist_ok=True)
    Path('/var/www/cyrius-acme').mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'assets', '/srv/cyrius/assets')
    for p in Path('/srv/cyrius/assets').rglob('*'):p.chmod(0o755 if p.is_dir() else 0o644)
    from PIL import Image
    config_path = Path('/etc/cyrius/copy-channels.json'); config = json.loads(config_path.read_text())
    for c, (_, _, _, _, logo) in zip(config['channels'], CHANNELS):
        picture = Image.open('/srv/cyrius/assets/logos/' + logo + '.png').convert('RGBA')
        bbox = picture.getchannel('A').getbbox()
        if not bbox: raise RuntimeError('Пустой логотип: ' + logo)
        c['logo']['crop'] = [bbox[0], bbox[1], bbox[2]-bbox[0], bbox[3]-bbox[1]]
        header = f'P7\nWIDTH {picture.width}\nHEIGHT {picture.height}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n'.encode()
        Path(c['logo']['path']).write_bytes(header + picture.tobytes())
    config_path.write_text(encode(config))
    Path('/usr/local/libexec').mkdir(parents=True, exist_ok=True)
    for p in (ROOT / 'src/playout').glob('*.py'):
        shutil.copy2(p, Path('/usr/local/libexec') / p.name)
    shutil.copytree(ROOT / 'src/panel', '/opt/cyrius-panel')
    for p in Path('/opt/cyrius-panel').rglob('*'):p.chmod(0o755 if p.is_dir() else 0o644)
    for name in ['Montserrat-Light.ttf', 'OFL.txt']:
        shutil.copy2(ROOT / 'assets/fonts' / name, Path('/opt/cyrius-panel/static') / name)
    shutil.copy2(ROOT / 'src/panel/cyrius-panel-admin.py', '/usr/local/sbin/cyrius-panel-admin')
    os.chmod('/usr/local/sbin/cyrius-panel-admin', 0o755)
    shutil.copy2(ROOT / 'scripts/admin.py', '/usr/local/sbin/cyrius-admin');os.chmod('/usr/local/sbin/cyrius-admin', 0o755)
    run('visudo', '-cf', '/etc/sudoers.d/cyrius-panel')
    run('/usr/bin/python3', '/usr/local/sbin/cyrius-admin', 'init', '--username', cfg['admin_username'])
    for p in Path('/var/lib/cyrius-panel').iterdir():os.chown(p, user.pw_uid, user.pw_gid);p.chmod(0o600)
    # A generated, silent test card is the only video created by installation.
    run('/usr/bin/ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=0x111827:s=1920x1080:r=25',
        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '12', '-c:v', 'libx264', '-preset', 'ultrafast',
        '-threads', '1', '-g', '100', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
        '/srv/cyrius/media/00-assets/cyrius-standby.mp4')
    for site in ['cyrius-origin', 'cyrius-panel']:
        Path('/etc/nginx/sites-enabled', site).symlink_to('/etc/nginx/sites-available/' + site)
    run('nginx', '-t')
    run('systemctl', 'daemon-reload')
    if cfg['secondary_storage']:
        for _, _, genre, _, _ in CHANNELS:
            run('systemctl', 'enable', '--now', f'srv-cyrius-media-{genre}-Disk2.mount')
    run('systemctl', 'reload', 'nginx')
    run('systemctl', 'enable', '--now', 'cyrius-panel.service', 'cyrius-programme.timer')
    # Channels start only after an explicit command, so CPU load can be assessed one by one.
    print('Установлено. Каналы ещё не запущены. Пароль: /root/cyrius-panel-credentials.json')
    print('Далее: docs/INSTALL.md — HTTPS и запуск первого канала.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true', help='Validate and show plan, no changes')
    group.add_argument('--render-dir', type=Path, help='Write templates to a NEW local staging directory')
    group.add_argument('--apply', action='store_true', help='Install on a fresh Ubuntu 24.04 server')
    args = parser.parse_args();cfg = validate(json.loads(args.config.read_text()))
    files = render(cfg)
    if args.check:
        print(encode({'panel': 'https://' + cfg['panel_domain'], 'origin': cfg['origin_base_url'],
                      'channels': 7, 'secondary_storage': cfg['secondary_storage'], 'files': sorted(files),
                      'torrent_transport': 'not installed', 'disks_formatted': False}))
    elif args.render_dir:
        args.render_dir.mkdir(parents=True, exist_ok=False)
        for rel, data in files.items():
            p = args.render_dir / rel;p.parent.mkdir(parents=True, exist_ok=True);p.write_text(data)
        print('Шаблоны сохранены: ' + str(args.render_dir))
    else:
        install(cfg)


if __name__ == '__main__':
    try: main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit('Ошибка: ' + str(error))
