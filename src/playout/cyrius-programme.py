#!/usr/bin/python3
"""Build a rolling XMLTV guide from the actual stream-copy queue and position."""
import json
import os
from pathlib import Path
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
from cyrius_queue import policy as queue_policy

TIMEZONE = os.environ.get('CYRIUS_TIMEZONE', 'Asia/Baku')
TZ = ZoneInfo(TIMEZONE)
ORIGIN = os.environ.get('CYRIUS_ORIGIN_URL', 'http://127.0.0.1:18080').rstrip('/')
PUBLIC = Path(os.environ.get('CYRIUS_PUBLIC_ROOT', '/srv/cyrius/public-copy'))
SLUGS = {1: 'cyrius-boeviki', 2: 'cyrius-comedy', 3: 'cyrius-kids-hd', 4: 'cyrius-horror', 5: 'cyrius-cinema-hd'}
NOW = time.time()
HORIZON = NOW + 24 * 3600


def formatted(epoch, xml=False):
    return datetime.fromtimestamp(epoch, TZ).strftime('%Y%m%d%H%M%S %z') if xml else datetime.fromtimestamp(epoch, TZ).isoformat(timespec='seconds')


def atomic(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(value)
    temporary.replace(path)


config = json.loads(Path(os.environ.get('CYRIUS_CONFIG_PATH', '/etc/cyrius/copy-channels.json')).read_text())
localized_titles = json.loads((Path(os.environ.get('CYRIUS_CONFIG_PATH', '/etc/cyrius/copy-channels.json')).parent / 'movie-titles-ru.json').read_text()).get('titles', {})
guide = {'generated_at': formatted(NOW), 'timezone': TIMEZONE, 'mode': 'automatic_movie_loop', 'estimated_times': True, 'channels': []}
tv = ET.Element('tv', {'generator-info-name': 'CYRIUS live playout'})
playlist = [f'#EXTM3U url-tvg="{ORIGIN}/epg.xml"']
for channel in config['channels']:
    cid = channel['id']
    slug = channel.get('slug', SLUGS.get(cid))
    if not slug:
        raise RuntimeError('Channel has no public route: ' + channel['name'])
    state_root = Path(os.environ.get('CYRIUS_STATE_ROOT', '/var/lib/cyrius-copy')) / str(cid)
    state = json.loads((state_root / 'state.json').read_text())
    catalog = json.loads((state_root / 'catalog.json').read_text())['files']
    catalog = [dict(item, title=localized_titles.get(Path(item['source']).name, item['title'])) for item in catalog]
    source = state.get('source', '')
    index = next((i for i, item in enumerate(catalog) if item['source'] == source), None)
    if (index is None and state.get('status') != 'queue_ended') or NOW - state.get('updated_at', 0) > 60:
        raise RuntimeError('Waiting for current live queue: ' + channel['name'])
    started = state.get('started_at', state['updated_at'] - state.get('position', 0))
    programmes = []
    if state.get('status') == 'queue_ended':
        programmes = []
    elif Path(source).name in ('cyrius-standby.mp4', '00-standby.mp4'):
        programmes = [{'title': channel['name'] + ' — техническая заставка', 'start': formatted(NOW - 60), 'stop': formatted(HORIZON), 'current': True}]
    else:
        for _ in range(2000):
            item = catalog[index]
            ended = started + item['duration']
            if ended > NOW:
                programmes.append({'title': item['title'], 'start': formatted(started), 'stop': formatted(ended), 'current': started <= NOW < ended})
            if ended >= HORIZON:
                break
            if not queue_policy(cid).get('repeat', True) and index + 1 >= len(catalog):
                break
            started = ended
            index = (index + 1) % len(catalog)
    guide['channels'].append({'id': slug, 'name': channel['name'], 'programmes': programmes})
    node = ET.SubElement(tv, 'channel', {'id': slug})
    ET.SubElement(node, 'display-name', {'lang': 'ru'}).text = channel['name']
    logo_url = channel.get('logo', {}).get('url', '')
    if logo_url:
        ET.SubElement(node, 'icon', {'src': logo_url})
    for programme in programmes:
        beginning = datetime.fromisoformat(programme['start']).timestamp()
        ending = datetime.fromisoformat(programme['stop']).timestamp()
        node = ET.SubElement(tv, 'programme', {'channel': slug, 'start': formatted(beginning, True), 'stop': formatted(ending, True)})
        ET.SubElement(node, 'title', {'lang': 'ru'}).text = programme['title']
        ET.SubElement(node, 'desc', {'lang': 'ru'}).text = 'Кинозалы CyriusTV.cc - Фильмы NON-STOP'
    playlist.extend([f'#EXTINF:-1 tvg-id="{slug}" tvg-name="{channel["name"]}" tvg-logo="{logo_url}" group-title="CYRIUS",{channel["name"]}', f'{ORIGIN}/{slug}/index.m3u8'])
assert len(guide['channels']) == len(config['channels'])
atomic(PUBLIC / 'programme.json', json.dumps(guide, ensure_ascii=False, indent=2) + '\n')
atomic(PUBLIC / 'epg.xml', '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding='unicode') + '\n')
atomic(PUBLIC / 'channels.m3u', '\n'.join(playlist) + '\n')
print(f"Published rolling 24-hour programme for {len(guide['channels'])} channels; timezone {TIMEZONE}")
