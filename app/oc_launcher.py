"""Chạy OpenCut (trình chỉnh sửa video trong D:\\ytb\\opencut) ở chế độ nền cho app.

Build trước bằng build_opencut.ps1.
"""
import ctypes
import os
import subprocess
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
SERVER_JS = ROOT / 'opencut' / 'apps' / 'web' / '.next' / 'standalone' / 'apps' / 'web' / 'server.js'
NODE = ROOT / '_tools' / 'node' / 'node.exe'
# Cổng cố định: OpenCut lưu project trong IndexedDB theo địa chỉ trang, đổi cổng là mất project
HOST, PORT = '127.0.0.1', 3917
URL = f'http://{HOST}:{PORT}'

_proc = None
_job = None
_lock = threading.Lock()


def is_built():
    return SERVER_JS.is_file()


def _healthy():
    try:
        with urllib.request.urlopen(URL + '/api/health', timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


# ---------------------------------------------------------------- Job Object: tắt app là tắt luôn OpenCut

class _BasicLimit(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimit), ('IoInfo', _IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


def _kill_with_app(proc):
    global _job
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    if _job is None:
        _job = k32.CreateJobObjectW(None, None)
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(_job, 9, ctypes.byref(info), ctypes.sizeof(info))  # JobObjectExtendedLimitInformation
    k32.AssignProcessToJobObject(_job, int(proc._handle))


# ---------------------------------------------------------------- Bật / tắt

def ensure_running(timeout=40):
    """Bật OpenCut nếu chưa chạy, trả về địa chỉ khi đã sẵn sàng."""
    global _proc
    with _lock:
        if _healthy():
            return URL
        if not is_built():
            raise RuntimeError(f'Chưa build OpenCut. Chạy {APP_DIR / "build_opencut.ps1"} trước.')
        if _proc is None or _proc.poll() is not None:
            env = {**os.environ, 'PORT': str(PORT), 'HOSTNAME': HOST, 'NODE_ENV': 'production',
                   'NEXT_TELEMETRY_DISABLED': '1'}
            log = open(APP_DIR / 'opencut.log', 'a', encoding='utf-8')
            _proc = subprocess.Popen([str(NODE), str(SERVER_JS)], cwd=SERVER_JS.parent, env=env,
                                     stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
            _kill_with_app(_proc)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _healthy():
                return URL
            if _proc.poll() is not None:
                raise RuntimeError(f'OpenCut không khởi động được, xem {APP_DIR / "opencut.log"}')
            time.sleep(0.3)
        raise RuntimeError('OpenCut khởi động quá lâu')


def stop():
    if _proc and _proc.poll() is None:
        _proc.terminate()
