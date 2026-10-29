"""Shared, deterministic FFmpeg options for the supplied CYRIUS logos."""
from fractions import Fraction


def branding_args(channel, video, caption=None):
    logo = channel.get('logo')
    standard = channel.get('video_standard')
    if not logo and not standard and not caption:
        return None
    width, height = video['width'], video['height']
    base = []
    if standard:
        width, height = int(standard['width']), int(standard['height'])
        assert width > 0 and height > 0 and width % 2 == 0 and height % 2 == 0
        assert standard.get('mode', 'crop') == 'crop'
        if video.get('active_picture_crop'):
            left, top, active_width, active_height = video['active_picture_crop']
            base.append(f'crop={active_width}:{active_height}:{left}:{top}')
        # Crop before scaling to avoid resampling pixels that will be discarded.
        # The display aspect ratio includes anamorphic source pixels.
        base.append(f"crop=w='if(gt(dar,{width}/{height}),floor(ih*{width}/{height}/sar/2)*2,iw)':h='if(gt(dar,{width}/{height}),ih,floor(iw*sar*{height}/{width}/2)*2)':x=(iw-ow)/2:y=(ih-oh)/2")
        scale_flags = standard.get('scale_flags', 'bicubic')
        assert scale_flags in ('bicubic', 'bilinear', 'fast_bilinear', 'lanczos')
        base.extend([f'scale={width}:{height}:flags={scale_flags}', 'setsar=1'])
    base_chain = ','.join(base) if base else 'null'
    logo_width = min(250, max(140, round(width * 0.13)))
    logo_width += logo_width % 2
    margin_x, margin_y = max(12, round(width * 0.025)), max(10, round(height * 0.03))
    inputs = []
    if logo:
        logo_width = int(logo.get('width', logo_width))
        logo_width += logo_width % 2
        margin_x, margin_y = int(logo.get('x', margin_x)), int(logo.get('y', margin_y))
        corner = logo.get('corner', 'top_right')
        logo_x = str(margin_x) if corner.endswith('left') else f'main_w-overlay_w-{margin_x}'
        logo_y = str(margin_y) if corner.startswith('top') else f'main_h-overlay_h-{margin_y}'
        inputs.extend(['-i', logo['path']])
        x, y, crop_width, crop_height = logo['crop']
        graph = (f'[0:v]{base_chain}[base];'
                 f'[1:v]crop={crop_width}:{crop_height}:{x}:{y},'
                 f'scale={logo_width}:-2:flags=lanczos,format=rgba[mark];'
                 f'[base][mark]overlay=x={logo_x}:y={logo_y}:'
                 'eof_action=repeat:repeatlast=1:format=yuv420[marked]')
    else:
        graph = f'[0:v]{base_chain}[marked]'
    if caption:
        caption_index = 2 if logo else 1
        inputs.extend(['-i', caption['path']])
        graph += (f';[{caption_index}:v]format=rgba[caption];'
                  f'[marked][caption]overlay=x={caption["x"]}:y={caption["y"]}:'
                  'eof_action=repeat:repeatlast=1:format=yuv420,format=yuv420p[branded]')
    else:
        graph += ';[marked]format=yuv420p[branded]'
    try:
        fps = float(Fraction(video.get('r_frame_rate', '25/1')))
    except (ValueError, ZeroDivisionError):
        fps = 25
    gop = max(24, round(fps * 4))
    return {
        'input': inputs,
        'filter': ['-filter_complex_threads', '1', '-filter_complex', graph, '-map', '[branded]'],
        'codec': ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                  '-crf', '22', '-maxrate', '8000k', '-bufsize', '16000k',
                  '-threads:v', '2', '-pix_fmt', 'yuv420p', '-g', str(gop),
                  '-keyint_min', str(gop), '-sc_threshold', '0',
                  '-force_key_frames', 'expr:gte(t,n_forced*4)',
                  '-max_muxing_queue_size', '2048'],
        'width': logo_width, 'margin_x': margin_x, 'margin_y': margin_y,
        'frame_width': width, 'frame_height': height,
    }
