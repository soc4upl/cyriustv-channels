import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/playout'))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


deploy = load('installer_tests', ROOT / 'deploy/install.py')


class Deployment(unittest.TestCase):
    def setUp(self):self.cfg = json.loads((ROOT / 'deploy/settings.example.json').read_text())

    def test_defaults_do_not_install_torrent_or_ffplayout(self):
        files = deploy.render(deploy.validate(self.cfg))
        units = '\n'.join(v for k, v in files.items() if '/systemd/' in k)
        self.assertNotIn('transmission', units);self.assertNotIn('ffplayout', units)
        self.assertNotIn('LoadCredential=', units)
        self.assertTrue(json.loads(files['etc/cyrius/torrent-disabled.json'])['disabled'])
        channels = json.loads(files['etc/cyrius/copy-channels.json'])['channels']
        self.assertEqual(len(channels), 7);self.assertEqual(len(channels[4]['media_roots']), 6)
        self.assertNotIn('seed', channels[0])
        self.assertEqual(json.loads(files['etc/cyrius/title-rules-ru.json']), {})

    def test_restricts_origin_and_protects_internal_video(self):
        files = deploy.render(self.cfg)
        self.assertIn('allow 127.0.0.1;', files['etc/nginx/sites-available/cyrius-origin'])
        self.assertIn('deny all;', files['etc/nginx/sites-available/cyrius-origin'])
        self.assertIn('internal;', files['etc/cyrius/panel-tls.conf'])
        self.assertIn('return 503;', files['etc/nginx/sites-available/cyrius-panel'])
        self.assertEqual(files['etc/sudoers.d/cyrius-panel'], 'ffpu ALL=(root) NOPASSWD: /usr/local/sbin/cyrius-panel-admin ""\n')

    def test_two_disk_paths_have_bind_mounts(self):
        self.cfg['secondary_storage'] = True
        files = deploy.render(self.cfg)
        mounts = [v for k, v in files.items() if k.endswith('.mount')]
        self.assertEqual(len(mounts), 7)
        self.assertTrue(all('Options=bind' in v for v in mounts))
        self.assertNotIn('mkfs', '\n'.join(files.values()))
        self.assertIn('/srv/cyrius/disk2/media', files['etc/systemd/system/cyrius-panel.service'])

    def test_public_outputs_readable_by_nginx_private_state_stays_private(self):
        files=deploy.render(self.cfg)
        self.assertIn('UMask=0022', files['etc/systemd/system/cyrius-copy@.service'])
        self.assertIn('UMask=0022', files['etc/systemd/system/cyrius-programme.service'])
        self.assertIn('StateDirectoryMode=0700', files['etc/systemd/system/cyrius-copy@.service'])
        self.assertIn('UMask=0027', files['etc/systemd/system/cyrius-panel.service'])

    def test_rejects_config_injection(self):
        for key, bad in [('panel_domain','example.com; touch /tmp/a'), ('panel_domain','https://panel.example.com'),
                         ('origin_base_url','http://admin:pass@example.com'), ('origin_base_url','http://example.com/path'),
                         ('origin_base_url','http://example.com\nEnvironment=BAD'), ('origin_allow',['all;']),
                         ('secondary_storage','false'), ('admin_username','admin\nroot')]:
            cfg = copy.deepcopy(self.cfg);cfg[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises((ValueError, TypeError)):
                deploy.validate(cfg)

    def test_render_is_separate_from_apply(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'rendered'
            cmd = [sys.executable, str(ROOT/'deploy/install.py'), '--config', str(ROOT/'deploy/settings.example.json'), '--render-dir', str(p)]
            subprocess.run(cmd, check=True, capture_output=True)
            self.assertTrue((p/'etc/cyrius/copy-channels.json').is_file())
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)

    def test_existing_installation_refused_before_packages(self):
        with mock.patch.object(deploy.sys, 'platform', 'linux'), mock.patch.object(deploy.os, 'geteuid', return_value=0), \
             mock.patch.object(deploy.Path, 'read_text', return_value='ID=ubuntu\nVERSION_ID="24.04"'), \
             mock.patch.object(deploy.Path, 'exists', return_value=True), mock.patch.object(deploy, 'run') as run:
            with self.assertRaises(RuntimeError):deploy.install(self.cfg)
            run.assert_not_called()


class Queue(unittest.TestCase):
    def test_order_repeat_commands_and_ack(self):
        with tempfile.TemporaryDirectory() as td:
            queue = load('queue_tests', ROOT/'src/playout/cyrius_queue.py');queue.ROOT = Path(td)
            paths = [Path('/one.mp4'),Path('/two.mp4')]
            queue.atomic(queue.ROOT/'1/queue.json', {'items':['/two.mp4'], 'use_all':False, 'repeat':False})
            self.assertEqual(queue.arrange(paths, 1), [paths[1]])
            cmd = {'id':'once','at':time.time(),'action':'next'}
            queue.atomic(queue.ROOT/'1/command.json', cmd);self.assertEqual(queue.command(1), cmd)
            queue.acknowledge(1, cmd);self.assertIsNone(queue.command(1))
            queue.atomic(queue.ROOT/'1/queue.json', {'items':[], 'use_all':True, 'mode':'random','seed':'stable'})
            self.assertEqual(queue.arrange(paths,1), queue.arrange(paths,1))


class Audio(unittest.TestCase):
    def test_stable_aac_lc_stereo_for_source_codecs(self):
        audio=load('audio_tests',ROOT/'src/playout/cyrius_audio.py')
        for codec in ['ac3','eac3','dts','mp3','aac']:
            source={'codec_name':codec,'channels':6,'tags':{'language':'rus'}}
            args=audio.audio_args(source);out=audio.output_audio(source)
            self.assertEqual(args[args.index('-c:a')+1],'aac')
            self.assertEqual(args[args.index('-profile:a')+1],'aac_low')
            self.assertEqual(args[args.index('-ar')+1],'48000')
            self.assertEqual(args[args.index('-ac')+1],'2')
            self.assertEqual(out['tags']['language'],'rus')
            self.assertEqual(source['channels'],6)
        self.assertEqual(audio.audio_args(None),['-an']);self.assertIsNone(audio.output_audio(None))


@unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow not installed')
class Branding(unittest.TestCase):
    def test_supplied_logos_font_and_native_fps_overlay(self):
        from PIL import Image
        caption=load('caption_tests',ROOT/'src/playout/cyrius_caption.py')
        branding=load('branding_tests',ROOT/'src/playout/cyrius_branding.py')
        for _,_,_,_,logo in deploy.CHANNELS:
            with Image.open(ROOT/'assets/logos'/str(logo+'.png')) as picture:
                self.assertIsNotNone(picture.convert('RGBA').getchannel('A').getbbox())
        with tempfile.TemporaryDirectory() as td:
            settings={'font':str(ROOT/'assets/fonts/Montserrat-Light.ttf'),'size':36,'x':48,'y':32,'color':'#ffffff'}
            rendered=caption.caption_asset('Проверка (2026)',settings,Path(td))
            self.assertTrue(Path(rendered['path']).read_bytes().startswith(b'P7\n'))
            args=branding.branding_args({'video_standard':{'width':1920,'height':1080,'mode':'crop'}},
                                        {'width':1280,'height':720,'r_frame_rate':'60/1'},rendered)
            self.assertEqual(args['frame_width'],1920);self.assertNotIn('-r',args['codec'])
            self.assertIn('240',args['codec'])


class RootHelper(unittest.TestCase):
    def test_no_shell_and_no_torrent_reenable(self):
        helper=load('helper_tests',ROOT/'src/panel/cyrius-panel-admin.py')
        with tempfile.TemporaryDirectory() as td:
            helper.BASE=Path(td);helper.BACKUPS=Path(td)/'backups'
            (helper.BASE/'copy-channels.json').write_text(json.dumps({'channels':[{'id':1}]}))
            with self.assertRaises(ValueError):helper.main({'action':'shell','channel':1})
            with self.assertRaises(ValueError):helper.main({'action':'auto','values':{'enabled':True}})


@unittest.skipUnless(importlib.util.find_spec('flask'), 'Flask not installed')
class Panel(unittest.TestCase):
    def test_normal_login_csrf_host_and_torrent_guards(self):
        from werkzeug.security import generate_password_hash
        with tempfile.TemporaryDirectory() as td:
            p = Path(td);(p/'secret.key').write_text('isolated-test-secret-not-a-real-credential')
            with sqlite3.connect(p/'panel.db') as db:
                db.execute('create table users(username text primary key,password_hash text,version integer default 1)')
                db.execute('create table audit(id integer primary key,at real,user text,ip text,action text,detail text)')
                db.execute('insert into users(username,password_hash) values(?,?)', ('test-user',generate_password_hash('isolated-test-password')))
            db.close()
            env = {'PANEL_STATE':str(p),'PANEL_HOME':str(ROOT/'src/panel'),'PANEL_CONFIG':str(p/'channels.json'),
                   'PANEL_NO_MONITOR':'1','PANEL_ALLOWED_HOSTS':'panel.example.com,localhost'}
            previous = {k:os.environ.get(k) for k in env};os.environ.update(env)
            try:
                module = load('panel_tests', ROOT/'src/panel/app.py');client = module.app.test_client();base='https://panel.example.com'
                self.assertEqual(client.get('/api/downloads',base_url=base).status_code,401)
                self.assertEqual(client.get('/api/health',base_url='https://wrong.example.com').status_code,400)
                r = client.post('/api/login',json={'username':'test-user','password':'isolated-test-password'},base_url=base)
                self.assertEqual(r.status_code,200);csrf=r.json['csrf']
                self.assertEqual(client.post('/api/downloads/add',json={},base_url=base).status_code,403)
                for route, data in [('add',{}),('1/control',{'action':'start'}),('settings',{'enabled':True})]:
                    r=client.post('/api/downloads/'+route,json=data,headers={'X-CSRF-Token':csrf,'Origin':base},base_url=base)
                    self.assertEqual(r.status_code,403);self.assertTrue(r.json['blocked'])
                r=client.post('/api/logout',json={},headers={'X-CSRF-Token':csrf,'Origin':'https://other.example.com'},base_url=base)
                self.assertEqual(r.status_code,403)
                r=client.get('/api/downloads',base_url=base);self.assertEqual(r.status_code,200);self.assertTrue(r.json['blocked'])
                with self.assertRaises(ValueError):module.rpc('torrent-start')
            finally:
                for k,v in previous.items():
                    if v is None:os.environ.pop(k,None)
                    else:os.environ[k]=v


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe') and importlib.util.find_spec('PIL'), 'FFmpeg/Pillow not installed')
class Playout(unittest.TestCase):
    def test_hls_switch_path_guard_and_epg(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td).resolve();media=p/'media';media.mkdir();control=p/'control/90';control.mkdir(parents=True)
            if os.environ.get('CYRIUS_TEST_SAMPLE'):
                # An externally generated synthetic fixture supports minimal FFmpeg builds without lavfi.
                shutil.copy2(os.environ['CYRIUS_TEST_SAMPLE'],media/'one.mp4')
            else:
                # Center-channel tone catches silent or incorrect 5.1-to-stereo downmixes.
                subprocess.run([shutil.which('ffmpeg'),'-nostdin','-v','error','-f','lavfi','-i','color=s=320x180:r=25',
                                '-f','lavfi','-i','aevalsrc=0|0|0.25*sin(2*PI*440*t)|0|0|0:s=44100:c=5.1',
                                '-t','12','-c:v','libx264','-threads','1','-g','25','-pix_fmt','yuv420p',
                                '-c:a','ac3','-b:a','384k','-shortest',str(media/'one.mp4')],check=True)
            shutil.copy2(media/'one.mp4',media/'two.mp4')
            cfg={'enabled':True,'channels':[{'id':90,'name':'Isolated test','slug':'isolated-test','media':str(media),
                 'public':str(p/'public/90/live'),'segment_type':'mpegts','allowed_video_codecs':['h264']}]}
            (p/'channels.json').write_text(json.dumps(cfg));(p/'movie-titles-ru.json').write_text('{"titles":{}}')
            env={**os.environ,'PYTHONPATH':str(ROOT/'src/playout'),'CYRIUS_CONFIG_PATH':str(p/'channels.json'),
                 'CYRIUS_STATE_ROOT':str(p/'state'),'CYRIUS_CONTROL_ROOT':str(p/'control'),'CYRIUS_PUBLIC_ROOT':str(p/'public'),
                 'CYRIUS_FFMPEG':shutil.which('ffmpeg'),'CYRIUS_FFPROBE':shutil.which('ffprobe'),
                 'CYRIUS_ORIGIN_URL':'http://new-origin.example.com:18080'}
            queue=load('queue_fixture',ROOT/'src/playout/cyrius_queue.py')
            def read(path):return queue.read(path)
            def wait(check, process, limit=25):
                end=time.monotonic()+limit
                while time.monotonic()<end:
                    if check():return
                    if process.poll() is not None:raise AssertionError('Worker exited: '+(p/'worker.log').read_text()[-1500:])
                    time.sleep(.2)
                raise AssertionError('Timed out: '+(p/'worker.log').read_text()[-1500:])
            with (p/'worker.log').open('w') as log:
                proc=subprocess.Popen([sys.executable,str(ROOT/'src/playout/cyrius-copy-playout.py'),'90'],env=env,stdout=log,stderr=log,umask=0o022)
                try:
                    state=p/'state/90/state.json';manifest=p/'public/90/live/index.m3u8'
                    wait(lambda:read(state).get('source')==str(media/'one.mp4') and manifest.exists(),proc)
                    self.assertTrue(manifest.stat().st_mode & 0o004)
                    fragment=next(line for line in manifest.read_text().splitlines() if line and not line.startswith('#'))
                    fragment_path=manifest.parent/fragment
                    self.assertTrue(fragment_path.stat().st_mode & 0o004)
                    streams=json.loads(subprocess.check_output([shutil.which('ffprobe'),'-v','error','-show_streams','-of','json',str(fragment_path)],env=env))['streams']
                    sound=next(s for s in streams if s['codec_type']=='audio')
                    self.assertEqual((sound['codec_name'],sound['profile'],sound['sample_rate'],sound['channels']),('aac','LC','48000',2))
                    pcm=subprocess.check_output([shutil.which('ffmpeg'),'-nostdin','-v','error','-i',str(fragment_path),'-t','1','-vn','-c:a','pcm_s16le','-f','s16le','pipe:1'],env=env)
                    if not os.environ.get('CYRIUS_TEST_SAMPLE'):
                        import array
                        samples=array.array('h');samples.frombytes(pcm)
                        self.assertGreater(max(map(abs,samples)),100)
                    self.assertEqual(read(state)['audio']['codec_name'],'aac')
                    cmd={'id':'switch','at':time.time(),'action':'play','source':str(media/'two.mp4')}
                    queue.atomic(control/'command.json',cmd)
                    wait(lambda:read(state).get('source')==str(media/'two.mp4') and read(control/'ack.json').get('id')=='switch',proc)
                    wait(lambda:any('run-' in line and line.split('/')[0]!=fragment.split('/')[0] for line in manifest.read_text().splitlines() if line and not line.startswith('#')),proc)
                    next_fragment=next(line for line in reversed(manifest.read_text().splitlines()) if line and not line.startswith('#'))
                    streams=json.loads(subprocess.check_output([shutil.which('ffprobe'),'-v','error','-show_streams','-of','json',str(manifest.parent/next_fragment)],env=env))['streams']
                    self.assertTrue(any(s.get('codec_name')=='aac' and s.get('channels')==2 for s in streams))
                    queue.atomic(control/'command.json',{'id':'bad','at':time.time(),'action':'play','source':'/etc/passwd'})
                    wait(lambda:read(control/'ack.json').get('id')=='bad',proc);self.assertFalse(read(control/'ack.json')['ok'])
                    queue.atomic(control/'command.json',{'id':'expired','at':time.time()-200,'action':'next'})
                    wait(lambda:read(control/'ack.json').get('id')=='expired',proc);self.assertFalse(read(control/'ack.json')['ok'])
                    subprocess.run([sys.executable,str(ROOT/'src/playout/cyrius-programme.py')],env=env,check=True,capture_output=True,umask=0o022)
                    self.assertTrue((p/'public/epg.xml').stat().st_mode & 0o004)
                    self.assertIn('http://new-origin.example.com:18080/isolated-test/index.m3u8',(p/'public/channels.m3u').read_text())
                    self.assertEqual(read(p/'public/programme.json')['timezone'],'Asia/Baku')
                finally:
                    proc.terminate()
                    try:proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait()


if __name__ == '__main__':unittest.main()
