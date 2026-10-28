#!/usr/bin/python3
"""Small privileged configuration writer. No shell, arbitrary paths or services."""
import base64
import fcntl
import io
import json
import os
from pathlib import Path
import re
import sys
import time

DOWNLOADS_DISABLED = True
BASE = Path('/etc/cyrius')
ASSETS = Path('/etc/cyrius/panel-logos')
BACKUPS = Path('/var/backups/cyrius-panel-config')


def save(path, value):
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    stamp = str(time.time_ns())
    if path.exists():
        backup = BACKUPS / (stamp + '-' + path.name)
        backup.write_bytes(path.read_bytes()); backup.chmod(0o600)
    tmp = path.with_name(path.name + '.panel.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.chmod(0o644 if path.name != 'auto-download.json' else 0o600)
    tmp.replace(path)


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Значение вне допустимого диапазона')
    return value


def main(data):
    kind = data.get('action')
    if kind == 'auto-read':
        cfg = json.loads((BASE / 'auto-download.json').read_text())
        result = {k: cfg.get(k) for k in ['enabled', 'daily_limit', 'max_active', 'max_torrent_bytes']}
        result['blocked'] = DOWNLOADS_DISABLED
        if result['blocked']: result['enabled'] = False
        for name in ['status', 'campaign-manifest']:
            p = Path('/var/lib/cyrius-auto-download') / (name + '.json')
            if p.exists():
                obj = json.loads(p.read_text())
                result[name] = {k: v for k, v in obj.items() if k not in ['films', 'candidates', 'added']}
        rejected = Path('/var/lib/cyrius-media-import/auto-rejected.json')
        if rejected.exists():
            records = json.loads(rejected.read_text())
            result['rejections'] = [{k: row.get(k) for k in ['filename', 'reason', 'time']} for row in records.values()][-30:]
        return result
    if kind == 'auto':
        if DOWNLOADS_DISABLED:
            raise ValueError('Торрент-загрузки в переносимой версии отключены')
        p = BASE / 'auto-download.json'; cfg = json.loads(p.read_text())
        values = data['values']
        if type(values.get('enabled')) is not bool: raise ValueError('Некорректное состояние')
        cfg['enabled'] = values['enabled']
        cfg['daily_limit'] = integer(values['daily_limit'], 1, 50)
        cfg['max_active'] = integer(values['max_active'], 1, 2)
        size = float(values['max_gb'])
        if not 0.5 <= size <= 12: raise ValueError('Максимум — 12 ГБ')
        cfg['max_torrent_bytes'] = int(size * 1e9)
        cfg['max_torrent_gib'] = cfg['max_torrent_bytes'] / 1024**3
        save(p, cfg)
        return {'saved': True}
    cfg_path = BASE / 'copy-channels.json'; cfg = json.loads(cfg_path.read_text())
    cid = integer(data.get('channel'), 1, 7)
    ch = next(c for c in cfg['channels'] if c['id'] == cid)
    if kind == 'title':
        name = data.get('filename', '')
        if not name or len(name) > 255 or name != Path(name).name or '/' in name or '\\' in name:
            raise ValueError('Некорректное имя файла')
        # The filename must already be present in this channel's media tree.
        roots = [Path(p) for p in ch.get('media_roots', [ch['media']])]
        if not any(p.is_file() for root in roots for p in root.rglob('*') if p.name == name):
            raise ValueError('Файл не найден в медиатеке')
        title = str(data.get('title', '')).strip()
        if not 1 <= len(title) <= 220 or any(ord(c) < 32 for c in title): raise ValueError('Некорректное название')
        p = BASE / 'movie-titles-ru.json'; titles = json.loads(p.read_text())
        titles.setdefault('titles', {})[name] = title; save(p, titles)
        return {'saved': True}
    if kind == 'branding':
        values = data['values']
        logo = ch['logo']; caption = ch['title_overlay']
        logo['width'] = integer(values['logo_width'], 100, 500)
        logo['x'] = integer(values['logo_x'], 0, 500)
        logo['y'] = integer(values['logo_y'], 0, 900)
        if values['corner'] not in ['top_right', 'top_left', 'bottom_right', 'bottom_left']: raise ValueError('Некорректное положение')
        logo['corner'] = values['corner']
        caption['size'] = integer(values['title_size'], 20, 64)
        caption['x'] = integer(values['title_x'], 0, 1500)
        caption['y'] = integer(values['title_y'], 0, 1000)
        color = values['title_color']
        if not re.fullmatch('#[a-fA-F0-9]{6}', color): raise ValueError('Некорректный цвет')
        caption['color'] = color
        if data.get('png'):
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = 4_000_000
            raw = base64.b64decode(data['png'], validate=True)
            if len(raw) > 4_000_000: raise ValueError('Логотип больше 4 МБ')
            picture = Image.open(io.BytesIO(raw))
            if picture.format != 'PNG' or picture.width * picture.height > 4_000_000: raise ValueError('Нужен PNG до 4 млн пикселей')
            picture = picture.convert('RGBA'); bbox = picture.getchannel('A').getbbox()
            if not bbox: raise ValueError('Логотип полностью прозрачный')
            # New assets preserve every original image and are private until the config is saved.
            stem = 'panel-' + str(cid) + '-' + str(time.time_ns())
            ASSETS.mkdir(mode=0o750, exist_ok=True)
            import grp
            os.chown(ASSETS, 0, grp.getgrnam('cyrius-media').gr_gid)
            png = ASSETS / (stem + '.png'); pam = ASSETS / (stem + '.pam')
            picture.save(png)
            header = f'P7\nWIDTH {picture.width}\nHEIGHT {picture.height}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n'.encode()
            pam.write_bytes(header + picture.tobytes())
            import pwd, grp
            for p in [png, pam]:
                os.chown(p, pwd.getpwnam('ffpu').pw_uid, grp.getgrnam('cyrius-media').gr_gid); p.chmod(0o640)
            logo['path'] = str(pam)
            logo['crop'] = [bbox[0], bbox[1], bbox[2]-bbox[0], bbox[3]-bbox[1]]
            logo['url'] = json.loads((BASE / 'site.json').read_text())['panel_url'].rstrip('/') + '/icons/' + png.name
        save(cfg_path, cfg)
        return {'saved': True, 'applies': 'next_media_or_reload'}
    raise ValueError('Неизвестное действие')


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(6_000_001)
        if len(raw) > 6_000_000: raise ValueError('Слишком большой запрос')
        descriptor = os.open(BASE / '.panel-config.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = main(json.loads(raw))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False)); sys.exit(1)
