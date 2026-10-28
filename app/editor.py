"""Mẫu chỉnh sửa (template) và render hàng loạt bằng ffmpeg.

Toạ độ trong mẫu đều là tỉ lệ 0..1 theo khung hình, nên một mẫu áp được cho mọi độ phân giải.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / 'templates'
ASSETS_DIR = TEMPLATES_DIR / 'assets'
TMP_DIR = APP_DIR / '_tmp'
USER_FONTS_DIR = APP_DIR / 'fonts'
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

# Font có sẵn trong Windows, đều hỗ trợ tiếng Việt
SYSTEM_FONTS = [
    'arial.ttf', 'arialbd.ttf', 'ariblk.ttf', 'segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf', 'tahoma.ttf',
    'tahomabd.ttf', 'verdana.ttf', 'verdanab.ttf', 'times.ttf', 'timesbd.ttf', 'georgia.ttf', 'georgiab.ttf',
    'calibri.ttf', 'calibrib.ttf', 'trebuc.ttf', 'trebucbd.ttf', 'impact.ttf', 'comic.ttf', 'comicbd.ttf',
    'consola.ttf',
]


def _ffmpeg(name='ffmpeg'):
    return shutil.which(name) or name


# ---------------------------------------------------------------- Font

@lru_cache(maxsize=1)
def list_fonts():
    win = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
    local = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'Windows' / 'Fonts'
    paths = []
    for d in (USER_FONTS_DIR, local):
        if d.is_dir():
            paths += sorted(p for p in d.iterdir() if p.suffix.lower() in ('.ttf', '.otf'))
    paths += [win / f for f in SYSTEM_FONTS if (win / f).is_file()]
    fonts, seen = [], set()
    for p in paths:
        try:
            family, style = ImageFont.truetype(str(p), 20).getname()
        except OSError:
            continue
        name = family if style in ('Regular', 'Normal') else f'{family} {style}'
        if name not in seen:
            seen.add(name)
            fonts.append({'key': p.name, 'name': name, 'path': str(p)})
    return fonts


def font_path(key):
    for f in list_fonts():
        if f['key'] == key:
            return f['path']
    return list_fonts()[0]['path']


def add_font(src):
    src = Path(src)
    ImageFont.truetype(str(src), 20)  # báo lỗi nếu không phải file font
    USER_FONTS_DIR.mkdir(exist_ok=True)
    shutil.copy(src, USER_FONTS_DIR / src.name)
    list_fonts.cache_clear()
    return src.name


# ---------------------------------------------------------------- Khung hình

RATIOS = {'16:9': (16, 9), '9:16': (9, 16), '1:1': (1, 1), '4:5': (4, 5), '4:3': (4, 3), '3:4': (3, 4)}
DEFAULT_CANVAS = {'ratio': 'original', 'bg': 'blur', 'bgColor': '#000000',
                  'video': {'mode': 'fit', 'scale': 1.0, 'cx': 0.5, 'cy': 0.5}}


def canvas_of(tpl):
    c = {**DEFAULT_CANVAS, **(tpl.get('canvas') or {})}
    c['video'] = {**DEFAULT_CANVAS['video'], **(c.get('video') or {})}
    return c


def _even(v):
    return max(2, int(round(v / 2)) * 2)


def canvas_size(ratio, sw, sh):
    """Kích thước video xuất ra. Cạnh ngắn giữ theo video gốc (trong khoảng 720..2160)."""
    if ratio not in RATIOS:
        return _even(sw), _even(sh)
    a, b = RATIOS[ratio]
    short = min(max(min(sw, sh), 720), 2160)
    return (_even(short * a / b), _even(short)) if a >= b else (_even(short), _even(short * b / a))


def video_rect(canvas, CW, CH, sw, sh):
    v = canvas['video']
    s = (max if v['mode'] == 'fill' else min)(CW / sw, CH / sh) * float(v['scale'])
    vw, vh = _even(sw * s), _even(sh * s)
    return round(v['cx'] * CW - vw / 2), round(v['cy'] * CH - vh / 2), vw, vh


# ---------------------------------------------------------------- Mẫu

def _tpl_file(tid):
    if not re.fullmatch(r'[a-f0-9]{8}', tid or ''):
        raise ValueError('Mã mẫu không hợp lệ')
    return TEMPLATES_DIR / f'{tid}.json'


def list_templates():
    TEMPLATES_DIR.mkdir(exist_ok=True)
    items = []
    for p in sorted(TEMPLATES_DIR.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            t = json.loads(p.read_text(encoding='utf-8'))
            items.append({'id': t['id'], 'name': t.get('name') or 'Không tên'})
        except (OSError, ValueError, KeyError):
            pass
    return items


def get_template(tid):
    return json.loads(_tpl_file(tid).read_text(encoding='utf-8'))


def save_template(t):
    TEMPLATES_DIR.mkdir(exist_ok=True)
    if not t.get('id'):
        t['id'] = uuid.uuid4().hex[:8]
    _tpl_file(t['id']).write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding='utf-8')
    return t


def new_template(name='Mẫu mới', copy_from=None):
    t = get_template(copy_from) if copy_from else {'layers': []}
    t.update(id=None, name=name)
    t = save_template(t)
    if copy_from and frame_file(copy_from).is_file():
        shutil.copy(frame_file(copy_from), frame_file(t['id']))
    return t


def delete_template(tid):
    _tpl_file(tid).unlink(missing_ok=True)
    frame_file(tid).unlink(missing_ok=True)


def frame_file(tid):
    return TEMPLATES_DIR / f'{tid}.jpg'


def add_asset(src):
    """Chép ảnh logo vào thư mục của mẫu để mẫu không hỏng khi file gốc bị xoá/di chuyển."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    dest = ASSETS_DIR / f'{uuid.uuid4().hex[:8]}{src.suffix.lower()}'
    shutil.copy(src, dest)
    w, h = probe_image(dest)
    return {'src': f'assets/{dest.name}', 'w': w, 'h': h}


def asset_path(rel):
    p = (TEMPLATES_DIR / rel).resolve()
    if p.parent != ASSETS_DIR.resolve():
        raise ValueError('Đường dẫn không hợp lệ')
    return p


# ---------------------------------------------------------------- ffprobe

def probe(path):
    r = subprocess.run([_ffmpeg('ffprobe'), '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(path)],
                       capture_output=True, creationflags=NO_WINDOW)
    data = json.loads(r.stdout or b'{}')
    v = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
    if not v:
        raise ValueError(f'Không đọc được video: {Path(path).name}')
    w, h = int(v['width']), int(v['height'])
    rot = abs(int((v.get('side_data_list') or [{}])[0].get('rotation', 0) or 0))
    if rot in (90, 270):
        w, h = h, w
    return {'w': w, 'h': h, 'duration': float(data.get('format', {}).get('duration') or 0),
            'has_audio': any(s.get('codec_type') == 'audio' for s in data.get('streams', []))}


def probe_image(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def extract_frame(tid, video):
    info = probe(video)
    t = min(info['duration'] * 0.2, 30) if info['duration'] else 0
    subprocess.run([_ffmpeg(), '-y', '-v', 'error', '-ss', f'{t:.2f}', '-i', str(video), '-frames:v', '1',
                    '-q:v', '3', str(frame_file(tid))], check=True, creationflags=NO_WINDOW)
    tpl = get_template(tid)
    tpl['frame'] = {'w': info['w'], 'h': info['h'], 'video': str(video), 'duration': info['duration']}
    return save_template(tpl)


# ---------------------------------------------------------------- Dựng filter ffmpeg

def _esc(path):
    """Đường dẫn dùng bên trong filtergraph: D:\\a b -> 'D\\:/a b'"""
    return "'" + str(path).replace('\\', '/').replace(':', '\\:') + "'"


def _color(hex_color, alpha=1.0):
    c = (hex_color or '#000000').lstrip('#')
    return f'0x{c[:6]}@{max(0.0, min(1.0, float(alpha))):.2f}'


def _enable(layer):
    s, e = layer.get('start'), layer.get('end')
    if s in (None, '') and e in (None, ''):
        return ''
    s = float(s or 0)
    e = float(e) if e not in (None, '') else 1e9
    return f":enable='between(t,{s},{e})'"


def _wrap(text, font, size, max_w):
    f = ImageFont.truetype(font, size)
    lines = []
    for para in text.split('\n'):
        line = ''
        for word in para.split(' '):
            cand = f'{line} {word}'.strip()
            if line and f.getlength(cand) > max_w:
                lines.append(line)
                line = word
            else:
                line = cand
        lines.append(line)
    return '\n'.join(lines)


def build_filter(tpl, sw, sh, title, tmp):
    """Trả về (danh sách ảnh input thêm, filter_complex, (rộng, cao) video xuất)."""
    extra_inputs, chains = [], []
    cur, n = '0:v', 0

    def nxt():
        nonlocal n
        n += 1
        return f'v{n}'

    # Khung hình: đặt video lên nền (mờ hoặc màu) theo tỉ lệ đã chọn
    canvas = canvas_of(tpl)
    W, H = canvas_size(canvas['ratio'], sw, sh)
    vx, vy, vw, vh = video_rect(canvas, W, H, sw, sh)
    if (W, H) != (_even(sw), _even(sh)) or (vx, vy, vw, vh) != (0, 0, W, H):
        if canvas['bg'] == 'color':
            chains.append(f"[0:v]split[bgs][fg];[bgs]scale={W}:{H},drawbox=0:0:{W}:{H}:color={_color(canvas['bgColor'])}:t=fill,setsar=1[bg]")
        else:
            bw, bh = _even(W / 6), _even(H / 6)  # làm mờ trên ảnh nhỏ rồi phóng to cho nhanh
            chains.append(f'[0:v]split[bgs][fg];[bgs]scale={bw}:{bh}:force_original_aspect_ratio=increase,'
                          f'crop={bw}:{bh},gblur=sigma=6,scale={W}:{H},eq=brightness=-0.06,setsar=1[bg]')
        chains.append(f'[fg]scale={vw}:{vh},setsar=1[fgs];[bg][fgs]overlay={vx}:{vy}[v0]')
        cur = 'v0'

    short = min(W, H)
    for i, L in enumerate(tpl.get('layers', [])):
        if L.get('hidden'):
            continue
        x, y = round(L['x'] * W), round(L['y'] * H)
        w, h = max(2, round(L['w'] * W)), max(2, round(L.get('h', 0.1) * H))  # chữ không có chiều cao cố định
        x, y = max(0, min(x, W - 2)), max(0, min(y, H - 2))
        w, h = min(w, W - x), min(h, H - y)

        if L['type'] == 'cover':
            mode, out = L.get('mode', 'blur'), nxt()
            if mode == 'color':
                chains.append(f"[{cur}]drawbox=x={x}:y={y}:w={w}:h={h}:color={_color(L.get('color'))}:t=fill{_enable(L)}[{out}]")
            elif mode == 'delogo':
                dx, dy = max(1, x), max(1, y)
                dw, dh = min(w, W - dx - 1), min(h, H - dy - 1)
                chains.append(f'[{cur}]delogo=x={dx}:y={dy}:w={dw}:h={dh}{_enable(L)}[{out}]')
            else:
                a, b, bl = f'a{out}', f'b{out}', f'bl{out}'
                sigma = max(1, int(L.get('strength', 20)))
                chains.append(f'[{cur}]split[{a}][{b}]')
                chains.append(f'[{b}]crop={w}:{h}:{x}:{y},gblur=sigma={sigma}:steps=3[{bl}]')
                chains.append(f'[{a}][{bl}]overlay={x}:{y}{_enable(L)}[{out}]')
            cur = out

        elif L['type'] == 'logo' and L.get('src'):
            idx = len(extra_inputs) + 1
            extra_inputs.append(str(asset_path(L['src'])))
            lg, out = f'lg{i}', nxt()
            op = float(L.get('opacity', 1))
            chains.append(f'[{idx}:v]scale={w}:-1,format=rgba,colorchannelmixer=aa={op:.2f}[{lg}]')
            chains.append(f'[{cur}][{lg}]overlay={x}:{y}:format=auto{_enable(L)}[{out}]')
            cur = out

        elif L['type'] == 'text':
            text = (L.get('text') or '').replace('{title}', title).strip()
            if not text:
                continue
            font = font_path(L.get('font'))
            size = max(8, round(float(L.get('size', 0.05)) * short))  # cỡ chữ theo cạnh ngắn của khung
            text = _wrap(text, font, size, w)
            tf = tmp / f'text{i}.txt'
            tf.write_text(text, encoding='utf-8', newline='\n')  # \r\n sẽ bị drawtext tính là 2 dòng
            align = {'left': 'L', 'right': 'R'}.get(L.get('align'), 'C')
            xe = {'L': f'{x}', 'R': f'{x}+{w}-text_w', 'C': f'{x}+({w}-text_w)/2'}[align]
            opts = [f'fontfile={_esc(font)}', f'textfile={_esc(tf)}', 'expansion=none', f'fontsize={size}',
                    f"fontcolor={_color(L.get('color', '#ffffff'))}", f'text_align={align}',
                    f'line_spacing={round(size * 0.2)}', f"x='{xe}'", f'y={y}']
            if float(L.get('bgOpacity', 0)) > 0:
                opts += ['box=1', f"boxcolor={_color(L.get('bg'), L.get('bgOpacity'))}",
                         f'boxborderw={round(size * 0.3)}']
            if float(L.get('stroke', 0)) > 0:
                opts += [f"borderw={max(1, round(float(L['stroke']) * size))}",
                         f"bordercolor={_color(L.get('strokeColor', '#000000'))}"]
            if float(L.get('shadow', 0)) > 0:
                d = max(1, round(float(L['shadow']) * size))
                opts += [f'shadowx={d}', f'shadowy={d}', f"shadowcolor={_color(L.get('shadowColor', '#000000'), 0.75)}"]
            out = nxt()
            chains.append(f"[{cur}]drawtext={':'.join(opts)}{_enable(L)}[{out}]")
            cur = out

    chains.append(f'[{cur}]format=yuv420p[vout]')
    return extra_inputs, ';'.join(chains), (W, H)


# ---------------------------------------------------------------- Encoder

@lru_cache(maxsize=1)
def video_encoder():
    """Dùng GPU NVIDIA (NVENC) nếu chạy được, không thì CPU (libx264)."""
    try:
        r = subprocess.run([_ffmpeg(), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=s=640x360:d=0.2',
                            '-c:v', 'h264_nvenc', '-f', 'null', '-'], capture_output=True, timeout=30,
                           creationflags=NO_WINDOW)
        if r.returncode == 0:
            return ['-c:v', 'h264_nvenc', '-preset', 'p5', '-tune', 'hq', '-rc', 'vbr', '-cq', '21', '-b:v', '0']
    except (OSError, subprocess.SubprocessError):
        pass
    return ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20']


def encoder_name():
    return 'GPU (NVENC)' if 'h264_nvenc' in video_encoder() else 'CPU'


# ---------------------------------------------------------------- Render

def clean_title(path):
    """Tên file -> tiêu đề: bỏ đuôi và mã ngôn ngữ kiểu ' [vi]'."""
    return re.sub(r'\s*\[[A-Za-z0-9-]+\]$', '', Path(path).stem)


def unique_path(path):
    path = Path(path)
    i = 2
    while path.exists():
        path = path.with_name(f'{path.stem.rsplit(" (", 1)[0]} ({i}){path.suffix}')
        i += 1
    return path


class RenderJob:
    kind = 'render'

    def __init__(self, template_id, src, outdir):
        self.id = uuid.uuid4().hex[:8]
        self.template_id = template_id
        self.src = str(src)
        self.outdir = str(outdir)
        self.title = Path(src).name
        self.status = 'queued'
        self.stage = ''
        self.percent = 0.0
        self.speed = None
        self.eta = None
        self.item = None
        self.error = None
        self.filepath = None
        self.cancel = threading.Event()
        self.proc = None

    def to_dict(self):
        d = {k: getattr(self, k) for k in (
            'id', 'kind', 'title', 'status', 'stage', 'percent', 'speed', 'eta', 'item', 'error', 'filepath')}
        d['url'] = self.src
        return d

    def stop(self):
        self.cancel.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def run_render(job):
    if job.cancel.is_set():
        job.status = 'cancelled'
        return
    tmp = TMP_DIR / job.id
    try:
        tpl = get_template(job.template_id)
        job.stage = tpl.get('name', '')
        info = probe(job.src)
        tmp.mkdir(parents=True, exist_ok=True)
        extra, graph, _ = build_filter(tpl, info['w'], info['h'], clean_title(job.src), tmp)
        Path(job.outdir).mkdir(parents=True, exist_ok=True)
        out = unique_path(Path(job.outdir) / f'{Path(job.src).stem}.mp4')
        job.filepath = str(out)

        cmd = [_ffmpeg(), '-y', '-hide_banner', '-v', 'error', '-nostats', '-progress', 'pipe:1', '-i', job.src]
        for img in extra:
            cmd += ['-i', img]
        cmd += ['-filter_complex', graph, '-map', '[vout]', '-map', '0:a?', *video_encoder(),
                '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(out)]

        job.status = 'processing'
        job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=NO_WINDOW, text=True, encoding='utf-8', errors='replace')
        err_lines = []
        threading.Thread(target=lambda: err_lines.extend(job.proc.stderr), daemon=True).start()
        speed = None
        for line in job.proc.stdout:
            key, _, val = line.strip().partition('=')
            if key == 'out_time_us' and val.isdigit() and info['duration']:
                t = int(val) / 1e6
                job.percent = min(99.9, t * 100 / info['duration'])
                if speed:
                    job.eta = (info['duration'] - t) / speed
            elif key == 'speed' and val.endswith('x'):
                try:
                    speed = float(val[:-1])
                    job.stage = f"{tpl.get('name', '')} · {speed:.1f}x"
                except ValueError:
                    pass
        code = job.proc.wait()
        if job.cancel.is_set():
            job.status = 'cancelled'
            out.unlink(missing_ok=True)
        elif code != 0:
            job.status = 'error'
            job.error = (''.join(err_lines).strip().splitlines() or ['ffmpeg lỗi'])[-1]
            out.unlink(missing_ok=True)
        else:
            job.status, job.percent, job.stage = 'done', 100.0, tpl.get('name', '')
    except Exception as e:  # noqa: BLE001
        job.status = 'cancelled' if job.cancel.is_set() else 'error'
        job.error = job.error or str(e)
    finally:
        job.eta = None
        shutil.rmtree(tmp, ignore_errors=True)
