"""Multicam Sync: local-only folder, audio alignment, preview and MP4 export server."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs
import uuid
import webbrowser
import zipfile

import media_engine
import export_engine

APP = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
HOME = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
DATA = Path(os.environ.get('MULTICAM_DATA', str(HOME / 'data')))
FFMPEG = str(APP / 'bin' / 'ffmpeg.exe')
FFPROBE = str(APP / 'bin' / 'ffprobe.exe')
SAMPLE = 'multi-cam sample video/Three_CAM'
LOCK = threading.RLock()
STATE = {'appName': 'MulticamSync', 'project': None, 'job': None, 'exports': [], 'sampleFolder': SAMPLE}
CANCEL = threading.Event()
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def save_session():
    DATA.mkdir(parents=True, exist_ok=True)
    with LOCK:
        p = DATA / 'session.json.tmp'
        p.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding='utf-8')
        p.replace(DATA / 'session.json')


def snapshot():
    with LOCK:
        return copy.deepcopy(STATE)


def progress(info):
    if CANCEL.is_set():
        raise RuntimeError('사용자가 작업을 취소했습니다.')
    with LOCK:
        if STATE['job']:
            value = float(info.get('progress', 0))
            STATE['job'].update(progress=round(min(100, max(0, value)), 1), message=info.get('message', '처리 중'))


def start_job(kind, action):
    with LOCK:
        if STATE['job'] and STATE['job']['status'] == 'running':
            raise ValueError('진행 중인 작업이 끝난 후 실행해주세요.')
        CANCEL.clear()
        STATE['job'] = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'running', 'progress': 0, 'message': '준비 중', 'result': None, 'error': None}

    def work():
        try:
            result = action()
            with LOCK:
                STATE['job'].update(status='cancelled' if CANCEL.is_set() else 'done', progress=100, message='취소됨' if CANCEL.is_set() else '완료', result=result)
        except Exception as exc:
            traceback.print_exc()
            with LOCK:
                STATE['job'].update(status='cancelled' if CANCEL.is_set() else 'error', message=str(exc), error=str(exc))
        finally:
            save_session()
    threading.Thread(target=work, daemon=True).start()
    return snapshot()


def current_project():
    p = snapshot()['project']
    if not p:
        raise ValueError('먼저 촬영 폴더를 불러오세요.')
    return p


def total_duration(p):
    offsets = {c['id']: float(c.get('offset', 0)) for c in p['cameras']}
    p['duration'] = max((float(c['start']) + offsets[c['cameraId']] + float(c['duration']) for c in p['clips']), default=0)


def run_process(args, cancel=CANCEL):
    log = DATA / ('ffmpeg-' + uuid.uuid4().hex + '.log')
    with log.open('wb') as output:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output, stderr=output, creationflags=NO_WINDOW)
        while proc.poll() is None:
            if cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise RuntimeError('사용자가 작업을 취소했습니다.')
            time.sleep(.2)
    if proc.returncode:
        raise RuntimeError(log.read_text(encoding='utf-8', errors='replace')[-2500:])
    log.unlink(missing_ok=True)


def preview_path(clip):
    src = Path(clip['path'])
    key = hashlib.sha256(f'{src}|{src.stat().st_size}|{src.stat().st_mtime_ns}'.encode()).hexdigest()[:24]
    return DATA / 'previews' / (key + '.mp4')


def create_previews():
    project = current_project()
    clips = project['clips']
    for index, clip in enumerate(clips):
        progress({'progress': index / len(clips) * 100, 'message': f"미리보기 준비 {index + 1}/{len(clips)} · {clip['name']}"})
        dst = preview_path(clip)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            original = Path(clip['path'])
            proxy = original.with_suffix('.LRF')
            if not proxy.exists():
                proxy = original.with_suffix('.lrf')
            src = str(proxy if proxy.exists() else original)
            tmp = dst.with_suffix('.partial.mp4')
            # DJI's LRF is an H.264 low-resolution preview. Remux if compatible;
            # retain original audio because some LRF variants have no audio track.
            info = media_engine.probe(src)
            streams = info.get('streams', []) if isinstance(info, dict) else []
            codec = next((s.get('codec_name') for s in streams if s.get('codec_type') == 'video'), '')
            if not codec and isinstance(info, dict):
                codec = info.get('codec', info.get('videoCodec', ''))
            args = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', src]
            if str(original) != src:
                args += ['-i', str(original), '-map', '0:v:0', '-map', '1:a:0?']
            else:
                args += ['-map', '0:v:0', '-map', '0:a:0?']
            if codec == 'h264' and proxy.exists():
                args += ['-c:v', 'copy']
            else:
                args += ['-vf', 'scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26', '-threads', '4', '-pix_fmt', 'yuv420p']
            args += ['-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', '-shortest', str(tmp)]
            try:
                run_process(args)
                tmp.replace(dst)
            finally:
                tmp.unlink(missing_ok=True)
        clip['previewReady'] = True
        with LOCK:
            if STATE['project']:
                for original_clip in STATE['project']['clips']:
                    if original_clip['id'] == clip['id']:
                        original_clip['previewReady'] = True
    return {'count': len(clips)}


def resolve_folder(path):
    folder = Path(str(path).strip()).expanduser()
    if not folder.is_absolute():
        folder = HOME / folder
    return folder.resolve()


def import_folder(path):
    folder = resolve_folder(path)
    if not folder.is_dir():
        raise ValueError('존재하는 촬영 폴더를 선택해주세요.')
    project = media_engine.scan_folder(str(folder), progress=progress)
    if len(project['cameras']) < 2:
        raise ValueError('카메라 2대 이상의 영상이 필요합니다. CAM A, CAM B 폴더에 나누어 넣어주세요.')
    for clip in project['clips']:
        clip['previewReady'] = preview_path(clip).exists()
    with LOCK:
        STATE['project'] = project
        STATE['exports'] = []
    return {'cameraCount': len(project['cameras']), 'clipCount': len(project['clips'])}


def sync_project(method):
    if method not in ('auto', 'audio', 'timecode'):
        raise ValueError('지원하지 않는 싱크 방식입니다.')
    project = media_engine.auto_sync(current_project(), method=method, progress=progress)
    total_duration(project)
    with LOCK:
        STATE['project'] = project
    return {'clipCount': len(project['clips'])}


def finite(value, limit=864000):
    number = float(value)
    if not math.isfinite(number) or abs(number) > limit:
        raise ValueError('유효한 시간 숫자를 입력하세요.')
    return number


def update_project(payload):
    project = current_project()
    for c in project['cameras']:
        if c['id'] in payload.get('cameraOffsets', {}):
            c['offset'] = finite(payload['cameraOffsets'][c['id']])
    for clip in project['clips']:
        if clip['id'] in payload.get('clipStarts', {}):
            clip['start'] = finite(payload['clipStarts'][clip['id']])
    total_duration(project)
    with LOCK:
        STATE['project'] = project
    save_session()
    return snapshot()


def load_project(data):
    if not isinstance(data, dict) or not data.get('cameras') or not data.get('clips'):
        raise ValueError('올바른 Multicam Sync 프로젝트 JSON을 선택해주세요.')
    if len(data['cameras']) < 2 or len(data['cameras']) > 32:
        raise ValueError('프로젝트는 카메라 2~32대를 지원합니다.')
    ids = {c['id'] for c in data['cameras']}
    if len(ids) != len(data['cameras']):
        raise ValueError('카메라 ID가 중복되었습니다.')
    clip_ids = set()
    for c in data['cameras']:
        c['offset'] = finite(c.get('offset', 0))
    for c in data['clips']:
        if c['id'] in clip_ids or c['cameraId'] not in ids:
            raise ValueError('잘못된 클립 ID입니다.')
        clip_ids.add(c['id'])
        p = Path(c['path']).resolve()
        if not p.is_file() or p.suffix.lower() not in media_engine.VIDEO_EXTENSIONS:
            raise ValueError(f'원본 영상을 찾을 수 없습니다: {p.name}')
        c['path'] = str(p)
        for k in ('start', 'autoStart', 'duration', 'fps'):
            c[k] = finite(c.get(k, c.get('start', 0)))
        if c['duration'] <= 0 or c['fps'] <= 0:
            raise ValueError('잘못된 영상 길이 또는 프레임 속도입니다.')
        c['previewReady'] = preview_path(c).exists()
    total_duration(data)
    with LOCK:
        STATE['project'] = data
        STATE['exports'] = []
    save_session()
    return snapshot()


def do_export(options):
    project = current_project()
    destination = HOME / 'exports' / (time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
    destination.mkdir(parents=True, exist_ok=True)
    paths = export_engine.export_project(project, options, str(destination), FFMPEG, FFPROBE, progress=progress, cancel=CANCEL)
    if options.get('mode') in ('individual', 'both'):
        archive = destination / '전체_추출영상.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as z:
            for path in paths:
                if CANCEL.is_set():
                    raise RuntimeError('사용자가 작업을 취소했습니다.')
                z.write(path, Path(path).name)
        paths = [*paths, archive]
    results = []
    for path in paths:
        path = Path(path)
        ident = uuid.uuid4().hex
        results.append({'id': ident, 'name': path.name, 'path': str(path), 'size': path.stat().st_size, 'url': '/api/download/' + ident})
    # A reviewable project and exact export settings accompany every render.
    (destination / 'project.json').write_text(json.dumps({'project': project, 'export': options}, ensure_ascii=False, indent=2), encoding='utf-8')
    with LOCK:
        STATE['exports'].extend(results)
    return results


def pick_folder():
    script = "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='CAM A, CAM B 폴더가 들어있는 촬영 폴더를 선택하세요'; $d.ShowNewFolderButton=$false; if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){ [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; [Console]::Write($d.SelectedPath) }"
    import base64
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    proc = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-EncodedCommand', encoded], capture_output=True, timeout=600, creationflags=NO_WINDOW)
    if proc.returncode:
        raise ValueError('폴더 선택기를 열지 못했습니다. 폴더 경로를 직접 붙여넣어 주세요.')
    return {'path': proc.stdout.decode('utf-8-sig').strip()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        if self.path != '/api/state':
            print(fmt % args, flush=True)

    def json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def host_valid(self):
        return self.headers.get('Host', '').split(':')[0] in ('127.0.0.1', 'localhost')

    def file(self, path, download=False, head=False):
        path = Path(path)
        if not path.is_file():
            self.json({'error': '파일을 찾을 수 없습니다.'}, 404)
            return
        size = path.stat().st_size
        start, end, partial = 0, size - 1, False
        value = self.headers.get('Range')
        if value:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
            if not match or not any(match.groups()):
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            a, b = match.groups()
            if a:
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
            else:
                start = max(0, size - int(b))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            partial = True
        self.send_response(206 if partial else 200)
        content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if path.suffix.lower() == '.lrf':
            content_type = 'video/mp4'
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(max(0, end - start + 1)))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if download:
            from urllib.parse import quote
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(path.name))
        self.end_headers()
        if head:
            return
        try:
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if not self.host_valid():
            return self.json({'error': '로컬 연결만 지원합니다.'}, 403)
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        try:
            if route == '/api/state':
                return self.json(snapshot())
            if route == '/api/project-download':
                body = json.dumps(current_project(), ensure_ascii=False, indent=2).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Disposition', 'attachment; filename="multicam-project.json"')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if route.startswith('/media/') or route.startswith('/api/waveform/'):
                ident = route.rsplit('/', 1)[-1]
                clip = next((c for c in current_project()['clips'] if c['id'] == ident), None)
                if not clip:
                    return self.json({'error': '영상을 찾을 수 없습니다.'}, 404)
                if route.startswith('/api/waveform/'):
                    return self.json(media_engine.waveform(clip))
                preview = preview_path(clip)
                use_preview = parse_qs(parsed.query).get('proxy') == ['1'] and preview.exists()
                return self.file(preview if use_preview else clip['path'], head=head)
            if route.startswith('/api/download/'):
                result = next((e for e in snapshot()['exports'] if e['id'] == route.rsplit('/', 1)[-1]), None)
                if not result:
                    return self.json({'error': '추출 파일을 찾을 수 없습니다.'}, 404)
                return self.file(result['path'], download=True, head=head)
            relative = route[len('/static/'):] if route.startswith('/static/') else route.lstrip('/')
            target = (APP / 'static' / (relative or 'index.html')).resolve()
            if not target.is_relative_to((APP / 'static').resolve()):
                return self.json({'error': '잘못된 경로입니다.'}, 403)
            return self.file(target, head=head)
        except Exception as exc:
            self.json({'error': str(exc)}, 400)

    def do_POST(self):
        origin = self.headers.get('Origin')
        expected = 'http://' + self.headers.get('Host', '')
        allowed = self.host_valid() and (not origin or origin == expected) and 'application/json' in self.headers.get('Content-Type', '')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 8 * 1024 * 1024:
                raise ValueError('프로젝트 요청이 너무 큽니다.')
            raw = self.rfile.read(size)
            if not allowed:
                return self.json({'error': '허용되지 않은 요청입니다.'}, 403)
            data = json.loads(raw or b'{}')
            route = urlparse(self.path).path
            if route == '/api/cancel':
                CANCEL.set()
                return self.json(snapshot())
            if route == '/api/pick-folder':
                return self.json(pick_folder())
            running = snapshot()['job']
            if running and running['status'] == 'running':
                raise ValueError('진행 중인 작업이 끝난 후 실행해주세요.')
            if route == '/api/shutdown':
                self.json({'message': '프로그램이 종료되었습니다.'})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if route == '/api/import':
                return self.json(start_job('import', lambda: import_folder(data.get('path', ''))))
            if route == '/api/sync':
                return self.json(start_job('sync', lambda: sync_project(data.get('method', 'auto'))))
            if route == '/api/proxies':
                return self.json(start_job('proxies', create_previews))
            if route == '/api/update':
                return self.json(update_project(data))
            if route == '/api/project-load':
                return self.json(load_project(data.get('project')))
            if route == '/api/export':
                return self.json(start_job('export', lambda: do_export(data)))
            return self.json({'error': '지원하지 않는 기능입니다.'}, 404)
        except Exception as exc:
            return self.json({'error': str(exc)}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    if getattr(sys, 'frozen', False):
        logfile = (DATA / 'application.log').open('a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = logfile
    if not args.no_browser and (DATA / 'server.json').exists():
        try:
            import urllib.request
            previous = json.loads((DATA / 'server.json').read_text(encoding='utf-8'))
            url = previous['url']
            if re.fullmatch(r'http://127\.0\.0\.1:\d+', url):
                with urllib.request.urlopen(url + '/api/state', timeout=1) as response:
                    existing = json.load(response)
                if existing.get('appName') == 'MulticamSync':
                    webbrowser.open(url)
                    return
        except Exception:
            pass
    media_engine.configure(FFMPEG, FFPROBE, str(DATA / 'audio'), cancel=CANCEL)
    if args.restore and (DATA / 'session.json').exists():
        try:
            saved = json.loads((DATA / 'session.json').read_text(encoding='utf-8'))
            STATE.update(saved)
            STATE['job'] = None
        except (OSError, ValueError):
            pass
    STATE['sampleFolder'] = SAMPLE
    project = STATE.get('project') or {}
    folder = project.get('folder')
    if folder and not resolve_folder(folder).is_dir():
        STATE['project'] = None
        STATE['exports'] = []
    server = None
    for port in range(args.port, args.port + 30):
        try:
            server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    if not server:
        raise RuntimeError('로컬 프로그램 포트를 열 수 없습니다.')
    url = f'http://127.0.0.1:{server.server_port}'
    print('Multicam Sync · ' + url, flush=True)
    (DATA / 'server.json').write_text(json.dumps({'url': url, 'pid': os.getpid()}), encoding='utf-8')
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        CANCEL.set()
        server.server_close()


if __name__ == '__main__':
    main()
