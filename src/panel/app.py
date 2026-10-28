"""CYRIUS control panel. Private WSGI service; public access goes through HTTPS nginx."""
import base64
from collections import defaultdict
from contextlib import contextmanager
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import random
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urljoin
import urllib.request
import urllib.error

from flask import Flask, jsonify, request, session, send_file, abort, Response
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

sys.path.insert(0, '/usr/local/libexec')
from cyrius_queue import atomic, policy, arrange, read, ROOT as CONTROL

HOME = Path(os.environ.get('PANEL_HOME', '/opt/cyrius-panel'))
STATE = Path(os.environ.get('PANEL_STATE', '/var/lib/cyrius-panel'))
CONFIG = Path(os.environ.get('PANEL_CONFIG', '/etc/cyrius/copy-channels.json'))
LIVE = Path(os.environ.get('PANEL_LIVE_STATE', '/var/lib/cyrius-copy'))
ALLOWED_HOSTS = set(os.environ.get('PANEL_ALLOWED_HOSTS', '127.0.0.1,localhost').split(','))
ASSETS = Path('/srv/cyrius/assets/logos')
PANEL_ASSETS = Path('/etc/cyrius/panel-logos')
DOWNLOADS_DISABLED = True
TORRENT_BLOCK = Path('/etc/cyrius/torrent-disabled.json')
TORRENT_BLOCK_MESSAGE = 'Торрент-загрузки и раздачи отключены. Этот способ получения файлов в переносимой версии не устанавливается.'
SLUGS = {1:'cyrius-boeviki',2:'cyrius-comedy',3:'cyrius-kids-hd',4:'cyrius-horror',5:'cyrius-cinema-hd',6:'cyrius-retro-hd',7:'cyrius-romance-hd'}
GENRES = {1:'Boevik',2:'Comedy',3:'KidsHD',4:'Horror',6:'Retro',7:'Romance'}
LOCK = threading.RLock()
RPC_LOCK = threading.Lock()
CACHE = {}
FAILURES = defaultdict(list)
app = Flask(__name__, static_folder=str(HOME/'static'), static_url_path='/static')
app.config.update(SECRET_KEY=(STATE/'secret.key').read_text().strip(), MAX_CONTENT_LENGTH=6_000_000,
                  SESSION_COOKIE_NAME='cyrius_session', SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=8*3600)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)


class Cookies(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):
        return request.is_secure


app.session_interface = Cookies()


@contextmanager
def db():
    c = sqlite3.connect(STATE/'panel.db', timeout=5)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def configs():
    return json.loads(CONFIG.read_text())['channels']


def channel(cid):
    return next((c for c in configs() if c['id'] == cid), None) or abort(404)


def audit(action, detail):
    with db() as c:
        c.execute('insert into audit(at,user,ip,action,detail) values(?,?,?,?,?)',
                  (time.time(),session.get('user','system'),request.headers.get('X-Real-IP',request.remote_addr),action,str(detail)[:500]))


def admin(data):
    result = subprocess.run(['/usr/bin/sudo','-n','/usr/local/sbin/cyrius-panel-admin'],
                            input=json.dumps(data),capture_output=True,text=True,timeout=30)
    try: value = json.loads(result.stdout)
    except ValueError: raise ValueError('Не удалось сохранить настройки')
    if result.returncode: raise ValueError(value.get('error','Ошибка сохранения'))
    return value


def rpc(method, args=None):
    if DOWNLOADS_DISABLED:
        raise ValueError(TORRENT_BLOCK_MESSAGE)
    with RPC_LOCK:
        credential = Path(os.environ['CREDENTIALS_DIRECTORY'])/'transmission-login'
        c = json.loads(credential.read_text())
        headers = {'Authorization':'Basic '+base64.b64encode((c['username']+':'+c['password']).encode()).decode()}
        if CACHE.get('rpc_session'): headers['X-Transmission-Session-Id'] = CACHE['rpc_session']
        for _ in range(3):
            req = urllib.request.Request('http://127.0.0.1:9091/transmission/rpc',
                data=json.dumps({'method':method,'arguments':args or {}}).encode(),headers=headers)
            try:
                with urllib.request.urlopen(req,timeout=20) as response: value=json.load(response)
                if value['result'] != 'success': raise ValueError('Transmission отклонил запрос')
                return value['arguments']
            except urllib.error.HTTPError as e:
                if e.code != 409: raise
                CACHE['rpc_session'] = e.headers['X-Transmission-Session-Id']
                headers['X-Transmission-Session-Id'] = CACHE['rpc_session']
        raise ValueError('Transmission пока не отвечает')


def torrents(force=False):
    if not force and time.time()-CACHE.get('torrents_at',0)<12: return CACHE.get('torrents',[])
    rows=rpc('torrent-get',{'fields':['id','name','hashString','status','percentDone','totalSize','leftUntilDone',
           'rateDownload','rateUpload','eta','error','errorString','labels','downloadDir','metadataPercentComplete']})['torrents']
    CACHE.update(torrents=rows,torrents_at=time.time(),rpc_error=None)
    return rows


def title_for(path, fallback=''):
    titles=read(CONFIG.parent/'movie-titles-ru.json').get('titles',{})
    return titles.get(path.name) or fallback or re.sub(r'[._]+',' ',path.stem)


def library(force=False):
    with LOCK:
        if not force and time.time()-CACHE.get('library_at',0)<30: return CACHE['library']
        infos={}; catalog_titles={}
        extra=read(STATE/'media-info.json')
        for c in configs():
            for entry in read(LIVE/str(c['id'])/'probe-cache.json',[]):
                infos[entry['source']]=entry['info']
            for entry in read(LIVE/str(c['id'])/'catalog.json').get('files',[]):
                catalog_titles[entry['source']]=entry['title']
        result={}
        excluded=set(read(CONFIG.parent/'media-exclusions.json').get('filenames',[]))
        for c in configs():
            if c['id']==5: continue
            root=Path(c['media'])
            for p in root.rglob('*'):
                if p.name in excluded: continue
                if p.suffix.lower() not in ['.mkv','.mp4','.avi','.webm','.mov'] or p.name in ['00-standby.mp4','cyrius-standby.mp4']: continue
                if any(v.startswith('.') for v in p.relative_to(root).parts) or not p.is_file() or p.is_symlink(): continue
                key=hashlib.sha256(str(p).encode()).hexdigest()[:24]
                st=p.stat();info=infos.get(str(p),{})
                cached=extra.get(key,{})
                if cached.get('size')==st.st_size and cached.get('mtime')==st.st_mtime_ns:info=cached['info']
                video=info.get('video',{});audio=info.get('audio') or {}
                result[key]={'id':key,'path':str(p),'filename':p.name,'title':title_for(p,catalog_titles.get(str(p),'')),
                    'channel':c['id'],'bytes':st.st_size,'duration':info.get('duration'),
                    'width':video.get('width'),'height':video.get('height'),'codec':video.get('codec_name'),
                    'fps':video.get('r_frame_rate'),'audio_language':audio.get('tags',{}).get('language','unknown')}
        # Cinema's bridge may still reference its old library. Expose it for current status only.
        CACHE.update(library=result,library_at=time.time())
        return result


def public_movie(movie): return {k:v for k,v in movie.items() if k!='path'}


def pool(cid):
    ch=channel(cid)
    roots=[Path(p) for p in ch.get('media_roots',[ch['media']])]
    rows=[m for m in library().values() if any(Path(m['path']).is_relative_to(root) for root in roots)]
    if ch.get('exclude_series'): rows=[m for m in rows if not re.search(r'\bs\d{1,2}e\d{1,3}(?!\d)|\b\d{1,2}[xх]\d{1,3}\b',m['filename'],re.I)]
    if ch.get('deduplicate_titles'):
        dedup={}
        for m in rows:
            k=re.sub(r'[\W_]+','',m['title'].casefold().replace('ё','е')); rank=(m.get('width') or 0)*(m.get('height') or 0)
            if k not in dedup or rank>(dedup[k].get('width') or 0)*(dedup[k].get('height') or 0):dedup[k]=m
        rows=list(dedup.values())
    return sorted(rows, key=lambda row: row['path'])


def enqueue_control(cid, action, source=None):
    if action not in ['next','restart','play','reload']: raise ValueError('Неизвестная команда')
    with LOCK:
        previous=read(CONTROL/str(cid)/'command.json');ack=read(CONTROL/str(cid)/'ack.json')
        if previous.get('id') and previous.get('id')!=ack.get('id') and time.time()-previous.get('at',0)<20:
            raise ValueError('Предыдущая команда ещё выполняется')
        value={'id':secrets.token_hex(12),'action':action,'at':time.time()}
        if source:value['source']=source
        atomic(CONTROL/str(cid)/'command.json',value)
        audit('control',{'channel':cid,'action':action})
        return {'accepted':True,'command':value['id']}


@app.before_request
def gate():
    if request.host.split(':')[0] not in ALLOWED_HOSTS: abort(400)
    if (request.path.startswith('/api/') or request.path.startswith('/watch/')) and request.path not in ['/api/login','/api/health']:
        if not session.get('user'): return jsonify(error='Требуется вход'),401
        with db() as c:r=c.execute('select version from users where username=?',(session['user'],)).fetchone()
        if not r or r['version']!=session.get('version'):session.clear();return jsonify(error='Войдите повторно'),401
        if request.method not in ['GET','HEAD','OPTIONS'] and not hmac.compare_digest(str(request.headers.get('X-CSRF-Token','')),str(session.get('csrf',''))):
            return jsonify(error='Обновите страницу и повторите действие'),403
    if request.method not in ['GET','HEAD','OPTIONS']:
        if os.environ.get('PANEL_READONLY')=='1' and request.path not in ['/api/login','/api/logout']:
            return jsonify(error='Предпросмотр: управление пока отключено'),403
        origin=request.headers.get('Origin')
        if origin and origin!=request.host_url.rstrip('/'): return jsonify(error='Недопустимый источник запроса'),403
        if request.path.startswith('/api/downloads/') and DOWNLOADS_DISABLED:
            return jsonify(error=TORRENT_BLOCK_MESSAGE,blocked=DOWNLOADS_DISABLED),403


@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options']='nosniff';response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; worker-src blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.path.startswith('/api/'):response.headers['Cache-Control']='no-store'
    return response


@app.errorhandler(ValueError)
def bad(e): return jsonify(error=str(e)),400


@app.errorhandler(Exception)
def unexpected(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e,HTTPException):return jsonify(error=e.description),e.code
    logging.exception('Panel request failed')
    return jsonify(error='Операция пока недоступна. Повторите через несколько секунд.'),503


@app.get('/')
def index():return send_file(HOME/'static/index.html')


@app.get('/api/health')
def health():return jsonify(ok=True,service='cyrius-panel')


@app.post('/api/login')
def login():
    ip=request.headers.get('X-Real-IP',request.remote_addr); now=time.time()
    with LOCK:
        FAILURES[ip]=[t for t in FAILURES[ip] if now-t<900]
        if len(FAILURES[ip])>=5:return jsonify(error='Слишком много попыток. Повторите через 15 минут.'),429
    data=request.get_json() or {}; username=str(data.get('username',''))[:80];password=str(data.get('password',''))[:256]
    with db() as c:r=c.execute('select * from users where username=?',(username,)).fetchone()
    if not r or not check_password_hash(r['password_hash'],password):
        with LOCK:FAILURES[ip].append(now)
        return jsonify(error='Неверный логин или пароль'),401
    session.clear();session.update(user=username,version=r['version'],csrf=secrets.token_hex(24));session.permanent=True
    with LOCK:FAILURES.pop(ip,None)
    audit('login','Вход в панель')
    return jsonify(user=username,csrf=session['csrf'])


@app.get('/api/session')
def who():return jsonify(user=session['user'],csrf=session['csrf'])


@app.get('/api/auth/check')
def auth_check():return '',204


@app.post('/api/logout')
def logout():session.clear();return jsonify(ok=True)


@app.post('/api/password')
def password():
    data=request.get_json();new=str(data.get('new',''))
    if not 12<=len(new)<=200:raise ValueError('Новый пароль должен содержать от 12 до 200 символов')
    with db() as c:
        r=c.execute('select * from users where username=?',(session['user'],)).fetchone()
        if not check_password_hash(r['password_hash'],str(data.get('old',''))):raise ValueError('Текущий пароль неверен')
        version=r['version']+1;c.execute('update users set password_hash=?,version=? where username=?',(generate_password_hash(new),version,session['user']))
    session['version']=version;audit('password','Пароль изменён');return jsonify(ok=True)


@app.get('/api/overview')
def overview():
    lib=library();by_path={m['path']:m for m in lib.values()};rows=[]
    for ch in configs():
        cid=ch['id'];x=read(LIVE/str(cid)/'state.json');cat=read(LIVE/str(cid)/'catalog.json').get('files',[])
        idx=next((i for i,v in enumerate(cat) if v['source']==x.get('source')),None)
        nxt=cat[(idx+1)%len(cat)] if idx is not None and cat and (policy(cid).get('repeat',True) or idx+1<len(cat)) else None
        manifest=Path(ch['public'])/'index.m3u8'
        hls_age=time.time()-manifest.stat().st_mtime if manifest.exists() else 9999
        status='ended' if x.get('status')=='queue_ended' else ('online' if time.time()-x.get('updated_at',0)<15 and hls_age<35 else 'offline')
        rows.append({'id':cid,'name':ch['name'],'slug':ch.get('slug',SLUGS[cid]),'status':status,
            'title':title_for(Path(x.get('source','')),x.get('title','Ожидание эфира')),'position':x.get('position',0),
            'duration':x.get('duration',0),'next':title_for(Path(nxt['source']),nxt['title']) if nxt else None,
            'movie_id':by_path.get(x.get('source'),{}).get('id'),'count':len(cat),'logo':'/icons/'+str(cid)+'.png',
            'video':x.get('video',{}),'audio':(x.get('audio') or {}).get('tags',{}).get('language'),
            'policy':{k:v for k,v in policy(cid).items() if k in ['mode','repeat','use_all','version']},
            'command':read(CONTROL/str(cid)/'ack.json')})
    return jsonify(channels=rows,system=CACHE.get('metrics',{}),downloads=dict(CACHE.get('download_summary',{}),updated_at=CACHE.get('torrents_at'),blocked=DOWNLOADS_DISABLED),at=time.time(),timezone=os.environ.get('CYRIUS_TIMEZONE','Asia/Baku'),read_only=os.environ.get('PANEL_READONLY')=='1')


@app.get('/api/library')
def get_library():return jsonify(movies=[public_movie(m) for m in library().values()])


@app.get('/api/channels/<int:cid>/queue')
def get_queue(cid):
    rows=pool(cid);by_path={m['path']:m for m in rows};ordered=arrange([Path(m['path']) for m in rows],cid)
    return jsonify(policy=policy(cid),items=[public_movie(by_path[str(p)]) for p in ordered],available=[public_movie(m) for m in rows],current=read(LIVE/str(cid)/'state.json').get('title'))


@app.post('/api/channels/<int:cid>/queue')
def save_queue(cid):
    data=request.get_json();rows={m['id']:m for m in pool(cid)};ids=data.get('items',[])
    if not isinstance(ids,list) or not ids or len(ids)>1000 or len(set(ids))!=len(ids) or any(k not in rows for k in ids):raise ValueError('Выберите хотя бы один фильм из медиатеки канала')
    if data.get('mode') not in ['sequential','random'] or type(data.get('repeat')) is not bool:raise ValueError('Некорректный режим')
    value={'mode':data['mode'],'repeat':data['repeat'],'use_all':bool(data.get('use_all',False)),
           'items':[rows[k]['path'] for k in ids],'seed':str(data.get('seed') or secrets.token_hex(12))[:100],'version':time.time_ns()}
    previous=policy(cid)
    if value['mode']=='random':
        if previous.get('mode')!='random' or previous.get('seed')!=value['seed'] or not previous.get('frozen_order'):
            random.Random(value['seed']).shuffle(value['items'])
        value['frozen_order']=True
    with LOCK:atomic(CONTROL/str(cid)/'queue.json',value)
    audit('queue',{'channel':cid,'count':len(ids),'mode':value['mode']});return jsonify(saved=True,epg_update_seconds=60)


@app.post('/api/channels/<int:cid>/control')
def control(cid):
    channel(cid);data=request.get_json();source=None
    if data.get('action')=='play':
        rows={m['id']:m for m in pool(cid)}
        if data.get('movie') not in rows:raise ValueError('Фильм отсутствует в медиатеке канала')
        source=rows[data['movie']]['path']
    return jsonify(enqueue_control(cid,data.get('action'),source))


@app.post('/api/library/<movie>/title')
def rename(movie):
    row=library().get(movie)
    if not row:abort(404)
    result=admin({'action':'title','channel':row['channel'],'filename':row['filename'],'title':request.get_json().get('title','')})
    CACHE.pop('library_at',None);audit('title',row['filename']);return jsonify(result)


@app.post('/api/library/<movie>/inspect')
def inspect_movie(movie):
    row=library().get(movie)
    if not row:abort(404)
    args=['/usr/bin/nice','-n','19',os.environ.get('CYRIUS_FFPROBE','/usr/bin/ffprobe'),'-v','error','-show_entries',
          'format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate:stream_tags=language,title','-of','json',row['path']]
    result=subprocess.run(args,capture_output=True,text=True,timeout=20,env=dict(os.environ))
    if result.returncode:raise ValueError('Не удалось прочитать параметры видео')
    data=json.loads(result.stdout);audios=[s for s in data['streams'] if s['codec_type']=='audio']
    info={'duration':float(data['format']['duration']),'video':next(s for s in data['streams'] if s['codec_type']=='video'),
          'audio':next((s for s in audios if s.get('tags',{}).get('language') in ['rus','ru']),audios[0] if audios else None)}
    st=Path(row['path']).stat()
    with LOCK:
        cache=read(STATE/'media-info.json');cache[movie]={'size':st.st_size,'mtime':st.st_mtime_ns,'info':info};atomic(STATE/'media-info.json',cache)
        CACHE.pop('library_at',None)
    return jsonify(ok=True)


@app.post('/api/library/<movie>/assign')
def assign(movie):
    row=library().get(movie);cid=request.get_json().get('channel')
    if not row or cid not in GENRES:raise ValueError('Выберите тематический канал')
    source=Path(row['path']);disk2=Path('/srv/cyrius/disk2')
    media_root=Path(channel(row['channel'])['media'])
    if source.is_relative_to(media_root/'Disk2'):
        physical=disk2/'media'/GENRES[row['channel']]/source.relative_to(media_root/'Disk2')
        if physical.is_file() and physical.samefile(source):source=physical
    root=(disk2/'media'/GENRES[cid]) if disk2.is_mount() and source.stat().st_dev==disk2.stat().st_dev else Path('/srv/cyrius/media')/GENRES[cid]
    target=root/source.name
    if target.exists():
        if not target.samefile(source):raise ValueError('В канале уже есть другой файл с таким именем')
    else:os.link(source,target)
    for affected in {cid,5}:
        value=policy(affected);value['version']=time.time_ns();atomic(CONTROL/str(affected)/'queue.json',value)
    CACHE.pop('library_at',None);audit('assign',{'movie':row['title'],'channel':cid});return jsonify(saved=True)


@app.get('/api/channels/<int:cid>/branding')
def get_branding(cid):
    c=channel(cid);return jsonify(logo={k:v for k,v in c['logo'].items() if k not in ['path']},title=c['title_overlay'])


@app.post('/api/channels/<int:cid>/branding')
def set_branding(cid):
    channel(cid);data=request.get_json();result=admin({'action':'branding','channel':cid,'values':data['values'],'png':data.get('png')})
    audit('branding',{'channel':cid})
    if data.get('apply_now'):result['control']=enqueue_control(cid,'reload')
    return jsonify(result)


@app.get('/icons/<name>')
def icons(name):
    if re.fullmatch(r'[1-7]\.png',name):p=Path(channel(int(name[0]))['logo']['path']).with_suffix('.png')
    elif re.fullmatch(r'panel-[1-7]-[0-9]+\.png',name):p=PANEL_ASSETS/name
    else:abort(404)
    if not p.is_file() or not any(p.is_relative_to(root) for root in [ASSETS,PANEL_ASSETS]):abort(404)
    return send_file(p,mimetype='image/png',max_age=60)


@app.get('/watch/<int:cid>/<path:fragment>')
def watch(cid, fragment):
    c=channel(cid)
    if not re.fullmatch(r'index\.m3u8|run-[0-9]+-[0-9]+/(?:seg-[0-9]+\.(?:ts|m4s)|init\.mp4)',fragment):abort(404)
    root=Path(c['public']).resolve();path=(root/fragment).resolve()
    if not path.is_relative_to(root) or not path.is_file():abort(404)
    if os.environ.get('PANEL_ACCEL')=='1':
        response=Response(status=200,mimetype='application/vnd.apple.mpegurl' if fragment.endswith('.m3u8') else 'video/mp2t');response.headers['X-Accel-Redirect']=f'/_streams/{cid}/{fragment}'
    else:response=send_file(path,mimetype='application/vnd.apple.mpegurl' if fragment.endswith('.m3u8') else 'video/mp2t',conditional=True)
    response.headers['Cache-Control']='no-cache'
    return response


@app.get('/api/programme')
def programme():return jsonify(read('/srv/cyrius/public-copy/programme.json'))


@app.get('/api/system')
def system():
    with db() as c:history=[dict(r) for r in c.execute('select at,user,action,detail from audit order by id desc limit 40')]
    return jsonify(metrics=CACHE.get('metrics',{}),history=history,rpc_error=CACHE.get('rpc_error'))


@app.get('/api/downloads')
def downloads():
    rows=[]
    for t in CACHE.get('torrents',[]):
        row={k:v for k,v in t.items() if k not in ['downloadDir','hashString']}
        row['channel']=next((cid for cid,g in {**GENRES,5:'CinemaHD'}.items() if Path(t['downloadDir']).name==g),None)
        rows.append(row)
    blocked=DOWNLOADS_DISABLED
    error=TORRENT_BLOCK_MESSAGE if blocked else CACHE.get('rpc_error')
    if not error and time.time()-CACHE.get('torrents_at',0)>60:
        error='Данные загрузок ожидают обновления. Показана последняя полученная очередь.'
    return jsonify(torrents=rows,error=error,blocked=blocked,updated_at=CACHE.get('torrents_at'),auto=CACHE.get('auto',{}))


@app.post('/api/downloads/settings')
def settings():
    result=admin({'action':'auto','values':request.get_json()});CACHE['auto']=admin({'action':'auto-read'})
    audit('auto_settings','Настройки автозагрузки изменены');return jsonify(result)


@app.post('/api/downloads/<int:tid>/control')
def torrent_control(tid):
    row=next((t for t in torrents(force=True) if t['id']==tid),None)
    if not row:abort(404)
    action=request.get_json().get('action');labels=list(row['labels'])
    if action=='pause':
        if 'cyrius-manual-paused' not in labels:labels.append('cyrius-manual-paused')
        rpc('torrent-set',{'ids':[tid],'labels':labels});rpc('torrent-stop',{'ids':[tid]})
    elif action=='start':
        limit=CACHE.get('auto',{}).get('max_torrent_bytes',12_000_000_000)
        if row['percentDone']<1 and row['totalSize']>limit:raise ValueError('Раздача превышает установленный размер')
        max_active=CACHE.get('auto',{}).get('max_active') or 2
        if row['percentDone']<1 and sum(t['percentDone']<1 and t['status'] in [1,2,3,4] for t in CACHE['torrents'] if t['id']!=tid)>=max_active:raise ValueError('Достигнут лимит одновременных загрузок. Дождитесь свободного места в очереди.')
        labels=[l for l in labels if l!='cyrius-manual-paused'];rpc('torrent-set',{'ids':[tid],'labels':labels});rpc('torrent-start',{'ids':[tid]})
    else:raise ValueError('Неизвестное действие')
    CACHE['torrents_at']=0;audit('torrent_'+action,row['name']);return jsonify(saved=True)


def public_fetch(url):
    """Bounded public HTTP fetch, pinned to a validated address on every redirect."""
    for _ in range(5):
        parsed=urlsplit(url)
        if parsed.scheme not in ['http','https'] or parsed.username or parsed.password or not parsed.hostname or parsed.port not in [None,80,443]:raise ValueError('Нужна публичная HTTP/HTTPS-ссылка на торрент')
        port=parsed.port or (443 if parsed.scheme=='https' else 80)
        addresses=socket.getaddrinfo(parsed.hostname,port,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise ValueError('Локальные адреса для загрузки запрещены')
        conn=http.client.HTTPConnection(parsed.hostname,port,timeout=12)
        sock=socket.create_connection((addresses[0][4][0],port),timeout=12)
        if parsed.scheme=='https':sock=ssl.create_default_context().wrap_socket(sock,server_hostname=parsed.hostname)
        conn.sock=sock
        try:
            path=parsed.path or '/'
            if parsed.query:path+='?'+parsed.query
            conn.request('GET',path,headers={'Host':parsed.netloc,'User-Agent':'CYRIUS-Control/1.0','Accept-Encoding':'identity'})
            response=conn.getresponse()
            if response.status in [301,302,303,307,308]:url=urljoin(url,response.getheader('Location',''));continue
            if response.status!=200:raise ValueError('Источник не отдал торрент-файл')
            raw=response.read(4_000_001)
            if len(raw)>4_000_000:raise ValueError('Торрент-файл больше 4 МБ')
            return raw
        finally:conn.close()
    raise ValueError('Слишком много перенаправлений')


@app.post('/api/downloads/add')
def torrent_add():
    data=request.get_json();cid=data.get('channel')
    if cid not in GENRES:raise ValueError('Выберите тематический канал; Cinema HD получает фильмы из них')
    url=str(data.get('url','')).strip();raw=None
    limit=CACHE.get('auto',{}).get('max_torrent_bytes',12_000_000_000)
    args={'download-dir':'/srv/cyrius/disk2/downloads/complete/'+GENRES[cid], 'paused':True,
          'labels':['cyrius-auto','cyrius-panel',GENRES[cid],channel(cid)['name']]}
    if data.get('torrent'):raw=base64.b64decode(data['torrent'],validate=True)
    elif url.startswith('magnet:'):
        if len(url)>8000 or not re.search(r'(?:[?&])xt=urn:btih:(?:[a-fA-F0-9]{40}|[a-zA-Z2-7]{32})(?:&|$)',url):raise ValueError('Некорректная magnet-ссылка')
        args['filename']=url
    else:raw=public_fetch(url)
    if raw is not None:
        import importlib.util
        spec=importlib.util.spec_from_file_location('auto_validate','/usr/local/libexec/cyrius-auto-download.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        try:_,size,_=m.torrent_details(raw,{'max_torrent_gib':limit/1024**3,'allow_private':True})
        except Exception:raise ValueError('Нужен торрент одного фильма до '+str(round(limit/1e9,1))+' ГБ, без программ и небезопасных путей')
        metrics=CACHE.get('metrics',{});pending=sum(t['leftUntilDone'] for t in CACHE.get('torrents',[]))
        if not metrics.get('disks'):raise ValueError('Ожидаем данные о свободном месте. Повторите через несколько секунд.')
        if any(d['free']-pending-size<100*1024**3 for d in metrics.get('disks',[])):raise ValueError('Недостаточно свободного места с учётом очереди и резерва')
        args['metainfo']=base64.b64encode(raw).decode()
    if raw is None:args['labels'].append('cyrius-panel-metadata')
    result=rpc('torrent-add',args);CACHE['torrents_at']=0
    if raw is None and result.get('torrent-added'):
        rpc('torrent-set',{'ids':[result['torrent-added']['id']],'downloadLimited':True,'downloadLimit':64})
    audit('torrent_add',{'channel':cid,'magnet':raw is None})
    return jsonify(added='torrent-added' in result,duplicate='torrent-duplicate' in result,message='Добавлено в очередь. Для magnet размер проверится после получения метаданных.')


def metrics_monitor():
    prev=None
    while True:
        try:
            values=list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:]));cur=(sum(values),values[3]+values[4])
            cpu=100*(1-(cur[1]-prev[1])/(cur[0]-prev[0])) if prev and cur[0]>prev[0] else 0;prev=cur
            mem={k:int(v.split()[0])*1024 for k,v in (ln.split(':',1) for ln in Path('/proc/meminfo').read_text().splitlines())}
            disks=[];seen_devices=set()
            for path,name in [('/','Диск 1'),('/srv/cyrius/disk2','Диск 2')]:
                if not Path(path).exists():continue
                device=Path(path).stat().st_dev
                if device in seen_devices:continue
                seen_devices.add(device)
                d=shutil.disk_usage(path);disks.append({'name':name,'total':d.total,'free':d.free,'used':d.used,'reserve':100*1024**3})
            CACHE['metrics']={'cpu':round(cpu,1),'cores':os.cpu_count(),'memory_total':mem['MemTotal'],'memory_used':mem['MemTotal']-mem['MemAvailable'],
                              'memory_cache':mem['Cached'],'disks':disks,'load':os.getloadavg()[0],'at':time.time()}
        except Exception:logging.exception('Metrics refresh failed')
        time.sleep(2)


def remote_monitor():
    last_auto=0
    while True:
        if DOWNLOADS_DISABLED:
            snapshot=read(STATE/'torrents-stopped.json')
            CACHE.update(torrents=snapshot.get('torrents',[]),torrents_at=snapshot.get('captured_at',0),rpc_error=None,
                         download_summary={'active':0,'pending':0,'rate':0,'remaining':0,'blocked':True})
            try:CACHE['auto']=admin({'action':'auto-read'})
            except Exception:logging.warning('Auto status unavailable')
            time.sleep(15)
            continue
        try:
            rows=torrents(force=True)
            CACHE['download_summary']={'active':sum(t['status']==4 and t['percentDone']<1 for t in rows),'pending':sum(t['percentDone']<1 for t in rows),
                'rate':sum(t['rateDownload'] for t in rows),'remaining':sum(t['leftUntilDone'] for t in rows)}
            limit=CACHE.get('auto',{}).get('max_torrent_bytes',12_000_000_000)
            for t in rows:
                if 'cyrius-panel' in t['labels'] and t['totalSize']>limit and t['percentDone']<1 and 'cyrius-blocked-size' not in t['labels']:
                    labels=[l for l in t['labels'] if l!='cyrius-auto']+['cyrius-blocked-size','cyrius-manual-paused']
                    rpc('torrent-set',{'ids':[t['id']],'labels':labels});rpc('torrent-stop',{'ids':[t['id']]})
                elif 'cyrius-panel-metadata' in t['labels'] and 0<t['totalSize']<=limit:
                    rpc('torrent-set',{'ids':[t['id']],'downloadLimited':False,'labels':[l for l in t['labels'] if l!='cyrius-panel-metadata']})
        except Exception:CACHE['rpc_error']='Transmission временно не отвечает; показаны последние полученные данные'
        if time.time()-last_auto>30:
            last_auto=time.time()
            try:CACHE['auto']=admin({'action':'auto-read'})
            except Exception:logging.warning('Auto status unavailable')
        time.sleep(15)


if os.environ.get('PANEL_NO_MONITOR')!='1':
    threading.Thread(target=metrics_monitor,daemon=True).start()
    threading.Thread(target=remote_monitor,daemon=True).start()
