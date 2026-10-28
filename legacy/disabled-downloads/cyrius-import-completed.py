#!/usr/bin/python3
"""Publish completed genre downloads to ffplayout without touching torrent data."""
import fcntl
import grp
import hashlib
import json
import logging
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import base64
from cyrius_auto_policy import rejection_reason

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
FAST_ONLY = "--fast-only" in sys.argv
STATE = Path("/var/lib/cyrius-media-import")
STATE.mkdir(exist_ok=True, mode=0o700)
lock = (STATE / ("fast.lock" if FAST_ONLY else "lock")).open("w")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(0)
credential_dir = Path(os.environ["CREDENTIALS_DIRECTORY"])
transmission = json.loads((credential_dir / "transmission-login").read_text())
playout = json.loads((credential_dir / "ffplayout-login").read_text())
config = json.loads(Path("/etc/cyrius/media-import.json").read_text())
cache_path = STATE / "imported.json"
cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
ffpu = pwd.getpwnam("ffpu")
media_gid = grp.getgrnam("cyrius-media").gr_gid
groups = os.getgrouplist("ffpu", ffpu.pw_gid)
env = dict(os.environ, LD_LIBRARY_PATH="/opt/cyrius/ffmpeg-7.1.5/lib")
work = Path("/srv/cyrius/media/.cyrius-import-work")
work.mkdir(exist_ok=True)
os.chown(work, ffpu.pw_uid, media_gid)
work.chmod(0o700)
headers = {"Authorization": "Basic " + base64.b64encode((transmission["username"] + ":" + transmission["password"]).encode()).decode()}
token = None


def work_for(channel):
    directory = Path(channel.get("work", str(work)))
    directory.mkdir(parents=True, exist_ok=True)
    os.chown(directory, ffpu.pw_uid, media_gid)
    directory.chmod(0o700)
    return directory


def rpc(method, arguments=None):
    body = json.dumps({"method": method, "arguments": arguments or {}}).encode()
    for _ in range(2):
        try:
            req = urllib.request.Request("http://127.0.0.1:9091/transmission/rpc", data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as response:
                data = json.load(response)
            if data["result"] != "success":
                raise RuntimeError(data["result"])
            return data["arguments"]
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            headers["X-Transmission-Session-Id"] = error.headers["X-Transmission-Session-Id"]


def api(path, data=None, method=None):
    global token
    base = "http://127.0.0.1:8787"
    if token is None:
        req = urllib.request.Request(base + "/auth/login", data=json.dumps(playout).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as response:
            token = json.load(response)["access"]
    req = urllib.request.Request(base + path, data=None if data is None else json.dumps(data).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method=method)
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read().decode()
        try:
            return json.loads(raw)
        except ValueError:
            return raw


def probe(path):
    result = subprocess.run(["/opt/cyrius/ffmpeg-7.1.5/bin/ffprobe", "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,codec_name,channels,width,height,pix_fmt:stream_disposition=default:stream_tags=language,title", "-of", "json", str(path)], capture_output=True, text=True, timeout=25, env=env, user=ffpu.pw_uid, group=ffpu.pw_gid, extra_groups=groups)
    if result.returncode:
        raise RuntimeError("Media probe failed")
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    video = [s for s in data["streams"] if s["codec_type"] == "video"]
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    if duration <= 0 or not video or not audio:
        raise RuntimeError("Missing playable video/audio")
    defaults = [a for a in audio if a.get("disposition", {}).get("default")]
    selected = max(defaults or audio, key=lambda a: a.get("channels", 0))
    russian = [a for a in audio if a.get("tags", {}).get("language", "").lower() in ("rus", "ru")]
    needs_remux = bool(russian) and selected.get("tags", {}).get("language", "").lower() not in ("rus", "ru")
    chosen = next((a for a in russian if "dub" in a.get("tags", {}).get("title", "").lower()), russian[0] if russian else selected)
    return data, needs_remux, chosen["index"]


def save_cache():
    with (STATE / "cache.lock").open("w") as cache_lock:
        fcntl.flock(cache_lock, fcntl.LOCK_EX)
        current = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        current.update(cache)
        temp = cache_path.with_name("imported." + str(os.getpid()) + ".tmp")
        temp.write_text(json.dumps(current, ensure_ascii=False, indent=2))
        temp.chmod(0o600)
        temp.replace(cache_path)


def activate(channel):
    media = Path(channel["media"])
    videos = sorted(p for p in media.rglob("*") if p.suffix.lower() in (".mp4", ".mkv", ".avi", ".webm") and p.name != "00-standby.mp4")
    if not videos:
        return
    cid = channel["id"]
    card = media / "00-standby.mp4"
    if card.exists():
        card.rename(work_for(channel) / ("standby-" + str(cid) + "-" + str(time.time_ns()) + ".mp4"))
        logging.info("%s: standby archived", channel["name"])
    copy_mode = Path("/etc/cyrius/copy-channels.json")
    if copy_mode.exists() and json.loads(copy_mode.read_text()).get("enabled"):
        return
    cfg = api(f"/api/playout/config/{cid}")
    if cfg["storage"]["filler"] == "00-standby.mp4":
        cfg["storage"]["filler"] = videos[0].relative_to(media).as_posix()
        api(f"/api/playout/config/{cid}", cfg, "PUT")
    state = api(f"/api/control/{cid}/media/current")
    source = (state.get("media") or {}).get("source", "")
    ch = api(f"/api/channel/{cid}")
    if ch["active"] and (not source or Path(source).name == "00-standby.mp4"):
        api(f"/api/control/{cid}/process", {"command": "restart"}, "POST")
        logging.info("%s: movie loop activated", channel["name"])


rows = rpc("torrent-get", {"fields": ["id", "hashString", "status", "percentDone", "error", "downloadDir", "files", "labels"]})["torrents"]
deferred = []
for channel in config["channels"]:
    required_mount = channel.get("requires_mount")
    if required_mount and not Path(required_mount).is_mount():
        logging.warning("Storage not mounted; skipped: %s", required_mount)
        continue
    download_root = Path(channel["downloads"]).resolve()
    media = Path(channel["media"]).resolve()
    for torrent in rows:
        directory = Path(torrent["downloadDir"]).resolve()
        if not directory.is_relative_to(download_root) or torrent["percentDone"] < 1 or torrent["error"] or torrent["status"] not in (0, 5, 6):
            continue
        for entry in torrent["files"]:
            relative = Path(entry["name"])
            if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() not in (".mp4", ".mkv", ".avi", ".webm"):
                continue
            if any(part.lower() in ("sample", "samples") for part in relative.parts) or relative.stem.lower().startswith("sample"):
                continue
            source = (directory / relative).resolve()
            if not source.is_relative_to(download_root) or entry["bytesCompleted"] != entry["length"] or not source.is_file() or source.stat().st_size != entry["length"]:
                continue
            target = media / source.relative_to(download_root)
            key = torrent["hashString"] + ":" + str(relative)
            if key in cache and not target.exists():
                # Respect a later removal or rename made by the channel operator.
                continue
            if target.exists():
                if key in cache or os.path.samefile(source, target):
                    cache.setdefault(key, {"path": str(target), "mode": "linked"})
                    continue
                logging.warning("Destination already exists; left untouched: %s", target.name)
                continue
            try:
                data, remux, audio_index = probe(source)
                rejected = rejection_reason(data, torrent.get('labels', []), audio_index)
                if rejected:
                    rejected_path = STATE / 'auto-rejected.json'
                    rejected_files = json.loads(rejected_path.read_text()) if rejected_path.exists() else {}
                    if key not in rejected_files:
                        logging.warning('%s: %s', source.name, rejected)
                        rejected_files[key] = {'filename': source.name, 'reason': rejected, 'time': time.time()}
                        temporary = rejected_path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(rejected_files, ensure_ascii=False, indent=2))
                        temporary.chmod(0o600)
                        temporary.replace(rejected_path)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                parent = target.parent
                while parent != media:
                    os.chown(parent, ffpu.pw_uid, media_gid)
                    parent.chmod(0o2750)
                    parent = parent.parent
                if remux:
                    deferred.append((channel, key, source, target, audio_index))
                    logging.info("Russian audio preparation queued: %s", source.name)
                    continue
                os.link(source, target)
                cache[key] = {"path": str(target), "mode": "linked"}
                logging.info("Imported into %s: %s", channel["name"], target.name)
            except Exception as error:
                logging.warning("Import pending for %s (%s)", source.name, type(error).__name__)
    activate(channel)
save_cache()

if FAST_ONLY:
    if deferred:
        subprocess.run(["systemctl", "start", "--no-block", "cyrius-media-import.service"], check=True)
    raise SystemExit(0)

for channel, key, source, target, audio_index in deferred:
    channel_work = work_for(channel)
    temp = channel_work / (hashlib.sha256(key.encode()).hexdigest() + ".mkv")
    try:
        logging.info("Preparing Russian audio: %s", source.name)
        if shutil.disk_usage(channel_work).free < source.stat().st_size + 50 * 1024**3:
            raise RuntimeError("Insufficient free space for safe remux")
        if temp.exists():
            temp.unlink()
        result = subprocess.run(["/opt/cyrius/ffmpeg-7.1.5/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:" + str(audio_index), "-c", "copy", "-sn", "-dn", "-disposition:a:0", "default", "-metadata:s:a:0", "language=rus", "-y", str(temp)], env=env, timeout=3600, user=ffpu.pw_uid, group=ffpu.pw_gid, extra_groups=groups)
        if result.returncode:
            raise RuntimeError("Remux failed")
        probe(temp)
        os.chown(temp, ffpu.pw_uid, media_gid)
        temp.chmod(0o640)
        if target.exists():
            raise RuntimeError("Destination appeared during preparation")
        temp.replace(target)
        cache[key] = {"path": str(target), "mode": "russian-audio-copy"}
        save_cache()
        logging.info("Imported with Russian audio into %s: %s", channel["name"], target.name)
        activate(channel)
    except Exception as error:
        logging.warning("Russian audio preparation pending for %s (%s)", source.name, type(error).__name__)
        if temp.exists():
            temp.unlink()
