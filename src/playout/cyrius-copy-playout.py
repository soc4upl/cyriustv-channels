#!/usr/bin/python3
"""Cycle media into HLS, optionally composing channel branding and Full HD output."""
import collections
import json
import logging
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
sys.path.insert(0, '/usr/local/libexec')
from cyrius_branding import branding_args
from cyrius_picture import picture_crop
from cyrius_caption import caption_asset
from cyrius_audio import audio_args, output_audio
from cyrius_queue import arrange as arrange_queue, policy as queue_policy, command as queue_command, acknowledge as queue_ack

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
CHANNEL_ID = int(sys.argv[1])
ALL_CONFIG = Path(os.environ.get('CYRIUS_CONFIG_PATH', '/etc/cyrius/copy-channels.json'))
CFG = next(c for c in json.loads(ALL_CONFIG.read_text())["channels"] if c["id"] == CHANNEL_ID)
SEGMENT_TYPE = CFG.get("segment_type", "fmp4")
assert SEGMENT_TYPE in ("fmp4", "mpegts")
TARGET_DURATION = int(CFG.get("target_duration", 15))
WINDOW_SECONDS = max(float(CFG.get("window_seconds", 90)), 3 * TARGET_DURATION)
RETAIN_SECONDS = max(float(CFG.get("retain_seconds", 600)), 3 * WINDOW_SECONDS + TARGET_DURATION)
MEDIA = Path(CFG["media"]).resolve()
MEDIA_ROOTS = [Path(p).resolve() for p in CFG.get('media_roots', [str(MEDIA)])]
bridge_source = None
PUBLIC = Path(CFG["public"]).resolve()
PUBLIC.mkdir(parents=True, exist_ok=True)
STATE = Path(os.environ.get('CYRIUS_STATE_ROOT', '/var/lib/cyrius-copy')) / str(CHANNEL_ID)
STATE.mkdir(parents=True, exist_ok=True)
ENV = dict(os.environ)
FFMPEG = os.environ.get("CYRIUS_FFMPEG", "/usr/bin/ffmpeg")
FFPROBE = os.environ.get("CYRIUS_FFPROBE", "/usr/bin/ffprobe")
stopping = False
child = None
metadata_cache = {}
segments = collections.deque()
seen = set()
sequence = int(time.time())
discontinuity_sequence = 0
last_published_sequence = None
last_publish_time = 0.0
cache_dirty = False
playing = False
waiting_for_queue = False
cache_file = STATE / "probe-cache.json"
if cache_file.exists():
    for entry in json.loads(cache_file.read_text()):
        metadata_cache[(entry['source'], entry['size'], entry['mtime_ns'])] = entry['info']


def atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def shutdown(*_):
    global stopping
    stopping = True
    if child is not None and child.poll() is None:
        child.terminate()


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def library():
    files = sorted({p for root in MEDIA_ROOTS for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".mp4", ".mkv", ".avi", ".webm", ".mov") and not any(part.startswith(".") for part in p.relative_to(root).parts)})
    if CFG.get('exclude_series'):
        files = [p for p in files if not re.search(r'(?:\bs\d{1,2}e\d{1,3}(?!\d)|\b\d{1,2}[xх]\d{1,3}\b)', p.name, re.I)]
    movies = [p for p in files if p.name != "00-standby.mp4"]
    allowed = CFG.get("allowed_video_codecs")
    if allowed:
        ready, pending = [], []
        for source in movies:
            try:
                info = probe(source)
                if info["video"]["codec_name"] in allowed and (CFG.get("logo") or CFG.get('video_standard') or round(info.get("max_segment_duration", 0)) <= TARGET_DURATION):
                    ready.append(source)
                else:
                    pending.append({"source": str(source), "video": info["video"], "duration": info["duration"], "reason": "unsupported_codec" if info["video"]["codec_name"] not in allowed else "long_keyframe_interval"})
            except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
                logging.warning("Cannot inspect %s: %s", source.name, error)
        atomic_json(STATE / "pending-compatibility.json", pending)
        return pool_catalog(ready or [Path(os.environ.get("CYRIUS_STANDBY", "/srv/cyrius/media/00-assets/cyrius-standby.mp4"))])
    return pool_catalog(movies or files or [Path(os.environ.get("CYRIUS_STANDBY", "/srv/cyrius/media/00-assets/cyrius-standby.mp4"))])


def pool_catalog(files):
    if CFG.get('deduplicate_titles'):
        choices = {}
        for path in files:
            name = re.sub(r'[\W_]+', '', title(path).casefold().replace('ё', 'е'))
            video = probe(path)['video']
            rank = video.get('width', 0) * video.get('height', 0)
            if name not in choices or rank > choices[name][0]:
                choices[name] = (rank, path)
        files = sorted(item[1] for item in choices.values())
    files = arrange_queue(files, CHANNEL_ID)
    if playing and source.is_file() and source not in files:
        files = [source] + files
    if bridge_source is not None and bridge_source.is_file():
        # Keep the currently airing movie first until it finishes; never add a copy.
        current_name = title(bridge_source)
        files = [bridge_source] + [p for p in files if title(p) != current_name and not p.samefile(bridge_source)]
    return catalog(files)


def refreshed_config():
    global CFG
    candidate = next(c for c in json.loads(ALL_CONFIG.read_text())['channels'] if c['id'] == CHANNEL_ID)
    if candidate['media'] != CFG['media'] or candidate['public'] != CFG['public']:
        raise ValueError('Media and output paths require a service restart')
    CFG = candidate


def requested_transition(files, position):
    """Return a validated transition, acknowledging each command exactly once."""
    cmd = queue_command(CHANNEL_ID)
    if not cmd:
        return None
    try:
        if time.time() - cmd.get('at', 0) > 120:
            raise ValueError('Команда устарела; повторите действие')
        action = cmd.get('action')
        if action == 'next':
            target = files[(files.index(source) + 1) % len(files)] if source in files else files[0]
            result = (target, 0.0)
        elif action in ('restart', 'reload'):
            result = (source, 0.0 if action == 'restart' else position)
        elif action == 'play':
            target = Path(cmd.get('source', '')).resolve()
            if not target.is_file() or target.suffix.lower() not in ('.mp4', '.mkv', '.avi', '.webm', '.mov') or not any(target.is_relative_to(root) for root in MEDIA_ROOTS):
                raise ValueError('Фильм не найден в медиатеке канала')
            probe(target)
            result = (target, 0.0)
        else:
            raise ValueError('Неизвестная команда')
        queue_ack(CHANNEL_ID, cmd, True, 'Команда принята')
        return result
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        queue_ack(CHANNEL_ID, cmd, False, str(error))
        return None


def probe(source):
    global cache_dirty
    signature = (str(source), source.stat().st_size, source.stat().st_mtime_ns)
    if signature not in metadata_cache:
        result = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,channels:stream_disposition=default:stream_tags=language,title", "-of", "json", str(source)], capture_output=True, text=True, timeout=25, env=ENV, check=True)
        data = json.loads(result.stdout)
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
        russian = [s for s in audio if s.get("tags", {}).get("language", "").lower() in ("rus", "ru")]
        preferred = russian or audio
        default = [s for s in preferred if s.get("disposition", {}).get("default")]
        selected = (default or preferred or [None])[0]
        metadata_cache[signature] = {"duration": float(data["format"]["duration"]), "video": video, "audio": selected}
        cache_dirty = True
    return metadata_cache[signature]


def title(source):
    localized = (ALL_CONFIG.parent / 'movie-titles-ru.json')
    if localized.exists():
        known = json.loads(localized.read_text()).get('titles', {})
        if source.name in known:
            return known[source.name]
    rules = (ALL_CONFIG.parent / 'title-rules-ru.json')
    if rules.exists():
        normalize = lambda value: re.sub(r'[. _-]+', ' ', value).strip().casefold()
        for prefix, translated in json.loads(rules.read_text()).items():
            if normalize(source.name).startswith(normalize(prefix)):
                year = re.search(r'(?<!\d)((?:19|20)\d{2})(?!\d)', source.name)
                return translated + (' (' + year[1] + ')' if year else '')
    for filename in ALL_CONFIG.parent.glob("titles*.json"):
        config = json.loads(filename.read_text())
        if config.get("channel_id") == CHANNEL_ID and source.name in config.get("titles", {}):
            return config["titles"][source.name]
    if source.name in ("00-standby.mp4", "cyrius-standby.mp4"):
        return CFG["name"]
    name = source.stem
    match = re.match(r"^(.*?)[. _-]+((?:19|20)\d{2})(?:[. _-]|$)", name)
    if match:
        return re.sub(r"[._]+", " ", match[1]).strip() + " (" + match[2] + ")"
    name = re.split(r"[. _-]+(?:BDRip|HDRip|WEB[- .]?DL|WEBRip|2160p|1080p|720p)\b", name, maxsplit=1, flags=re.I)[0]
    return re.sub(r"[._]+", " ", name).strip()


def catalog(files):
    global cache_dirty
    entries = [{"source": str(path), "title": title(path), "duration": probe(path)["duration"]} for path in files]
    atomic_json(STATE / "catalog.json", {"id": CHANNEL_ID, "channel": CFG["name"], "updated_at": time.time(), "files": entries})
    if cache_dirty:
        atomic_json(cache_file, [{"source": key[0], "size": key[1], "mtime_ns": key[2], "info": value} for key, value in metadata_cache.items()])
        cache_dirty = False
    return files


def resume_playlist():
    global sequence, discontinuity_sequence
    playlist = PUBLIC / "index.m3u8"
    if not playlist.exists():
        return
    initialization = None
    duration = None
    discontinuity = False
    try:
        for line in playlist.read_text().splitlines():
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                sequence = int(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                discontinuity_sequence = int(line.split(":", 1)[1])
            elif line == "#EXT-X-DISCONTINUITY":
                discontinuity = True
            elif line.startswith("#EXT-X-MAP:"):
                initialization = re.search(r'URI="([^"]+)"', line).group(1)
            elif line.startswith("#EXTINF:"):
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            elif line and not line.startswith("#") and duration is not None:
                compatible = line.endswith(".ts") if SEGMENT_TYPE == "mpegts" else bool(initialization and line.endswith(".m4s"))
                if compatible and (PUBLIC / line).is_file() and (SEGMENT_TYPE == "mpegts" or (PUBLIC / initialization).is_file()):
                    segments.append({"uri": line, "map": initialization if SEGMENT_TYPE == "fmp4" else None, "duration": duration, "disc": discontinuity, "seq": sequence})
                    seen.add(line)
                sequence += 1
                duration = None
                discontinuity = False
    except (ValueError, AttributeError, OSError):
        segments.clear()


def publish(run, first):
    global sequence, discontinuity_sequence, last_published_sequence, last_publish_time, cache_dirty
    manifest = run / "stream.m3u8"
    if not manifest.exists():
        return first
    initialization = None
    duration = None
    for line in manifest.read_text().splitlines():
        if line.startswith("#EXT-X-MAP:"):
            initialization = re.search(r'URI="([^"]+)"', line).group(1)
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line and not line.startswith("#") and duration is not None and (initialization or SEGMENT_TYPE == "mpegts"):
            uri = run.name + "/" + line
            if uri not in seen and (run / line).is_file():
                if round(duration) > TARGET_DURATION:
                    probe(source)["max_segment_duration"] = duration
                    cache_dirty = True
                    raise RuntimeError(f"Source needs compatible copy: segment {duration:.3f}s exceeds fixed HLS target {TARGET_DURATION}s")
                segments.append({"uri": uri, "map": run.name + "/" + initialization if initialization else None, "duration": duration, "disc": first, "seq": sequence})
                sequence += 1
                first = False
                seen.add(uri)
            duration = None
    total_duration = sum(s["duration"] for s in segments)
    while len(segments) > 1 and total_duration - segments[0]["duration"] >= WINDOW_SECONDS:
        removed = segments.popleft()
        total_duration -= removed["duration"]
        discontinuity_sequence += int(removed["disc"])
    if segments and sequence != last_published_sequence and time.monotonic() - last_publish_time >= TARGET_DURATION / 2:
        version = 7 if any(s["map"] for s in segments) else 6
        lines = ["#EXTM3U", f"#EXT-X-VERSION:{version}", "#EXT-X-INDEPENDENT-SEGMENTS", f"#EXT-X-TARGETDURATION:{TARGET_DURATION}", f"#EXT-X-MEDIA-SEQUENCE:{segments[0]['seq']}", f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity_sequence}"]
        previous_map = None
        for segment in segments:
            if segment["disc"]:
                lines.append("#EXT-X-DISCONTINUITY")
            if segment["map"] and segment["map"] != previous_map:
                lines.append('#EXT-X-MAP:URI="' + segment["map"] + '"')
                previous_map = segment["map"]
            lines.extend([f"#EXTINF:{segment['duration']:.6f},", segment["uri"]])
        temp = PUBLIC / "index.m3u8.tmp"
        temp.write_text("\n".join(lines) + "\n")
        temp.replace(PUBLIC / "index.m3u8")
        last_published_sequence = sequence
        last_publish_time = time.monotonic()
    return first


def cleanup(current):
    used = {s["uri"].split("/", 1)[0] for s in segments}
    referenced = {s["uri"] for s in segments}
    cutoff = time.time() - RETAIN_SECONDS
    for directory in PUBLIC.glob("run-*"):
        if directory != current and directory.name not in used and directory.is_dir() and directory.stat().st_mtime < cutoff:
            shutil.rmtree(directory)
            for uri in list(seen):
                if uri.startswith(directory.name + "/"):
                    seen.remove(uri)
        elif directory == current:
            for fragment in directory.glob("seg-*"):
                uri = directory.name + "/" + fragment.name
                if fragment.suffix in (".ts", ".m4s") and uri not in referenced and fragment.stat().st_mtime < cutoff:
                    fragment.unlink()
                    seen.discard(uri)


resume_playlist()
state_path = STATE / "state.json"
saved = json.loads(state_path.read_text()) if state_path.exists() else CFG.get("seed", {})
waiting_for_queue = saved.get('status') == 'queue_ended'
waiting_version = queue_policy(CHANNEL_ID).get('version', 0)
source = Path(saved.get("source", ""))
if CFG.get('resume_once_source') and source == Path(CFG['resume_once_source']) and source.is_file():
    bridge_source = source
seek = max(0.0, float(saved.get("position", 0)) + max(0, time.time() - float(saved.get("updated_at", time.time())))) if source.is_file() else 0.0
completed_movies = 0
while not stopping:
    refreshed_config()
    files = library()
    if waiting_for_queue:
        transition = requested_transition(files, 0)
        if transition or queue_policy(CHANNEL_ID).get('version', 0) != waiting_version:
            source, seek = transition or (files[0], 0.0)
            bridge_source = source if source not in files else None
            waiting_for_queue = False
        else:
            saved_state = json.loads(state_path.read_text())
            saved_state.update(status='queue_ended', updated_at=time.time())
            atomic_json(state_path, saved_state)
            atomic_json(PUBLIC / 'now.json', {k: v for k, v in saved_state.items() if k != 'source'})
            time.sleep(2)
            continue
    if source not in files:
        source, seek = files[0], 0.0
    try:
        info = dict(probe(source))
        info['video'] = dict(info['video'])
        if CFG.get('video_standard'):
            info['video']['active_picture_crop'] = picture_crop(source, info, STATE)
        duration = min(info["duration"], float(CFG.get("test_seconds", info["duration"])))
        if seek >= duration:
            if source == bridge_source:
                bridge_source = None
                files = library()
                source, seek = files[0], 0.0
                continue
            source, seek = files[(files.index(source) + 1) % len(files)], 0.0
            continue
        run = PUBLIC / ("run-" + str(time.time_ns()) + "-" + str(os.getpid()))
        run.mkdir()
        args = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-stats_period", "2", "-progress", "pipe:1", "-fflags", "+genpts", "-re"]
        if seek:
            args.extend(["-ss", f"{seek:.3f}"])
        caption = caption_asset(title(source), CFG['title_overlay'], STATE) if CFG.get('title_overlay') else None
        branding = branding_args(CFG, info['video'], caption)
        if branding:
            args.extend(['-threads', '1'])
        args.extend(["-i", str(source)])
        if branding:
            args.extend(branding['input'])
            args.extend(branding['filter'])
        else:
            args.extend(['-map', '0:v:0'])
        if info["audio"] is not None:
            args.extend(["-map", "0:" + str(info["audio"]["index"])])
        args.extend(branding['codec'] if branding else ["-c:v", "copy"])
        args.extend(audio_args(info["audio"]))
        args.extend(["-sn", "-dn"])
        if not branding and info["video"]["codec_name"] == "mpeg4":
            args.extend(["-bsf:v", "mpeg4_unpack_bframes"])
        if not branding and info["video"]["codec_name"] == "hevc" and SEGMENT_TYPE == "fmp4":
            args.extend(["-tag:v", "hvc1"])
        if branding and seek:
            # Align the output start after an accurate input seek.
            args.extend(["-ss", "0"])
        if "test_seconds" in CFG:
            args.extend(["-t", str(duration - seek)])
        args.extend(["-avoid_negative_ts", "make_zero", "-f", "hls", "-hls_segment_type", SEGMENT_TYPE, "-hls_time", "4", "-hls_list_size", "64", "-hls_flags", "omit_endlist+independent_segments+temp_file"])
        if SEGMENT_TYPE == "fmp4":
            args.extend(["-hls_fmp4_init_filename", "init.mp4"])
        extension = ".ts" if SEGMENT_TYPE == "mpegts" else ".m4s"
        args.extend(["-hls_segment_filename", str(run / ("seg-%08d" + extension)), "-y", str(run / "stream.m3u8")])
        logging.info("Playing media (%s): %s", "H.264 with branding/formatting" if branding else "packet copy", source.name)
        started_at = time.time() - seek
        child = subprocess.Popen(args, stdout=subprocess.PIPE, env=ENV)
        playing = True
        os.set_blocking(child.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        buffer = ""
        position = seek
        first = True
        last_cleanup = 0.0
        requested = None
        policy_version = queue_policy(CHANNEL_ID).get('version', 0)
        while child.poll() is None and not stopping:
            for key, _ in selector.select(timeout=1):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                buffer += data.decode(errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.startswith("out_time_us="):
                        try:
                            position = seek + max(0, int(line.split("=", 1)[1])) / 1_000_000
                        except ValueError:
                            pass
            first = publish(run, first)
            output_video = dict(info['video'])
            if branding:
                output_video['codec_name'] = 'h264'
                output_video['width'] = branding['frame_width']
                output_video['height'] = branding['frame_height']
                output_video['sample_aspect_ratio'] = '1:1'
                output_video.pop('active_picture_crop', None)
            state = {"channel": CFG["name"], "source": str(source), "position": position, "duration": info["duration"], "started_at": started_at, "updated_at": time.time(), "video": output_video, "source_video": info['video'], "audio": output_audio(info["audio"]), "source_audio": info["audio"], "title": title(source), "copyright": "Copyright Cyrius IPTV", "mode": "live_logo" if branding else "streamcopy", "container": SEGMENT_TYPE, "library_mode": "thematic_mix" if CFG.get('media_roots') else "folder"}
            atomic_json(state_path, state)
            atomic_json(PUBLIC / "now.json", {k: v for k, v in state.items() if k != "source"})
            requested = requested_transition(files, position)
            if requested:
                child.terminate()
                break
            version = queue_policy(CHANNEL_ID).get('version', 0)
            if version != policy_version:
                files = library()
                policy_version = version
            if time.time() - last_cleanup > 30:
                cleanup(run)
                last_cleanup = time.time()
        if stopping and child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        selector.close()
        child.stdout.close()
        publish(run, first)
        playing = False
        if stopping:
            break
        if requested:
            source, seek = requested
            bridge_source = source if source not in arrange_queue(files, CHANNEL_ID) else None
            atomic_json(state_path, {'source': str(source), 'position': seek, 'updated_at': time.time()})
            continue
        if child.returncode:
            logging.warning("Media copy exited %s for %s", child.returncode, source.name)
            time.sleep(3)
        if source == bridge_source:
            bridge_source = None
        files = library()
        if not queue_policy(CHANNEL_ID).get('repeat', True) and source in files and files.index(source) == len(files) - 1:
            waiting_for_queue = True
            waiting_version = queue_policy(CHANNEL_ID).get('version', 0)
            continue
        source, seek = files[(files.index(source) + 1) % len(files)] if source in files else files[0], 0.0
        atomic_json(state_path, {"source": str(source), "position": 0, "updated_at": time.time()})
        completed_movies += 1
        if CFG.get("test_movies") and completed_movies >= CFG["test_movies"]:
            break
    except Exception:
        playing = False
        logging.exception("Copy playout will retry")
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        time.sleep(5)
        if source == bridge_source:
            bridge_source = None
        files = library()
        source, seek = files[(files.index(source) + 1) % len(files)] if source in files else files[0], 0.0
