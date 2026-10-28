"""Conservative cached detection of black borders in original movie files."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess

FFMPEG = os.environ.get('CYRIUS_FFMPEG', '/usr/bin/ffmpeg')


def picture_crop(source, info, state_directory):
    source = Path(source)
    stat = source.stat()
    signature = str(source) + ':' + str(stat.st_size) + ':' + str(stat.st_mtime_ns)
    directory = Path(state_directory) / 'picture-crops'
    directory.mkdir(exist_ok=True)
    cached = directory / (hashlib.sha256(signature.encode()).hexdigest() + '.json')
    if cached.exists():
        return json.loads(cached.read_text())['crop']
    width, height = info['video']['width'], info['video']['height']
    rectangles = []
    env = dict(os.environ)
    for fraction in (0.18, 0.50, 0.82):
        seek = max(0, min(info['duration'] * fraction, info['duration'] - 5))
        command = [FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'info',
                   '-ss', str(seek), '-noaccurate_seek', '-threads', '1', '-i', str(source),
                   '-map', '0:v:0', '-frames:v', '20', '-vf', 'cropdetect=limit=24:round=2:reset=0',
                   '-an', '-sn', '-dn', '-fps_mode', 'passthrough', '-f', 'null', '-']
        try:
            result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=6)
            found = [tuple(map(int, match)) for match in re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)', result.stderr)]
            found = [(w, h, x, y) for w, h, x, y in found if w > 0 and h > 0 and x >= 0 and y >= 0 and x + w <= width and y + h <= height]
            if not found:
                rectangles = []
                break
            rectangles.append(max(found, key=lambda rectangle: rectangle[0] * rectangle[1]))
        except subprocess.TimeoutExpired:
            rectangles = []
            break
    crop = None
    if len(rectangles) == 3:
        left = min(r[2] for r in rectangles) // 2 * 2
        top = min(r[3] for r in rectangles) // 2 * 2
        right = min(width, ((max(r[2] + r[0] for r in rectangles) + 1) // 2) * 2)
        bottom = min(height, ((max(r[3] + r[1] for r in rectangles) + 1) // 2) * 2)
        removed_x, removed_y = width - (right - left), height - (bottom - top)
        symmetrical = abs(left - (width - right)) <= max(8, width * 0.02) and abs(top - (height - bottom)) <= max(8, height * 0.02)
        significant = removed_x >= max(8, width * 0.015) or removed_y >= max(8, height * 0.015)
        if symmetrical and significant and right - left >= width * 0.5 and bottom - top >= height * 0.5:
            crop = [left, top, right - left, bottom - top]
    temporary = cached.with_suffix('.tmp')
    temporary.write_text(json.dumps({'source': str(source), 'crop': crop, 'samples': rectangles}) + '\n')
    temporary.replace(cached)
    logging.info('Picture border check: %s -> %s', source.name, crop or 'full original frame')
    return crop
