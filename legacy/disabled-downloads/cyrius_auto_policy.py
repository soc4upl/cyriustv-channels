"""Additional fail-closed media checks for automatically selected torrents."""
import re


def rejection_reason(data, labels, selected_audio_index):
    if 'cyrius-auto' not in labels:
        return None
    if not 3600 <= float(data.get('format', {}).get('duration', 0)) <= 14400:
        return 'Автоподбор: длительность вне диапазона 60–240 минут'
    video = next((s for s in data['streams'] if s.get('codec_type') == 'video'), {})
    if video.get('codec_name') != 'h264':
        return 'Автоподбор: требуется совместимое видео H.264'
    if not 900 <= video.get('width', 0) <= 1920 or not 500 <= video.get('height', 0) <= 1080:
        return 'Автоподбор: разрешение не соответствует выбранному качеству'
    if re.search(r'10|12|16', video.get('pix_fmt', '')):
        return 'Автоподбор: 10/12/16-битное видео требует отдельной подготовки'
    audio = next((s for s in data['streams'] if s.get('index') == selected_audio_index and s.get('codec_type') == 'audio'), {})
    language = audio.get('tags', {}).get('language', '').casefold()
    if language in ('rus', 'ru'):
        return None
    return 'Автоподбор: русская звуковая дорожка не подтверждена'
