"""YT Downloader - ứng dụng cửa sổ cho yt-dlp.

Chạy: pythonw server.py  (giao diện là index.html, hiển thị trong cửa sổ pywebview)
"""
import ctypes
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
TOOLS = ROOT / '_tools'

# pythonw không có console: sys.stdout/stderr là None, yt-dlp ghi vào đó sẽ lỗi -> ghi ra file
if sys.stderr is None or sys.stdout is None:
    sys.stdout = sys.stderr = open(APP_DIR / 'app.log', 'a', encoding='utf-8', buffering=1)

# Đảm bảo tìm thấy ffmpeg / deno / node kể cả khi PATH của hệ thống chưa cập nhật
for _d in (TOOLS / 'ffmpeg' / 'bin', TOOLS / 'deno', TOOLS / 'node'):
    if _d.is_dir():
        os.environ['PATH'] = str(_d) + os.pathsep + os.environ.get('PATH', '')
sys.path.insert(0, str(ROOT / 'yt-dlp'))

import webview  # noqa: E402
import yt_dlp  # noqa: E402

import editor  # noqa: E402
import oc_launcher  # noqa: E402
from yt_dlp.utils import DownloadCancelled  # noqa: E402

HOST, PORT = '127.0.0.1', 0  # PORT được gán lại sau khi chọn được cổng trống
WINDOW = None
OC_WINDOW = None  # cửa sổ OpenCut (None khi chưa mở hoặc đã đóng)
MAX_PARALLEL = 2
RENDER_PARALLEL = 2
SETTINGS_FILE = APP_DIR / 'settings.json'
VIDEO_TYPES = ('Video (*.mp4;*.mkv;*.webm;*.mov;*.avi;*.m4v)', 'Tất cả (*.*)')
IMAGE_TYPES = ('Ảnh (*.png;*.jpg;*.jpeg;*.webp)',)
DEFAULT_SETTINGS = {
    'outdir': str(ROOT / 'downloads'),
    'export_dir': str(ROOT / 'exports'),
    'after_template': '',  # mẫu tự áp sau khi tải xong ('' = không)
    'mode': 'video',
    'quality': 'best',
    'audio_format': 'mp3',
    'subs': False,
    'audio_lang': '',  # '' = giọng gốc, hoặc mã ngôn ngữ của bản lồng tiếng (vd: 'vi')
}


def load_settings():
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding='utf-8')


# ---------------------------------------------------------------- Lấy thông tin

class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _extract(url, noplaylist):
    opts = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'extract_flat': 'in_playlist', 'noplaylist': noplaylist,
        'logger': _QuietLogger(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def get_info(url):
    info = _extract(url, noplaylist=True)
    result = {'video': None, 'playlist': None}
    if info.get('_type') == 'playlist':
        entries = info.get('entries') or []
        result['playlist'] = {'title': info.get('title'), 'count': info.get('playlist_count') or len(entries),
                              'uploader': info.get('uploader') or info.get('channel'),
                              'thumbnail': _playlist_thumb(info)}
        return result

    heights = sorted({f['height'] for f in info.get('formats') or []
                      if f.get('height') and f.get('vcodec') not in (None, 'none')}, reverse=True)
    result['video'] = {
        'title': info.get('title'), 'uploader': info.get('uploader') or info.get('channel'),
        'duration': info.get('duration'), 'thumbnail': info.get('thumbnail'), 'heights': heights,
        'audio_langs': _audio_langs(info.get('formats') or []),
    }
    # Link video nằm trong playlist (…&list=…) -> cho phép chọn tải cả playlist
    if 'list' in parse_qs(urlparse(url).query):
        try:
            pl = _extract(url, noplaylist=False)
            if pl.get('_type') == 'playlist':
                result['playlist'] = {'title': pl.get('title'),
                                      'count': pl.get('playlist_count') or len(pl.get('entries') or [])}
        except Exception:
            pass
    return result


def _audio_langs(formats):
    """Các track âm thanh theo ngôn ngữ (giọng gốc + lồng tiếng). Rỗng nếu video chỉ có 1 giọng."""
    langs = {}
    for f in formats:
        code = f.get('language')
        if not code or f.get('acodec') in (None, 'none'):
            continue
        note = f.get('format_note') or ''
        entry = langs.setdefault(code, {'code': code, 'name': note.split(',')[0].replace(' original (default)', '').strip() or code,
                                        'original': False})
        if 'original' in note or (f.get('language_preference') or 0) >= 10:
            entry['original'] = True
    return sorted(langs.values(), key=lambda x: not x['original']) if len(langs) > 1 else []


def _playlist_thumb(info):
    thumbs = info.get('thumbnails') or []
    if thumbs:
        return thumbs[-1].get('url')
    for e in info.get('entries') or []:
        t = (e.get('thumbnails') or [{}])[-1].get('url')
        if t:
            return t
    return None


# ---------------------------------------------------------------- Tải xuống

class Job:
    def __init__(self, req):
        self.id = uuid.uuid4().hex[:8]
        self.url = req['url']
        self.req = req
        self.title = req.get('title') or req['url']
        self.status = 'queued'  # queued | downloading | processing | done | error | cancelled
        self.stage = ''
        self.percent = 0.0
        self.speed = None
        self.eta = None
        self.item = None
        self.error = None
        self.filepath = None
        self.files = []  # file đã tải xong (playlist có nhiều file)
        self.outdir = req['outdir']
        self.cancel = threading.Event()

    kind = 'download'

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            'id', 'kind', 'url', 'title', 'status', 'stage', 'percent', 'speed', 'eta', 'item', 'error', 'filepath')}

    def stop(self):
        self.cancel.set()


class JobLogger:
    def __init__(self, job):
        self.job = job

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass

    def error(self, msg):
        self.job.error = msg.removeprefix('ERROR: ').strip()


def build_argv(req):
    playlist = bool(req.get('playlist'))
    lang = req.get('audio_lang') or ''
    if not re.fullmatch(r'[A-Za-z0-9-]*', lang):
        raise ValueError('Mã ngôn ngữ không hợp lệ')
    name = '%(title)s' + (f' [{lang}]' if lang else '') + '.%(ext)s'
    argv = [
        '--ignore-config', '--no-progress', '--embed-metadata',
        '-P', req['outdir'],
        '-o', '%(playlist_title|Playlist)s/%(playlist_index|0)03d - ' + name if playlist else name,
        '--yes-playlist' if playlist else '--no-playlist',
    ]
    if playlist:
        argv.append('--ignore-errors')
    # Chọn track theo ngôn ngữ; video không có bản lồng tiếng đó thì lấy giọng gốc
    want = f'[language={lang}]' if lang else ''
    if req.get('mode') == 'audio':
        argv += ['-f', f'ba{want}/ba/b', '-x', '--audio-format', req.get('audio_format') or 'mp3',
                 '--audio-quality', '0', '--embed-thumbnail']
    else:
        q = req.get('quality') or 'best'
        # Ưu tiên H.264 + AAC để mở được trên mọi máy (YouTube chỉ có H.264 tới 1080p)
        sort = ('res' if q == 'best' else f'res:{int(q)}') + ',vcodec:h264,acodec:aac,ext:mp4:m4a'
        argv += ['-f', f'bv*+ba{want}/bv*+ba/b' if lang else 'bv*+ba/b', '-S', sort,
                 '--merge-output-format', 'mp4']
        if req.get('subs'):
            argv += ['--write-subs', '--sub-langs', 'vi.*,en.*', '--embed-subs']
    return argv


def run_job(job):
    if job.cancel.is_set():
        job.status = 'cancelled'
        return

    def progress_hook(d):
        if job.cancel.is_set():
            raise DownloadCancelled()
        info = d.get('info_dict') or {}
        if info.get('title') and not job.req.get('title'):
            job.title = info['title']
        if info.get('playlist_index') and info.get('n_entries'):
            job.item = f"{info['playlist_index']}/{info['n_entries']}"
        if d['status'] == 'downloading':
            job.status = 'downloading'
            job.stage = 'Âm thanh' if info.get('vcodec') == 'none' else (
                'Video' if info.get('acodec') == 'none' else '')
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total:
                job.percent = min(100.0, d.get('downloaded_bytes', 0) * 100 / total)
            job.speed, job.eta = d.get('speed'), d.get('eta')
        elif d['status'] == 'finished':
            job.percent, job.speed, job.eta = 100.0, None, None

    def pp_hook(d):
        if job.cancel.is_set():
            raise DownloadCancelled()
        if d['status'] == 'started':
            job.status, job.stage = 'processing', d.get('postprocessor', '')
        elif d['status'] == 'finished':
            path = (d.get('info_dict') or {}).get('filepath')
            if path:
                job.filepath = path

    try:
        ydl_opts = yt_dlp.parse_options(build_argv(job.req)).ydl_opts
        ydl_opts.update({
            'logger': JobLogger(job), 'quiet': True, 'noprogress': True,
            'progress_hooks': [progress_hook], 'postprocessor_hooks': [pp_hook],
            'post_hooks': [job.files.append],  # nhận đường dẫn file cuối cùng của từng video
        })
        job.status = 'downloading'
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            retcode = ydl.download([job.url])
        if job.cancel.is_set():
            job.status = 'cancelled'
        elif retcode and not job.req.get('playlist'):
            job.status = 'error'
            job.error = job.error or 'Tải thất bại'
        else:
            job.status, job.percent, job.stage = 'done', 100.0, ''
    except DownloadCancelled:
        job.status = 'cancelled'
    except Exception as e:  # noqa: BLE001
        job.status = 'cancelled' if job.cancel.is_set() else 'error'
        job.error = job.error or str(e).removeprefix('ERROR: ')
    job.speed = job.eta = None
    if job.files:
        job.filepath = job.files[-1]

    # Tải xong thì tự áp mẫu (chỉ với video)
    tid = job.req.get('after_template')
    if tid and job.req.get('mode') != 'audio' and not job.cancel.is_set():
        for f in job.files:
            enqueue_render(tid, f, job.req.get('export_dir') or DEFAULT_SETTINGS['export_dir'])


JOBS: dict = {}
JOB_QUEUE: 'queue.Queue[Job]' = queue.Queue()
RENDER_QUEUE: 'queue.Queue[editor.RenderJob]' = queue.Queue()


def worker():
    while True:
        run_job(JOB_QUEUE.get())


def render_worker():
    while True:
        editor.run_render(RENDER_QUEUE.get())


def enqueue_render(tid, src, outdir):
    job = editor.RenderJob(tid, src, outdir)
    JOBS[job.id] = job
    RENDER_QUEUE.put(job)
    return job


MEDIA: dict = {}  # token -> đường dẫn video được phép phát trong khung xem trước


def media_token(path):
    token = hashlib.sha1(str(path).encode('utf-8')).hexdigest()[:16]
    MEDIA[token] = str(path)
    return token


def with_media(tpl):
    video = (tpl.get('frame') or {}).get('video')
    tpl['media'] = media_token(video) if video and Path(video).is_file() else None
    return tpl


def pick_files(types, multiple, directory=''):
    result = WINDOW.create_file_dialog(webview.FileDialog.OPEN, directory=directory,
                                       allow_multiple=multiple, file_types=types)
    return [os.path.normpath(p) for p in result or []]


def open_in_explorer(path):
    p = Path(path)
    if p.is_file():
        subprocess.Popen(f'explorer /select,"{p}"')
    else:
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(p)


def pick_folder(initial):
    result = WINDOW.create_file_dialog(webview.FileDialog.FOLDER, directory=initial)
    return os.path.normpath(result[0]) if result else None


def read_clipboard():
    CF_UNICODETEXT = 13
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        return ''
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        ptr = kernel32.GlobalLock(handle) if handle else None
        if not ptr:
            return ''
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


class OpenCutBridge:
    """Hàm OpenCut gọi được qua window.pywebview.api.

    Xuất hàng loạt song song: mỗi cửa sổ xuất là một tiến trình trình duyệt riêng,
    nên nhiều video được dựng hình cùng lúc thay vì thay phiên trên một luồng.
    """

    MAX_WORKERS = 4

    def __init__(self):
        self._workers = []  # thuộc tính bắt đầu bằng _ không bị pywebview đưa sang JS

    def open_export_workers(self, count):
        self.close_export_workers()
        n = max(1, min(int(count), self.MAX_WORKERS))
        for i in range(n):
            # Đặt ngoài màn hình thay vì ẩn: cửa sổ ẩn có thể bị trình duyệt giảm tốc
            w = webview.create_window(f'OpenCut — xuất nền {i + 1}', oc_launcher.URL + '/batch-worker',
                                      width=360, height=200, x=-32000, y=-32000, focus=False)
            w.events.shown += lambda w=w: hide_from_taskbar(w)
            self._workers.append(w)
        return n

    def close_export_workers(self):
        for w in self._workers:
            try:
                w.destroy()
            except Exception:  # noqa: BLE001 - cửa sổ đã đóng
                pass
        self._workers = []


OC_BRIDGE = OpenCutBridge()


def hide_from_taskbar(window):
    user32 = ctypes.windll.user32
    user32.GetWindowLongW.restype = ctypes.c_long
    hwnd = window.native.Handle.ToInt64()
    style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), -20)  # GWL_EXSTYLE
    style = (style | 0x80) & ~0x40000  # + WS_EX_TOOLWINDOW, - WS_EX_APPWINDOW
    user32.SetWindowLongW(ctypes.c_void_p(hwnd), -20, style)
    user32.ShowWindow(ctypes.c_void_p(hwnd), 0)  # ẩn rồi hiện lại để taskbar cập nhật
    user32.ShowWindow(ctypes.c_void_p(hwnd), 4)  # SW_SHOWNOACTIVATE


def open_opencut():
    global OC_WINDOW
    url = oc_launcher.ensure_running()
    if OC_WINDOW is not None:
        OC_WINDOW.restore()
        OC_WINDOW.show()
        return
    OC_WINDOW = webview.create_window('OpenCut — chỉnh sửa video', url + '/projects', js_api=OC_BRIDGE,
                                      width=1440, height=900, min_size=(1000, 640), maximized=True)
    OC_WINDOW.events.shown += lambda: set_window_icon(OC_WINDOW)

    def on_closed():
        global OC_WINDOW
        OC_WINDOW = None
        OC_BRIDGE.close_export_workers()
    OC_WINDOW.events.closed += on_closed


def close_all_windows():
    """Đóng cửa sổ chính thì đóng luôn OpenCut và cửa sổ xuất, để app thoát hẳn."""
    OC_BRIDGE.close_export_workers()
    if OC_WINDOW is not None:
        try:
            OC_WINDOW.destroy()
        except Exception:  # noqa: BLE001 - cửa sổ đã đóng
            pass


def set_window_icon(window=None):
    window = window or WINDOW
    icon = APP_DIR / 'icon.ico'
    if not icon.is_file():
        return
    hwnd = window.native.Handle.ToInt64()
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    for size, which in ((16, 0), (32, 1)):  # ICON_SMALL, ICON_BIG
        h = user32.LoadImageW(None, str(icon), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        user32.SendMessageW(hwnd, 0x80, which, h)  # WM_SETICON


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = 'YTDownloader'

    def log_message(self, *args):
        pass

    def _allowed(self):
        # Chặn trang web lạ gọi vào server (DNS rebinding / CSRF)
        if self.headers.get('Host') not in (f'{HOST}:{PORT}', f'localhost:{PORT}'):
            return False
        origin = self.headers.get('Origin')
        return origin in (None, f'http://{HOST}:{PORT}', f'http://localhost:{PORT}')

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {'error': 'forbidden'})
        path = urlparse(self.path).path
        if path == '/':
            return self._send(200, (APP_DIR / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        if path == '/api/settings':
            return self._send(200, load_settings())
        if path == '/api/jobs':
            return self._send(200, [j.to_dict() for j in reversed(list(JOBS.values()))])
        try:
            if path.startswith('/tpl/frame/'):
                f = editor.frame_file(path.rsplit('/', 1)[1].removesuffix('.jpg'))
                return self._send_file(f, 'image/jpeg')
            if path.startswith('/tpl/assets/'):
                f = editor.asset_path('assets/' + path.rsplit('/', 1)[1])
                return self._send_file(f, 'image/' + f.suffix.lstrip('.').replace('jpg', 'jpeg'))
            if path.startswith('/font/'):
                return self._send_file(Path(editor.font_path(path.rsplit('/', 1)[1])), 'font/ttf')
            if path.startswith('/media/') and path.rsplit('/', 1)[1] in MEDIA:
                return self._send_media(Path(MEDIA[path.rsplit('/', 1)[1]]))
        except ValueError:
            pass
        except ConnectionError:  # trình phát video tự huỷ yêu cầu khi tua
            return
        self._send(404, {'error': 'not found'})

    def _send_media(self, f):
        """Phát video có hỗ trợ tua (HTTP Range)."""
        size = f.stat().st_size
        start, end = 0, size - 1
        m = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range') or '')
        if m and (m[1] or m[2]):
            start, end = (int(m[1]), int(m[2]) if m[2] else size - 1) if m[1] else (size - int(m[2]), size - 1)
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        else:
            self.send_response(200)
        ctype = {'.webm': 'video/webm', '.mkv': 'video/x-matroska', '.mov': 'video/quicktime'}.get(f.suffix.lower(), 'video/mp4')
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        self.end_headers()
        with open(f, 'rb') as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _send_file(self, f, ctype):
        if not f.is_file():
            return self._send(404, {'error': 'not found'})
        return self._send(200, f.read_bytes(), ctype)

    def do_POST(self):
        if not self._allowed() or not (self.headers.get('Content-Type') or '').startswith('application/json'):
            return self._send(403, {'error': 'forbidden'})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        except ValueError:
            return self._send(400, {'error': 'JSON không hợp lệ'})
        try:
            return self._send(200, self.route(urlparse(self.path).path, req))
        except KeyError:
            return self._send(404, {'error': 'not found'})
        except Exception as e:  # noqa: BLE001
            return self._send(400, {'error': str(e).removeprefix('ERROR: ')})

    def route(self, path, req):
        if path == '/api/info':
            return get_info(req['url'].strip())
        if path == '/api/download':
            settings = {**load_settings(), **{k: req[k] for k in DEFAULT_SETTINGS if k in req}}
            save_settings(settings)
            job = Job({**settings, 'url': req['url'].strip(), 'title': req.get('title'),
                       'audio_lang': req.get('audio_lang') or '',
                       'playlist': bool(req.get('playlist'))})
            Path(job.req['outdir']).mkdir(parents=True, exist_ok=True)
            JOBS[job.id] = job
            JOB_QUEUE.put(job)
            return job.to_dict()
        if path == '/api/cancel':
            JOBS[req['id']].stop()
            if JOBS[req['id']].status == 'queued':
                JOBS[req['id']].status = 'cancelled'
            return {'ok': True}
        if path == '/api/settings':
            settings = {**load_settings(), **{k: req[k] for k in DEFAULT_SETTINGS if k in req}}
            save_settings(settings)
            return settings

        # ---- Mẫu
        if path == '/api/templates':
            return {'templates': editor.list_templates(), 'fonts': [
                {'key': f['key'], 'name': f['name']} for f in editor.list_fonts()],
                'encoder': editor.encoder_name()}
        if path == '/api/template/get':
            return with_media(editor.get_template(req['id']))
        if path == '/api/template/new':
            return with_media(editor.new_template(req.get('name') or 'Mẫu mới', req.get('copy_from')))
        if path == '/api/template/save':
            tpl = dict(req['template'])
            tpl.pop('media', None)
            return editor.save_template(tpl)
        if path == '/api/font/add':
            files = pick_files(('Font (*.ttf;*.otf)',), True)
            for f in files:
                editor.add_font(f)
            return {'added': len(files), 'fonts': [{'key': f['key'], 'name': f['name']} for f in editor.list_fonts()]}
        if path == '/api/template/delete':
            editor.delete_template(req['id'])
            return {'ok': True}
        if path == '/api/template/frame':
            files = pick_files(VIDEO_TYPES, False, load_settings()['outdir'])
            return with_media(editor.extract_frame(req['id'], files[0])) if files else {}
        if path == '/api/template/logo':
            files = pick_files(IMAGE_TYPES, False)
            return editor.add_asset(files[0]) if files else {}

        # ---- Xuất hàng loạt
        if path == '/api/pick-videos':
            return {'files': pick_files(VIDEO_TYPES, True, load_settings()['outdir'])}
        if path == '/api/pick-export-dir':
            folder = pick_folder(load_settings()['export_dir'])
            if folder:
                save_settings({**load_settings(), 'export_dir': folder})
            return {'export_dir': folder}
        if path == '/api/render':
            editor.get_template(req['template'])  # báo lỗi sớm nếu mẫu không tồn tại
            outdir = req.get('export_dir') or load_settings()['export_dir']
            return [enqueue_render(req['template'], f, outdir).to_dict() for f in req['files']]
        if path == '/api/clear':
            for jid in [j.id for j in JOBS.values() if j.status in ('done', 'error', 'cancelled')]:
                del JOBS[jid]
            return {'ok': True}
        if path == '/api/open':
            if req.get('id'):
                job = JOBS[req['id']]
                open_in_explorer(job.filepath or job.outdir)
            else:
                open_in_explorer(load_settings()[req.get('which') or 'outdir'])
            return {'ok': True}
        if path == '/api/clipboard':
            return {'text': read_clipboard()}
        if path == '/api/opencut/open':
            open_opencut()
            return {'ok': True}
        if path == '/api/pick-folder':
            folder = pick_folder(load_settings()['outdir'])
            if folder:
                save_settings({**load_settings(), 'outdir': folder})
            return {'outdir': folder}
        raise KeyError(path)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # trên Windows, bật cái này sẽ cho phép 2 server chung cổng
    daemon_threads = True


def main():
    global PORT, WINDOW
    # Tách khỏi nhóm "Python" trên taskbar để hiện icon riêng
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ytb.YTDownloader')
    httpd = Server((HOST, 0), Handler)
    PORT = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    for _ in range(MAX_PARALLEL):
        threading.Thread(target=worker, daemon=True).start()
    for _ in range(RENDER_PARALLEL):
        threading.Thread(target=render_worker, daemon=True).start()
    threading.Thread(target=editor.video_encoder, daemon=True).start()  # dò GPU sẵn trong nền
    if oc_launcher.is_built():  # bật sẵn OpenCut để lúc bấm mở không phải chờ
        threading.Thread(target=lambda: oc_launcher.ensure_running(), daemon=True).start()

    WINDOW = webview.create_window('YT Downloader', f'http://{HOST}:{PORT}/',
                                   width=1240, height=900, min_size=(760, 600))
    WINDOW.events.shown += lambda: set_window_icon(WINDOW)
    WINDOW.events.closed += close_all_windows
    webview.settings['ALLOW_DOWNLOADS'] = True  # nút xuất video của OpenCut tải file về máy
    try:
        webview.start(private_mode=False, storage_path=str(APP_DIR / '.webview'))
    finally:
        oc_launcher.stop()


if __name__ == '__main__':
    main()
