"""One stable audio format across films, including when video is packet-copied."""


def audio_args(source_audio):
    if source_audio is None:
        return ['-an']
    return ['-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', '160k',
            '-ar', '48000', '-ac', '2', '-threads:a', '1',
            '-af', 'aresample=48000:async=1:first_pts=0']


def output_audio(source_audio):
    if source_audio is None:
        return None
    return {'index': 1, 'codec_type': 'audio', 'codec_name': 'aac', 'profile': 'LC',
            'sample_rate': '48000', 'channels': 2, 'channel_layout': 'stereo',
            'bit_rate': '160000', 'tags': dict(source_audio.get('tags', {}))}
