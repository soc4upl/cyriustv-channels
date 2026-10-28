#!/usr/bin/python3
"""Select standalone Russian-language movies and enqueue bounded Transmission jobs."""
import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import fcntl
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

STATE = Path('/var/lib/cyrius-auto-download')
CONFIG = Path('/etc/cyrius/auto-download.json')
GENRES = {'Boevik': 'Cyrius Боевики', 'Comedy': 'Cyrius Comedy', 'KidsHD': 'Cyrius Kids HD', 'Horror': 'Cyrius Horror', 'CinemaHD': 'Cyrius Cinema HD', 'Retro': 'Cyrius Ретро HD', 'Romance': 'Cyrius Романтика HD'}
VIDEO = {'.mkv', '.mp4', '.avi'}
BAD = re.compile(r'\b(?:CAMRip|HDCAM|HDTS|TELESYNC|TS|TC|DVDScr|SCR|2160p|4K|UHD|HDR10|HDR|Dolby\s*Vision|3D|HEVC|H\.?265|x265|AV1)\b', re.I)
SERIES = re.compile(r'(?:\bS\d{1,2}(?:E\d{1,3})?\b|\b\d{1,2}[xх]\d{1,3}|сезон|сери[ияй]|эпизод|из\s+\d+\s*\]|сборник|коллекция|трилогия|дилогия)', re.I)
QUALITY = re.compile(r'\b(?:BDRip|BluRay|WEB[- .]?DL(?:Rip)?|WEBRip)\b', re.I)


def atomic(path, value, mode=0o600):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(mode)
    temporary.replace(path)


def store_movie_title(path, filename, title):
    """Merge under the panel writer's lock so auto-selection preserves manual edits."""
    descriptor = os.open(path.parent / '.panel-config.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        names = json.loads(path.read_text()) if path.exists() else {'titles': {}}
        names.setdefault('titles', {}).setdefault(filename, title)
        atomic(path, names, 0o644)
        return names


def normalized(value):
    value = unicodedata.normalize('NFKD', value.casefold().replace('ё', 'е'))
    return ''.join(c for c in value if c.isalnum())


def identities(title, year):
    return {(normalized(part), str(year)) for part in title.split(' / ') if normalized(part)}


def filename_identity(name):
    name = Path(name).stem if Path(name).suffix.casefold() in VIDEO else name
    match = re.search(r'(?<!\d)((?:19|20)\d{2})(?!\d)', name)
    if not match:
        cleaned = re.sub(r'[._]+', ' ', name)
        cleaned = re.split(r'\b(?:WEB[- ]?DL|WEBRip|BDRip|BluRay|1080[piр]|720[piр]|HEVC|x264|H264)\b', cleaned, maxsplit=1, flags=re.I)[0].strip(' (-')
        return identities(cleaned, '*') if cleaned else set()
    return identities(re.sub(r'[._]+', ' ', name[:match.start()]).strip(' (-'), match[1])


def already_known(keys, existing):
    return bool(keys & existing or {(name, '*') for name, _ in keys} & existing)


def safe_title(title):
    return not BAD.search(title) and not SERIES.search(title) and bool(QUALITY.search(title)) and bool(re.search(r'\b(?:720|1080)[piр]\b', title, re.I))


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'textarea'):
            self.skip += 1
        if tag in ('br', 'tr', 'td', 'p', 'div') and not self.skip:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'textarea') and self.skip:
            self.skip -= 1
        if tag in ('tr', 'td', 'p', 'div') and not self.skip:
            self.parts.append('\n')

    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)


def plain(markup):
    p = PageText()
    p.feed(markup)
    return '\n'.join(' '.join(line.split()) for line in ''.join(p.parts).splitlines() if line.strip())


def field(text, *names):
    for name in names:
        match = re.search(r'(?:^|\n)' + re.escape(name) + r'\s*:\s*([^\n]+)', text, re.I)
        if match:
            return match[1].strip()
    return ''


def movie_channel(genre, year):
    if 'ужас' in genre:
        return 'Horror'
    if 1900 <= year <= 1999:
        return 'Retro'
    if 'боевик' in genre or 'военн' in genre:
        return 'Boevik'
    if any(word in genre for word in ('мелодрам', 'романтик', 'романтич')):
        return 'Romance'
    if 'комеди' in genre:
        return 'Comedy'
    return 'CinemaHD' if genre else None


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('rutor.info', 'd.rutor.info') or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Unapproved download destination')


def fetch(url, limit=2_000_000):
    validate_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'CYRIUS-Media-Automation/1.0'})
    with urllib.request.build_opener(RestrictedRedirect()).open(request, timeout=15) as response:
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Response exceeds limit')
        return raw


def page_metadata(tid, listing, category, popular):
    cache = STATE / 'pages' / (str(tid) + '.json')
    if cache.exists() and time.time() - cache.stat().st_mtime < 3600:
        data = json.loads(cache.read_text())
        if data.get('channel') not in (None, 'KidsHD'):
            data['channel'] = movie_channel(data['genres'], data['year'])
        return dict(data, popular=popular)
    markup = fetch('https://rutor.info/torrent/' + str(tid)).decode('utf-8', errors='replace')
    details = re.split(r'id=[\"\x27]details[\"\x27]', markup, maxsplit=1)
    if len(details) != 2:
        raise ValueError('Torrent details absent')
    # Ignore linked releases, comments, adverts and executable page content.
    details = details[1].split('Связанные раздачи', 1)[0]
    text = plain(details)
    genre = field(text, 'Жанр', 'Жанры').casefold()
    russian = field(text, 'Название', 'Название фильма', 'Русское название')
    original = field(text, 'Оригинальное название', 'Ориг. название')
    year_match = re.search(r'(?<!\d)((?:19|20)\d{2})(?!\d)', field(text, 'Год', 'Год выхода', 'Год выпуска'))
    if not year_match:
        year_match = re.search(r'\(((?:19|20)\d{2})\)', listing)
    if not russian:
        russian = re.split(r'\s*/\s*|\s*\((?:19|20)\d{2}\)', listing)[0].strip()
    if not original:
        pieces = re.split(r'\(((?:19|20)\d{2})\)', listing, maxsplit=1)[0].split(' / ')
        original = ' / '.join(pieces[1:])
    runtime = field(text, 'Продолжительность', 'Длительность')
    clock = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', runtime)
    minutes = int(clock[1]) * 60 + int(clock[2]) if clock else 0
    seeds = re.search(r'(?:^|\n)Раздают\s*\n?\s*(\d+)', text)
    production = field(text, 'Производство', 'Страна', 'Страна производства')
    category_name = re.search(r'(?:^|\n)Категория\s*\n([^\n]+)', text)
    video = field(text, 'Видео', 'Видео #1')
    dimensions = re.search(r'(\d{3,4})\s*[xх×]\s*(\d{3,4})', video)
    age = re.search(r'(?:возраст\w*[^\n]*|рейтинг[^\n]*|ограничения[^\n]*)\b(0|6|12|16|18)\s*\+', text, re.I)
    audio_lines = '\n'.join(line for line in text.splitlines() if re.match(r'(?:Аудио|Язык|Перевод|Звук)', line, re.I))
    native = bool(re.search(r'Россия|СССР', production, re.I))
    russian_audio = bool(re.search(r'русск|Russian', audio_lines, re.I)) or native
    # Translation statements sometimes have their label and value on separate lines.
    if not russian_audio:
        russian_audio = bool(re.search(r'(?:Аудио[^\n]*|Язык[^\n]*|Перевод)\s*:?\s*\n?[^\n]*(?:русск|Russian|дублир|многоголос)', text, re.I))
    year = int(year_match[1]) if year_match else 0
    if any(word in genre for word in ('эротик', 'порно', 'короткометраж', 'документаль')) or SERIES.search(listing):
        channel = None
    elif 'мульт' in genre or category == 7:
        positive = any(word in genre for word in ('семейн', 'детск'))
        adult = any(word in genre for word in ('ужас', 'эрот', 'порно')) or (age and int(age[1]) > 12)
        channel = 'KidsHD' if positive and not adult else None
    else:
        channel = movie_channel(genre, year)
    data = {'tid': tid, 'title': russian, 'original': original, 'year': year, 'genres': genre,
            'channel': channel, 'minutes': minutes, 'seeds': int(seeds[1]) if seeds else 0,
            'russian_audio': russian_audio, 'native_russian': native, 'video': video,
            'width': int(dimensions[1]) if dimensions else 0, 'height': int(dimensions[2]) if dimensions else 0,
            'category': category_name[1] if category_name else '', 'listing': listing, 'popular': popular,
            'external_ids': re.findall(r'(?:imdb\.com/title/(tt\d+)|kinopoisk\.ru/(?:film|series)/(\d+))', details)}
    atomic(cache, data)
    return data


def eligible(m, cfg, current_year):
    if not m['channel'] or not m['russian_audio'] or len(m['title']) > 200 or not any('\u0400' <= c <= '\u04ff' for c in m['title']):
        return False
    if not safe_title(m['listing']) or not 60 <= m['minutes'] <= 240 or not 1900 <= m['year'] <= current_year:
        return False
    if m['category'] not in ('Зарубежные фильмы', 'Наши фильмы', 'Мультипликация'):
        return False
    if m['width'] < 900 or m['width'] > 1920 or m['height'] < 500 or m['height'] > 1080:
        return False
    if not re.search(r'AVC|H\.?264|x264', m['video'], re.I) or re.search(r'10\s*bit|10\s*бит|HEVC|H\.?265|AV1', m['video'], re.I):
        return False
    threshold = cfg['min_seeds_new'] if m['year'] >= current_year - 1 else cfg['min_seeds_catalogue']
    threshold = cfg.get('min_seeds_by_channel', {}).get(m['channel'], threshold)
    return m['seeds'] >= threshold


def bdecode(raw):
    pos, info_span = 0, None

    def read(depth=0):
        nonlocal pos, info_span
        if depth > 24 or pos >= len(raw):
            raise ValueError('Invalid torrent nesting')
        tag = raw[pos:pos + 1]
        if tag == b'i':
            end = raw.index(b'e', pos)
            value = int(raw[pos + 1:end]); pos = end + 1
            return value
        if tag in (b'l', b'd'):
            pos += 1; value = [] if tag == b'l' else {}
            while raw[pos:pos + 1] != b'e':
                if tag == b'l':
                    value.append(read(depth + 1))
                else:
                    key = read(depth + 1)
                    if not isinstance(key, bytes) or key in value:
                        raise ValueError('Invalid torrent dictionary')
                    begin = pos; value[key] = read(depth + 1)
                    if depth == 0 and key == b'info':
                        info_span = (begin, pos)
            pos += 1
            return value
        colon = raw.index(b':', pos)
        size = int(raw[pos:colon]); pos = colon + 1
        if size < 0 or pos + size > len(raw):
            raise ValueError('Invalid torrent string')
        value = raw[pos:pos + size]; pos += size
        return value

    value = read()
    if pos != len(raw) or not info_span:
        raise ValueError('Not a torrent')
    return value, hashlib.sha1(raw[info_span[0]:info_span[1]]).hexdigest()


def torrent_details(raw, cfg):
    data, digest = bdecode(raw); info = data[b'info']
    if (info.get(b'private') and not cfg.get('allow_private')) or not isinstance(info.get(b'pieces'), bytes):
        raise ValueError('Unsupported/private torrent')
    name = info.get(b'name.utf-8', info[b'name']).decode('utf-8', errors='strict')
    rows = info.get(b'files', [{b'path': [], b'length': info.get(b'length')}])
    paths, total = [], 0
    for row in rows:
        parts = [p.decode('utf-8', errors='strict') for p in row.get(b'path.utf-8', row[b'path'])]
        components = [name] + parts
        if any(not p or p in ('.', '..') or '/' in p or '\\' in p or '\x00' in p for p in components):
            raise ValueError('Unsafe torrent path')
        length = row[b'length']
        if not isinstance(length, int) or length < 0 or row.get(b'symlink path') or b'l' in row.get(b'attr', b''):
            raise ValueError('Unsupported torrent entry')
        relative = PurePosixPath(*components)
        suffix = relative.suffix.casefold()
        if suffix not in VIDEO | {'.nfo', '.txt', '.jpg', '.jpeg', '.png', '.srt', '.ass', '.sub', '.idx'}:
            raise ValueError('Non-media payload')
        paths.append((str(relative), length)); total += length
    videos = [(p, size) for p, size in paths if Path(p).suffix.casefold() in VIDEO and size > 100 * 1024**2]
    if len(videos) != 1 or videos[0][1] < total * 0.95 or not 0.4 * 1024**3 <= total <= cfg['max_torrent_gib'] * 1024**3:
        raise ValueError('Not one bounded feature film')
    return digest, total, videos[0][0]


class RPC:
    def __init__(self):
        directory = Path(os.environ.get('CREDENTIALS_DIRECTORY', '/root'))
        path = directory / ('transmission-login' if 'CREDENTIALS_DIRECTORY' in os.environ else 'cyrius-transmission-credentials.json')
        c = json.loads(path.read_text())
        self.headers = {'Authorization': 'Basic ' + base64.b64encode((c['username'] + ':' + c['password']).encode()).decode()}

    def call(self, method, arguments=None):
        for attempt in range(3):
            try:
                req = urllib.request.Request('http://127.0.0.1:9091/transmission/rpc', data=json.dumps({'method': method, 'arguments': arguments or {}}).encode(), headers=self.headers)
                with urllib.request.urlopen(req, timeout=30) as response:
                    data = json.load(response)
                if data['result'] != 'success':
                    raise RuntimeError('Transmission rejected ' + method)
                return data['arguments']
            except urllib.error.HTTPError as error:
                if error.code != 409:
                    raise
                self.headers['X-Transmission-Session-Id'] = error.headers['X-Transmission-Session-Id']
            except (TimeoutError, urllib.error.URLError):
                if attempt == 2:
                    raise
                time.sleep(2)
        raise RuntimeError('Transmission session unavailable')


def sources(cfg):
    found, errors = {}, []
    for category in (1, 5, 7):
        try:
            doc = ET.fromstring(fetch(f'https://rutor.info/rss.php?cat={category}'))
            for item in doc.findall('.//item'):
                title = item.findtext('title', ''); link = item.findtext('link', '')
                tid = re.search(r'(?:rss=|/download/)(\d+)', link)
                if tid and safe_title(title):
                    found[int(tid[1])] = (title, category, False)
        except Exception as error:
            errors.append('RSS ' + str(category) + ': ' + type(error).__name__)
    try:
        markup = fetch('https://rutor.info/top').decode('utf-8', errors='replace')
        for tid, title in re.findall(r'<a\b[^>]*href=[\"\x27]/torrent/(\d+)/[^\"\x27]+[\"\x27][^>]*>(.*?)</a>', markup, re.S):
            title = plain(title)
            if safe_title(title):
                found[int(tid)] = (title, found.get(int(tid), ('', 0, False))[1], True)
    except Exception as error:
        errors.append('Top: ' + type(error).__name__)
    thematic = {}
    buckets = []
    for url in cfg.get('listing_pages', []):
        try:
            markup = fetch(url).decode('utf-8', errors='replace')
            bucket = []
            for tid, title in re.findall(r'<a\b[^>]*href=[\"\x27]/torrent/(\d+)/[^\"\x27]+[\"\x27][^>]*>(.*?)</a>', markup, re.S):
                title = plain(title)
                if safe_title(title):
                    bucket.append((int(tid), (title, 0, False)))
                    if len(bucket) >= cfg.get('items_per_listing', 10):
                        break
            buckets.append(bucket)
        except Exception as error:
            errors.append('Genre catalogue: ' + type(error).__name__)
    # Interleave sources so a large action catalogue cannot starve Kids or Retro.
    for index in range(max((len(b) for b in buckets), default=0)):
        for bucket in buckets:
            if index < len(bucket):
                tid, value = bucket[index]
                thematic[tid] = value
    offset = cfg.get('scan_offset', 0)
    entries = list(thematic.items())
    if entries:
        offset %= len(entries)
        thematic = dict(entries[offset:] + entries[:offset])
    return ({**thematic, **found} if cfg.get('campaign_scan') else {**found, **thematic}), errors


def selection_quota(cfg, ledger, today, rows):
    accepted = [r for r in ledger if r['status'] in ('reserved', 'added')]
    allowed = cfg.get('download_channels', list(GENRES))
    campaign = cfg.get('campaign', {})
    if campaign.get('enabled'):
        channels = [c for c in campaign['channels'] if c in allowed]
        records = [r for r in accepted if r.get('campaign') == campaign['id']]
        targets = {c: campaign['per_channel'] for c in channels}
        counts = Counter(r['channel'] for r in records)
        incomplete = [c for c in channels if counts[c] < targets[c]]
        hashes = {r['hash'] for r in records}
        outstanding = any(r['hashString'] in hashes and r['percentDone'] < 1 for r in rows)
        if incomplete or outstanding:
            return {'records': records, 'targets': targets, 'limit': sum(targets.values()),
                    'byte_limit': sum(targets.values()) * cfg.get('max_torrent_bytes', round(cfg['max_torrent_gib'] * 1024**3)),
                    'max_pending': campaign.get('max_pending', sum(targets.values())),
                    'campaign': campaign['id'], 'phase': 'selecting' if incomplete else 'downloading'}
    records = [r for r in accepted if r['day'] == today]
    return {'records': records, 'targets': {c: cfg['max_per_channel_day'] for c in allowed},
            'limit': cfg['daily_limit'], 'byte_limit': cfg['daily_gib'] * 1024**3,
            'max_pending': cfg['max_pending'], 'campaign': None, 'phase': 'daily'}


def main():
    if Path('/etc/cyrius/torrent-disabled.json').exists():
        print('BitTorrent disabled: copyright complaint containment')
        return
    args = argparse.ArgumentParser(); args.add_argument('--dry-run', action='store_true'); dry = args.parse_args().dry_run
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    STATE.mkdir(exist_ok=True, mode=0o700); (STATE / 'pages').mkdir(exist_ok=True)
    with (STATE / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        cfg = json.loads(CONFIG.read_text()); rpc = RPC(); now = datetime.now(ZoneInfo('Asia/Baku')); today = now.date().isoformat()
        if not cfg.get('enabled') and not dry:
            return
        state_path = STATE / 'state.json'; state = json.loads(state_path.read_text()) if state_path.exists() else {'ledger': []}
        rows = rpc.call('torrent-get', {'fields': ['id', 'hashString', 'name', 'labels', 'status', 'percentDone', 'error', 'downloadDir', 'leftUntilDone', 'addedDate']})['torrents']
        existing, hashes = set(), {r['hashString'].lower() for r in rows}
        for record in state['ledger']:
            if record['status'] == 'reserved':
                if record['hash'] in hashes:
                    record['status'] = 'added'
                elif time.time() - datetime.fromisoformat(record['added_at']).timestamp() > 180:
                    record['status'] = 'failed'
        if not dry:
            atomic(state_path, state)
        names_path = Path('/etc/cyrius/movie-titles-ru.json'); names = json.loads(names_path.read_text())
        actual_filenames = {Path(row['name']).name for row in rows}
        for row in rows:
            existing |= filename_identity(row['name'])
        for channel in json.loads(Path('/etc/cyrius/copy-channels.json').read_text())['channels']:
            for path in Path(channel['media']).rglob('*'):
                if path.suffix.casefold() in VIDEO:
                    existing |= filename_identity(path.name)
                    actual_filenames.add(path.name)
        for filename in actual_filenames:
            name = names['titles'].get(filename)
            if name:
                existing |= filename_identity(name)
                year = re.search(r'\(((?:19|20)\d{2})\)', name)
                if year:
                    existing |= identities(name[:year.start()].strip(), year[1])
        for record in state['ledger']:
            if record['status'] in ('reserved', 'added'):
                existing |= identities(record['title'], record['year']) | identities(record.get('original', ''), record['year'])
        auto = [r for r in rows if 'cyrius-auto' in r.get('labels', [])]
        pending = [r for r in auto if r['percentDone'] < 1 and 'cyrius-manual-paused' not in r.get('labels', [])]
        active = [r for r in pending if r['status'] in (1, 2, 3, 4)]
        active.sort(key=lambda r: (r['addedDate'], r['id']))
        disk = Path(cfg['download_root']); root_free = shutil.disk_usage('/').free; free = shutil.disk_usage(disk).free
        incomplete_remaining = sum(r['leftUntilDone'] for r in rows)
        target_remaining = sum(r['leftUntilDone'] for r in rows if Path(r['downloadDir']).is_relative_to(disk))
        reserve = cfg['reserve_gib'] * 1024**3
        available = min(root_free - incomplete_remaining - reserve, free - target_remaining - reserve)
        mount_ok = Path('/srv/cyrius/disk2').is_mount()
        daily = [r for r in state['ledger'] if r['day'] == today and r['status'] in ('reserved', 'added')]
        quota = selection_quota(cfg, state['ledger'], today, rows)
        per_channel = Counter(r['channel'] for r in quota['records']); count = len(quota['records']); day_bytes = sum(r['bytes'] for r in quota['records'])
        if not dry and len(active) > cfg['max_active']:
            rpc.call('torrent-stop', {'ids': [row['id'] for row in active[cfg['max_active']:]]})
            active = active[:cfg['max_active']]
        if not dry and (not mount_ok or available <= 0) and active:
            rpc.call('torrent-stop', {'ids': [row['id'] for row in active]})
            active = []
        if not dry and mount_ok and available > 0:
            for row in sorted(pending, key=lambda r: r['addedDate']):
                if len(active) >= cfg['max_active']:
                    break
                if row['status'] == 0 and not row['error']:
                    rpc.call('torrent-start', {'ids': [row['id']]}); active.append(row)
        status = {'checked_at': now.isoformat(), 'mode': 'preview' if dry else 'enabled', 'daily_limit': cfg['daily_limit'], 'today_added': len(daily),
                  'selection_phase': quota['phase'], 'campaign': quota['campaign'], 'targets': quota['targets'], 'selected_by_channel': dict(per_channel),
                  'pending': len(pending), 'active': len(active), 'available_for_new_gib': round(max(0, available) / 1024**3, 1),
                  'reserve_gib': cfg['reserve_gib'], 'candidates': [], 'added': [], 'source_errors': [], 'skipped': {}}
        if count >= quota['limit'] or len(pending) >= quota['max_pending'] or not mount_ok or available <= 0:
            status['selection_hold'] = 'quota_or_queue_full' if mount_ok and available > 0 else 'storage_reserve'
            atomic(STATE / ('preview.json' if dry else 'status.json'), status)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return
        skipped = Counter()
        cfg['scan_offset'] = state.get('scan_offset', 0)
        cfg['campaign_scan'] = bool(quota['campaign'])
        if quota['campaign']:
            cfg['min_seeds_by_channel'] = {**cfg.get('min_seeds_by_channel', {}), **cfg['campaign'].get('min_seeds_by_channel', {})}
            cfg['listing_pages'] = cfg['campaign'].get('listing_pages', cfg.get('listing_pages', []))
        page_limit = cfg.get('campaign', {}).get('max_pages', cfg['max_pages']) if quota['campaign'] else cfg['max_pages']
        listings, status['source_errors'] = sources(cfg)
        candidates = []
        def inspect(entry):
            tid, (listing, category, popular) = entry
            try:
                m = page_metadata(tid, listing, category, popular)
                if not eligible(m, cfg, now.year) or m['channel'] not in quota['targets']:
                    return None, 'quality_language_genre_or_availability'
                keys = identities(m['title'], m['year']) | identities(m['original'], m['year'])
                if already_known(keys, existing):
                    return None, 'already_in_library'
                return m, None
            except Exception as error:
                return None, type(error).__name__
        entries = list(listings.items())[:page_limit]
        with ThreadPoolExecutor(max_workers=cfg.get('metadata_workers', 1)) as pool:
            for index, (m, reason) in enumerate(pool.map(inspect, entries), 1):
                if m:
                    candidates.append(m)
                else:
                    skipped[reason] += 1
                if index % 20 == 0:
                    logging.info('Reviewed %s/%s releases; eligible by genre: %s', index, len(entries), dict(Counter(m['channel'] for m in candidates)))
        candidates.sort(key=lambda m: (not bool(re.search(r'1080[piр]', m['listing'], re.I)), not m['popular'], -m['seeds']))
        while candidates and count < quota['limit'] and len(pending) < quota['max_pending']:
            if datetime.now(ZoneInfo('Asia/Baku')).date().isoformat() != today:
                break
            candidates.sort(key=lambda m: per_channel[m['channel']])
            m = candidates.pop(0); channel = m['channel']
            if per_channel[channel] >= quota['targets'].get(channel, 0):
                continue
            keys = identities(m['title'], m['year']) | identities(m['original'], m['year'])
            if already_known(keys, existing):
                continue
            if not mount_ok or available <= 0:
                skipped['storage_reserve'] += 1; break
            try:
                raw = fetch('https://d.rutor.info/download/' + str(m['tid']), limit=4_000_000)
                digest, size, filename = torrent_details(raw, cfg)
                if digest in hashes:
                    skipped['existing_torrent'] += 1; continue
                if size > available or day_bytes + size > quota['byte_limit']:
                    skipped['storage_or_daily_bytes'] += 1; continue
                title = m['title'] + ' (' + str(m['year']) + ')'
                if Path(filename).name in names['titles'] and names['titles'][Path(filename).name] != title:
                    skipped['filename_title_collision'] += 1; continue
                status['candidates'].append({'title': title, 'channel': GENRES[channel], 'size_gib': round(size / 1024**3, 2), 'seeds': m['seeds']})
                record = dict(m, hash=digest, bytes=size, filename=filename, day=today, status='reserved', added_at=now.isoformat())
                if quota['campaign']:
                    record['campaign'] = quota['campaign']
                # Reserve quota before RPC: a timeout may still mean the torrent was added.
                existing |= keys; hashes.add(digest); per_channel[channel] += 1; count += 1; day_bytes += size; available -= size
                if not dry:
                    state['ledger'].append(record); atomic(state_path, state)
                    names = store_movie_title(names_path, Path(filename).name, title)
                    labels = ['cyrius-auto', channel, GENRES[channel]] + (['cyrius-native-russian'] if m['native_russian'] else [])
                    if quota['campaign']:
                        labels.append('cyrius-batch')
                    result = rpc.call('torrent-add', {'metainfo': base64.b64encode(raw).decode(), 'download-dir': str(disk / channel), 'paused': True, 'labels': labels})
                    if 'torrent-added' not in result:
                        duplicate = result.get('torrent-duplicate')
                        observed = rpc.call('torrent-get', {'ids': [duplicate['id']], 'fields': ['id', 'labels', 'downloadDir']})['torrents'] if duplicate else []
                        if not observed or 'cyrius-auto' not in observed[0].get('labels', []) or Path(observed[0]['downloadDir']) != disk / channel:
                            record['status'] = 'duplicate'; atomic(state_path, state); continue
                        # A retry after a timeout can return the torrent added by this attempt.
                        result['torrent-added'] = duplicate
                    record['status'] = 'added'; atomic(state_path, state)
                    if len(active) < cfg['max_active']:
                        rpc.call('torrent-start', {'ids': [result['torrent-added']['id']]}); active.append({'hashString': digest})
                    pending.append({'hashString': digest}); status['added'].append(status['candidates'][-1])
                    logging.info('Added %s to %s', title, GENRES[channel])
            except Exception as error:
                skipped['enqueue_' + type(error).__name__] += 1
                logging.warning('Candidate %s deferred (%s)', m['title'], type(error).__name__)
        status.update(today_added=len([r for r in state['ledger'] if r['day'] == today and r['status'] in ('reserved', 'added')]), pending=len(pending), active=len(active), available_for_new_gib=round(max(0, available) / 1024**3, 1), skipped=dict(skipped), selected_by_channel=dict(per_channel))
        if not dry:
            state['scan_offset'] = state.get('scan_offset', 0) + len(entries)
            atomic(state_path, state)
        atomic(STATE / ('preview.json' if dry else 'status.json'), status)
        print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
