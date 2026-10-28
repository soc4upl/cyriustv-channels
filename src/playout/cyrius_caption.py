"""Render a small, cached RGBA movie caption once per film, not once per frame."""
import hashlib
import json
from pathlib import Path
import re
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageColor


def caption_asset(title, settings, state_directory):
    text = re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', title).strip()
    font_path = Path(settings['font'])
    signature = json.dumps([text, settings, font_path.stat().st_mtime_ns], sort_keys=True, ensure_ascii=False)
    directory = Path(state_directory) / 'captions'
    directory.mkdir(exist_ok=True)
    path = directory / (hashlib.sha256(signature.encode()).hexdigest() + '.pam')
    padding = 6
    if not path.exists():
        size = int(settings.get('size', 36))
        width_limit = int(settings.get('max_width', 1300))
        font = ImageFont.truetype(str(font_path), size)
        lines, current = [], ''
        for word in text.split():
            candidate = (current + ' ' + word).strip()
            if current and font.getlength(candidate) > width_limit:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        text = '\n'.join(lines)
        draw = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
        box = draw.multiline_textbbox((0, 0), text, font=font, spacing=9)
        width, height = box[2] - box[0], box[3] - box[1]
        canvas = Image.new('RGBA', (width + 2 * padding, height + 2 * padding))
        shadow = Image.new('RGBA', canvas.size)
        ImageDraw.Draw(shadow).multiline_text((padding - box[0] + 1, padding - box[1] + 1), text, font=font, spacing=9, fill=(0, 0, 0, 150))
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(1.3)))
        color = ImageColor.getrgb(settings.get('color', '#ffffff'))
        ImageDraw.Draw(canvas).multiline_text((padding - box[0], padding - box[1]), text, font=font, spacing=9, fill=(*color, 255))
        header = f'P7\nWIDTH {canvas.width}\nHEIGHT {canvas.height}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n'.encode()
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(header + canvas.tobytes())
        temporary.replace(path)
    return {'path': str(path), 'x': int(settings.get('x', 48)) - padding, 'y': int(settings.get('y', 32)) - padding}
