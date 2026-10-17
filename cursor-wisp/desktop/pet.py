"""A desktop companion. It hangs beside the text caret while you type.

It reads the Cursor chat draft so it can suggest a note before you send.
The draft is kept in memory for that lookup and is never written to the log.
Left-click and drag the body to move it. Right-click nudges it.
Double right-click changes character. Ctrl+Alt+. brings it to the mouse.
Ctrl+Alt+W closes it.

POST /suggestion on 127.0.0.1 pings the pet, then opens a card to its left.
"""

import array
import base64
import ctypes
import io
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
import winsound
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

def _per_monitor_dpi():
    """Real pixels on each screen. One system-wide scale makes the other screen look zoomed."""

    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


_per_monitor_dpi()
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
FRAME = HERE.parent / "overlay" / "wisp-frame.html"
LOG = HERE / "pet.log"
PORT_FILE = HERE / "wisp.port"
ACCEPT_FILE = HERE / "wisp-accept.json"
WIDTH = 300
HEIGHT = 260
OPEN_WIDTH = 560
OPEN_HEIGHT = 280
IGNORE_FOR = 10 * 60
SUGGEST_BODY_MAX = 65536
ALERT_FOR = 0.35


def _notice_wav():
    """Two short rising notes. Bip, then a slightly higher bip."""
    rate = 22050
    total = int(rate * 0.62)
    samples = [0.0] * total

    def tone(t0, dur, f0, f1, amp):
        start = int(t0 * rate)
        count = int(dur * rate)
        for i in range(count):
            idx = start + i
            if idx >= total:
                break
            u = i / count
            env = (u / 0.12) if u < 0.12 else math.exp(-5.4 * (u - 0.12))
            freq = f0 * ((f1 / f0) ** u)
            phase = 2 * math.pi * freq * (i / rate)
            samples[idx] += amp * env * (math.sin(phase) + 0.22 * math.sin(phase * 2))

    tone(0.02, 0.15, 784, 1046, 0.62)
    tone(0.30, 0.18, 1046, 1568, 0.55)
    peak = max(abs(sample) for sample in samples) or 1.0
    pcm = array.array("h")
    for sample in samples:
        pcm.append(int(max(-1.0, min(1.0, sample / peak * 0.62)) * 32000))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return buf.getvalue()


NOTICE_WAV = _notice_wav()


def play_notice():
    def run():
        try:
            winsound.PlaySound(NOTICE_WAV, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except RuntimeError:
            log("notice sound failed")

    threading.Thread(target=run, name="wisp-notice", daemon=True).start()


TYPE_KEYS = (
    list(range(0x30, 0x3A))
    + list(range(0x41, 0x5B))
    + list(range(0x60, 0x6A))
    + list(range(0xBA, 0xC0))
    + [0x08, 0x09, 0x0D, 0x20, 0xBD, 0xBE, 0xBF, 0xDB, 0xDC, 0xDD, 0xDE]
)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", RECT),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetWindow.restype = wintypes.HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.EnumChildWindows.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.keybd_event.restype = None
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_wchar_p,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
KEYEVENTF_KEYUP = 0x0002

HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
GW_OWNER = 4
GWLP_HWNDPARENT = -8
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOREDIRECTIONBITMAP = 0x00200000
PASS_THROUGH = WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOREDIRECTIONBITMAP


def log(message):
    try:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%H:%M:%S ") + message + "\n")
    except OSError:
        pass


class SuggestState:
    """The card the pet is showing. The suggestion text is never written to the log."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.pending = False
        self.open = False
        self.turn_ended = False
        self.summary = ""
        self.quote = ""
        self.source = ""
        self.item_id = ""
        self.ignored = {}

    def blocked(self, item_id):
        with self.lock:
            return self.prune_blocked(item_id, time.time())

    def prune_blocked(self, item_id, now):
        dead = [key for key, until in self.ignored.items() if until <= now]
        for key in dead:
            del self.ignored[key]
        until = self.ignored.get(item_id)
        return until is not None and until > now


suggest_state = SuggestState()
ui_queue = queue.Queue()
card_click = {"hwnd": 0}
draft_watch = None


_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "be", "it", "we",
    "i", "me", "my", "our", "what", "which", "who", "why", "how", "do", "does", "did", "should", "with",
    "about", "this", "that", "from", "at", "by", "as", "us", "you", "your", "can", "will", "would",
    "have", "has", "had", "not", "but", "if", "so", "just", "into", "also",
})


def _content_words(text):
    seen = []
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(word) < 4 or word in _STOP_WORDS or word in seen:
            continue
        seen.append(word)
    return seen


def _related(draft, quote):
    """The draft is about this note when most of its words are in the quote."""

    draft_words = _content_words(draft)
    quote_words = set(_content_words(quote))
    if len(draft_words) < 2 or len(quote_words) < 2:
        return False
    shared = [word for word in draft_words if word in quote_words]
    return len(shared) >= 2 and len(shared) * 2 >= len(draft_words)


def _accepting():
    return pet_ref is not None and pet_ref.accept_latched


def offer_suggestion(data):
    """Show a found cue on the pet. A card already open swaps to the new match."""
    if not isinstance(data, dict) or data.get("state") != "found":
        return False
    item_id = _plain(data.get("item_id"))
    if suggest_state.blocked(item_id):
        return False
    payload = {
        "summary": _plain(data.get("summary")),
        "quote": _plain(data.get("quote")),
        "source": _plain(data.get("source")),
        "item_id": item_id,
    }
    with suggest_state.lock:
        if _accepting():
            return False
        same = (
            item_id
            and item_id == suggest_state.item_id
            and payload["quote"] == suggest_state.quote
            and payload["summary"] == suggest_state.summary
            and (suggest_state.open or suggest_state.pending)
        )
        if same:
            return True
        suggest_state.seq += 1
        payload["seq"] = suggest_state.seq
        suggest_state.pending = True
        suggest_state.turn_ended = False
    ui_queue.put(("show", payload))
    return True


def retire_suggestion():
    """Close the card when the draft no longer matches a stored note."""
    with suggest_state.lock:
        if _accepting() or not (suggest_state.open or suggest_state.pending):
            return
        suggest_state.seq += 1
        suggest_state.pending = False
    ui_queue.put(("hide", None))


def _plain(value):
    if value is None:
        return ""
    return str(value)


class SuggestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def handle_error(self):
        return

    def _empty(self, code):
        self.close_connection = True
        try:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        if length > SUGGEST_BODY_MAX:
            return None
        return self.rfile.read(length)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if len(path) > 1:
            path = path.rstrip("/")
        body = self._read_body()
        if path == "/turn-ended":
            self._turn_ended()
            return
        if path != "/suggestion" or body is None:
            self._empty(204 if path == "/suggestion" else 404)
            return
        self._suggestion(body)

    def _turn_ended(self):
        with suggest_state.lock:
            if suggest_state.open or suggest_state.pending:
                suggest_state.turn_ended = True
        self._empty(204)

    def _suggestion(self, body):
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._empty(204)
            return
        offer_suggestion(data)
        self._empty(204)


def start_suggest_server():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), SuggestHandler)
    except OSError as error:
        log(f"suggest bind failed: {type(error).__name__}")
        return
    port = int(server.server_address[1])
    try:
        PORT_FILE.write_text(str(port), encoding="ascii")
    except OSError as error:
        log(f"port file failed: {type(error).__name__}")
        server.server_close()
        return
    thread = threading.Thread(target=server.serve_forever, name="wisp-suggest", daemon=True)
    thread.start()
    log(f"suggest 127.0.0.1:{port}")


def cleanup_port():
    try:
        PORT_FILE.unlink()
    except OSError:
        pass


def write_accept_file(text, item_id):
    raw = json.dumps({"text": text, "item_id": item_id}, ensure_ascii=False).encode("utf-8")
    tmp = ACCEPT_FILE.with_name("wisp-accept.json.tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, ACCEPT_FILE)


def set_click_through(enabled):
    """WS_EX_TRANSPARENT is on while the pet is alone, off while the card can be clicked."""
    hwnd = Pet.hwnd
    if not hwnd:
        return
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    updated = style
    if enabled:
        updated |= WS_EX_TRANSPARENT
        updated &= ~WS_EX_NOACTIVATE
    else:
        updated &= ~WS_EX_TRANSPARENT
        updated |= WS_EX_NOACTIVATE
    if updated == style:
        return
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, updated)
    user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )


def _exe_name(hwnd):
    if not hwnd:
        return ""
    try:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return Path(buf.value).name
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def _is_cursor(hwnd):
    return _exe_name(hwnd).lower() == "cursor.exe"


def _focus_window(hwnd):
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    current_fg = user32.GetForegroundWindow()
    if current_fg and int(current_fg) == int(hwnd):
        return True
    this_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    other_thread = user32.GetWindowThreadProcessId(current_fg, None) if current_fg else 0
    attached_target = False
    attached_other = False
    try:
        if target_thread and target_thread != this_thread:
            attached_target = bool(user32.AttachThreadInput(this_thread, target_thread, True))
        if other_thread and other_thread not in (this_thread, target_thread):
            attached_other = bool(user32.AttachThreadInput(this_thread, other_thread, True))
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached_other:
            user32.AttachThreadInput(this_thread, other_thread, False)
        if attached_target:
            user32.AttachThreadInput(this_thread, target_thread, False)
    fg = user32.GetForegroundWindow()
    return bool(fg) and int(fg) == int(hwnd)


def send_ctrl_end():
    """Move to the end of the chat draft. Enter would send it."""
    try:
        user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(0x23, 0, 0, 0)
        time.sleep(0.02)
        return True
    except Exception:
        return False
    finally:
        try:
            user32.keybd_event(0x23, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass


def send_ctrl_v():
    """Ctrl+V only. Enter would send the chat."""
    try:
        user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(0x56, 0, 0, 0)
        time.sleep(0.02)
        return True
    except Exception:
        return False
    finally:
        try:
            user32.keybd_event(0x56, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass


def paste_into_cursor():
    try:
        fg = user32.GetForegroundWindow()
        if not _is_cursor(fg):
            saved = card_click.get("hwnd") or 0
            saved_hwnd = wintypes.HWND(int(saved)) if saved else None
            if not saved_hwnd or not _is_cursor(saved_hwnd):
                return False
            if not _focus_window(saved_hwnd):
                return False
        fg = user32.GetForegroundWindow()
        if not _is_cursor(fg):
            return False
        return send_ctrl_v()
    except Exception:
        return False


def own_the_process():
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.restype = wintypes.DWORD
    kernel32.CreateMutexW(None, False, "Local\\WeaveWispDesktop")
    if kernel32.GetLastError() == 183:
        sys.exit(0)


def monitor_scale(x, y):
    """CSS pixels to physical pixels for the screen under this point. 1.0 at 96 DPI."""

    try:
        shcore = ctypes.windll.shcore
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HMONITOR, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
        ]
        shcore.GetDpiForMonitor.restype = ctypes.c_long
        user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        monitor = user32.MonitorFromPoint(POINT(int(x), int(y)), 2)
        dx, dy = wintypes.UINT(), wintypes.UINT()
        if monitor and shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
            return dx.value / 96.0
    except Exception:
        pass
    return 1.0


def window_scale(hwnd=None):
    hwnd = hwnd or Pet.hwnd
    if hwnd:
        dpi = user32.GetDpiForWindow(hwnd)
        if dpi:
            return dpi / 96.0
    return 1.0


def css_px(css, scale):
    return max(1, int(round(css * scale)))


def work_area(x, y):
    monitor = user32.MonitorFromPoint(POINT(int(x), int(y)), 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    rect = info.rcWork
    return rect.left, rect.top, rect.right, rect.bottom


def clamp(x, y, scale=None):
    if scale is None:
        scale = window_scale()
    w, h = css_px(WIDTH, scale), css_px(HEIGHT, scale)
    extra = 0
    if pet_ref is not None and pet_ref.card_open:
        extra = css_px(OPEN_WIDTH - WIDTH, scale)
        h = css_px(OPEN_HEIGHT, scale)
    left, top, right, bottom = work_area(x + w / 2, y + h / 2)
    x = min(max(x, left + extra + 8), right - w - 8)
    y = min(max(y, top + 8), bottom - h - 8)
    return x, y


MAX_FOLLOW_JUMP = 380
AWAY_DIST = 220
LOOK_GRACE = 1.0


def anchor_sane(x, y, height):
    if height > 320 or height < 1:
        return False
    left, top, right, bottom = work_area(x, y)
    if x < left - 40 or x > right + 40 or y < top - 60 or y > bottom + 80:
        return False
    return not (abs(x) < 2 and abs(y) < 2)


def caret_from_win32(our_hwnd):
    foreground = user32.GetForegroundWindow()
    if not foreground or foreground == our_hwnd:
        return None
    thread_id = user32.GetWindowThreadProcessId(foreground, None)
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)) or not info.hwndCaret:
        return None
    rect = info.rcCaret
    height = rect.bottom - rect.top
    if height < 2:
        return None
    point = POINT(rect.left, rect.top)
    if not user32.ClientToScreen(info.hwndCaret, ctypes.byref(point)):
        return None
    anchor = (point.x, point.y, max(height, 16))
    if not anchor_sane(*anchor):
        return None
    return anchor


def release_owner(hwnd):
    """A hidden owner window keeps this one from staying above other apps."""
    owner = user32.GetWindow(hwnd, GW_OWNER)
    if not owner:
        return
    user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, 0)
    log("released owner")


def _window_class(hwnd):
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def fullscreen(our_hwnd):
    """A borderless window that covers the whole monitor, including the taskbar.

    A maximized editor still has a title bar, and on this machine its rectangle
    reaches the screen edges. The desktop window also covers the monitor, and
    must not count.
    """
    foreground = user32.GetForegroundWindow()
    if not foreground or foreground == our_hwnd:
        return False
    exe = _exe_name(foreground).lower()
    if exe in {"", "cursor.exe", "explorer.exe"}:
        return False
    if _window_class(foreground).startswith("Chrome_WidgetWin"):
        return False
    if _window_class(foreground) in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
        return False
    style = user32.GetWindowLongW(foreground, -16)
    if style & 0x00C00000:
        return False
    rect = RECT()
    if not user32.GetWindowRect(foreground, ctypes.byref(rect)):
        return False
    monitor = user32.MonitorFromPoint(POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2), 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    screen = info.rcMonitor
    return (
        rect.left <= screen.left + 2
        and rect.top <= screen.top + 2
        and rect.right >= screen.right - 2
        and rect.bottom >= screen.bottom - 2
    )


class FocusWatch:
    """Finds where text is being edited. Coordinates only."""

    def __init__(self):
        self.point = None
        self.updated = 0.0
        self.last_type = 0.0
        self.keys_held = False
        self.look = None
        self.look_updated = 0.0
        self._lock = threading.Lock()
        self._proc = None
        self._lines = queue.Queue()
        threading.Thread(target=self._loop, name="wisp-focus", daemon=True).start()

    def snapshot(self):
        with self._lock:
            return self.last_type, self.point, self.updated, self.keys_held, self.look, self.look_updated

    def _keys(self):
        if _ctrl_down() and _alt_down():
            with self._lock:
                self.keys_held = False
            return
        edge = False
        held = False
        for vk in TYPE_KEYS:
            state = user32.GetAsyncKeyState(vk)
            if state & 1:
                edge = True
            if state & 0x8000:
                held = True
        if edge or held:
            cursor = POINT()
            if user32.GetCursorPos(ctypes.byref(cursor)):
                with self._lock:
                    self.keys_held = held
                    self.last_type = time.time()
                    self.look = (cursor.x, cursor.y, 18)
                    self.look_updated = self.last_type
                return
        with self._lock:
            self.keys_held = held
            if edge or held:
                self.last_type = time.time()

    def _start_helper(self):
        if self._proc and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "focusrect.ps1")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        threading.Thread(target=self._read_helper, args=(self._proc,), name="wisp-caret", daemon=True).start()

    def _read_helper(self, proc):
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            self._lines.put(raw)

    def _ask(self):
        self._start_helper()
        try:
            self._proc.stdin.write(b"?\n")
            self._proc.stdin.flush()
            raw = self._lines.get(timeout=0.15)
        except (OSError, AttributeError, queue.Empty):
            return None
        if not raw:
            return None
        text = raw.decode("utf-16") if b"\x00" in raw[:8] else raw.decode("utf-8", "replace")
        parts = text.strip().split()
        if len(parts) != 5 or parts[0] not in {"caret", "box"}:
            return None
        try:
            x, y, width, height = (float(part) for part in parts[1:])
        except ValueError:
            return None
        kind = parts[0]
        if kind != "caret":
            return None
        if width > 2000 or height > 400:
            return None
        caret_x = x + min(width, 12)
        anchor = (caret_x, y, max(height, 16))
        if not anchor_sane(*anchor):
            return None
        return anchor

    def _loop(self):
        while True:
            self._keys()
            with self._lock:
                recent = time.time() - self.last_type < 1.4
            need_caret = recent or (pet_ref is not None and pet_ref.pinned)
            if need_caret:
                found = caret_from_win32(Pet.hwnd)
                caret = self._ask()
                if caret:
                    found = caret
                if found and anchor_sane(*found):
                    with self._lock:
                        self.point = found
                        self.look = found
                        self.updated = time.time()
                        self.look_updated = self.updated
            time.sleep(0.12 if recent else 0.05)


class Pet:
    hwnd = None

    def __init__(self, focus):
        self.focus = focus
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        self.x, self.y = self.place_near(point.x, point.y, 20)
        self.tx, self.ty = self.x, self.y
        self.mode = "idle"
        self.jump_from = (self.x, self.y)
        self.jump_to = (self.x, self.y)
        self.jump_started = 0.0
        self.visible = True
        self.look_gx = 0.0
        self.look_gy = 0.0
        self.reading = False
        self.manual = False
        self.pinned = False
        self.block_follow = False
        self.hide_logged = False
        self.card_open = False
        self.reveal_at = 0.0
        self.close_at = 0.0
        self.accept_latched = False
        self.form = None
        self.win_point = None
        self.win_size = None

    def place_near(self, caret_x, caret_y, caret_h):
        scale = monitor_scale(caret_x, caret_y)
        x = caret_x - 20
        y = caret_y - css_px(HEIGHT, scale) - 4
        return clamp(x, y, scale)

    def begin_jump(self, x, y):
        self.mode = "jump"
        self.jump_from = (self.x, self.y)
        self.jump_to = (x, y)
        self.jump_started = time.time()

    def _sync_drag(self):
        if not drag_state["active"]:
            return
        cursor = POINT()
        if not user32.GetCursorPos(ctypes.byref(cursor)):
            cursor.x, cursor.y = drag_state["cur_pt"]
        dx = cursor.x - drag_state["start_pt"][0]
        dy = cursor.y - drag_state["start_pt"][1]
        if dx * dx + dy * dy > 1:
            drag_state["moved"] = True
        self.x, self.y = clamp(
            drag_state["start_xy"][0] + dx,
            drag_state["start_xy"][1] + dy,
        )
        self.tx, self.ty = self.x, self.y
        self.manual = True
        self.mode = "idle"
        self.jump_started = 0.0

    def recall(self, screen_x, screen_y):
        scale = monitor_scale(screen_x, screen_y)
        self.x, self.y = clamp(screen_x - 24, screen_y - css_px(HEIGHT, scale) - 12, scale)
        self.tx, self.ty = self.x, self.y
        self.pinned = False
        self.manual = False
        self.mode = "idle"
        self.visible = True
        self.block_follow = True
        self.jump_started = 0.0
        self.apply()

    def step(self, view=None, look_state=None):
        if drag_state["active"] or drag_state["pending"]:
            return
        now = time.time()
        if self.block_follow:
            _, _, _, keys_held, _, _ = self.focus.snapshot()
            self._refresh_expression(now, view, look_state)
            if keys_held or _ctrl_down() or _alt_down():
                self.x, self.y = clamp(self.x, self.y)
                self.apply()
                return
            self.block_follow = False
        last_type, point, updated, keys_held, _, _ = self.focus.snapshot()
        typing = point and keys_held and now - last_type < 1.35
        if self.manual and not typing:
            self.tx, self.ty = self.x, self.y
            self._refresh_expression(now, view, look_state)
            self.x, self.y = clamp(self.x, self.y)
            self.apply()
            return
        if typing and not self.pinned:
            self.manual = False
            tx, ty = self.place_near(*point)
            distance = math.hypot(tx - self.x, ty - self.y)
            if distance > MAX_FOLLOW_JUMP:
                self._refresh_expression(now, view, look_state)
                self.x, self.y = clamp(self.x, self.y)
                self.apply()
                return
            if self.mode != "jump":
                if distance > 120:
                    self.begin_jump(tx, ty)
                else:
                    self.mode = "follow"
                    self.tx, self.ty = tx, ty
            self._refresh_expression(now, view, look_state)
        elif typing and self.pinned:
            self._refresh_expression(now, view, look_state)
            self.x, self.y = clamp(self.x, self.y)
            self.apply()
            return
        elif self.mode == "follow" and not typing:
            self.mode = "idle"
            self.tx, self.ty = self.x, self.y
            self._refresh_expression(now, view, look_state)
        elif not typing:
            self._refresh_expression(now, view, look_state)

        if self.mode == "jump":
            t = min(1.0, (now - self.jump_started) / 0.42)
            ease = 1 - (1 - t) ** 3
            self.x = self.jump_from[0] + (self.jump_to[0] - self.jump_from[0]) * ease
            hop = math.sin(math.pi * t) * 54
            self.y = self.jump_from[1] + (self.jump_to[1] - self.jump_from[1]) * ease - hop
            if t >= 1:
                self.mode = "follow" if typing else "idle"
                self.x, self.y = self.jump_to
        elif self.mode in {"follow", "idle"}:
            rate = 7.5 if self.mode == "follow" else 0.0
            if rate:
                blend = min(1.0, 0.016 * rate)
                self.x += (self.tx - self.x) * blend
                self.y += (self.ty - self.y) * blend

        self.x, self.y = clamp(self.x, self.y)
        self._refresh_expression(now, view, look_state)
        self.apply()

    def _refresh_expression(self, now, view, look_state):
        if look_state is None or view is None or view.CoreWebView2 is None:
            return
        last_type, _point, _updated, keys_held, look, look_updated = self.focus.snapshot()
        typing = keys_held and (now - last_type) < 1.35
        fresh = look and ((now - last_type) < 1.6 or (now - look_updated) < 1.6 or self.pinned)
        if not fresh:
            mood = "sad" if self.pinned else ""
            self._send_expression(view, look_state, 0.0, 0.6 if self.pinned else 0.0, False, mood, 1)
            return
        cx = self.x + WIDTH / 2
        cy = self.y + HEIGHT / 2
        gx = max(-1.0, min(1.0, (look[0] - cx) / 160.0))
        gy = max(-1.0, min(1.0, (look[1] - cy) / 120.0))
        dist = math.hypot(look[0] - cx, look[1] - cy)
        away = self.pinned and dist > AWAY_DIST
        recent = (now - last_type) < 1.2
        if gx < -0.12:
            flip = -1
        elif gx > 0.12:
            flip = 1
        else:
            flip = look_state.get("flip", 1)
        if away and (typing or recent):
            mood = "peek"
            reading = False
        elif away or (self.pinned and not recent):
            mood = "sad"
            reading = False
            gy = min(1.0, 0.55 + gy * 0.25)
        else:
            mood = ""
            reading = typing or recent
        self._send_expression(view, look_state, gx, gy, reading, mood, flip)

    def _send_expression(self, view, look_state, gx, gy, reading, mood, flip):
        if look_state.get("reading") != reading:
            look_state["reading"] = reading
            view.CoreWebView2.ExecuteScriptAsync(f"weaveReading({json.dumps(reading)})")
        if look_state.get("mood") != mood or look_state.get("flip") != flip:
            look_state["mood"] = mood
            look_state["flip"] = flip
            view.CoreWebView2.ExecuteScriptAsync(
                f"weaveMood({json.dumps(mood)},{flip})"
            )
            log(f"mood {mood or 'clear'} flip {flip}")
        if (
            abs(gx - look_state.get("gx", 0.0)) > 0.03
            or abs(gy - look_state.get("gy", 0.0)) > 0.03
        ):
            look_state["gx"] = gx
            look_state["gy"] = gy
            view.CoreWebView2.ExecuteScriptAsync(f"weaveLook({gx:.3f},{gy:.3f})")

    def apply(self):
        hwnd = Pet.hwnd
        if not hwnd:
            return
        covered = fullscreen(hwnd)
        on_screen = user32.IsWindowVisible(hwnd)
        if covered and on_screen:
            user32.ShowWindow(hwnd, SW_HIDE)
            self.visible = False
            if not self.hide_logged:
                self.hide_logged = True
                log(f"hide fullscreen {_exe_name(user32.GetForegroundWindow())} {_window_class(user32.GetForegroundWindow())}")
        elif not covered:
            self.hide_logged = False
            if click_hole["down"]:
                self.visible = False
            elif not on_screen or not self.visible:
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                self.visible = True
        if self.visible and not click_hole["down"]:
            rect = RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return
            nx = int(self.x)
            ny = int(self.y)
            scale = window_scale(hwnd)
            tw, th = css_px(WIDTH, scale), css_px(HEIGHT, scale)
            if self.card_open:
                # The pet slot stays at (self.x, self.y). The extra width hangs to its left.
                nx -= css_px(OPEN_WIDTH - WIDTH, scale)
                tw, th = css_px(OPEN_WIDTH, scale), css_px(OPEN_HEIGHT, scale)
            if getattr(self, "_scale_logged", None) != scale:
                self._scale_logged = scale
                log(f"dpi {scale:.2f} {tw}x{th}")
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            needs_top = not (style & 0x8)
            needs_move = abs(rect.left - nx) > 1 or abs(rect.top - ny) > 1
            needs_size = abs(w - tw) > 2 or abs(h - th) > 2
            if not (needs_top or needs_move or needs_size):
                return
            flags = SWP_NOACTIVATE
            if not needs_move:
                flags |= SWP_NOMOVE
            if not needs_size:
                flags |= SWP_NOSIZE
            if needs_top:
                flags |= SWP_FRAMECHANGED
            if needs_size and self.form is not None and self.win_point is not None and self.win_size is not None:
                self.form.Location = self.win_point(nx, ny)
                self.form.ClientSize = self.win_size(tw, th)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, nx, ny, tw, th, flags)

    def bind(self):
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value == "Wisp":
                    found.append(hwnd)
            return True

        user32.EnumWindows.argtypes = [type(visit), wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.EnumWindows(visit, 0)
        if not found:
            return False
        hwnd = found[-1]
        Pet.hwnd = hwnd
        log(f"window {hwnd} style {user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF:08x}")
        return True


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


WH_MOUSE_LL = 14
_BUTTON_DOWN = (0x0201, 0x0204, 0x0207)
_BUTTON_UP = (0x0202, 0x0205, 0x0208)
click_hole = {"down": False, "show_at": 0.0, "cycle": False, "poke": False}
ink_rect = {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0, "ready": False}
drag_state = {
    "active": False,
    "pending": False,
    "moved": False,
    "up": False,
    "start_pt": (0, 0),
    "start_xy": (0.0, 0.0),
    "cur_pt": (0, 0),
}
pet_ref = None
BODY_INSET = 0.1
RIGHT_DBL_GAP = 0.55
RIGHT_DBL_DIST = 28
right_click = {
    "last_down": 0.0,
    "last_pt": (0, 0),
    "pending_poke": False,
    "block_up_poke": False,
}
_mouse_proc = None
_mouse_hook = None
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t


def _over_pet(x, y):
    hwnd = Pet.hwnd
    if not hwnd:
        return False
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    return rect.left <= x < rect.right and rect.top <= y < rect.bottom


def _on_pet_body(x, y):
    """The draggable pet. The card occupies the rest of the window while it is open."""
    if pet_ref is not None and pet_ref.card_open:
        left = int(pet_ref.x)
        top = int(pet_ref.y)
        scale = window_scale()
        return left <= x < left + css_px(WIDTH, scale) and top <= y < top + css_px(HEIGHT, scale)
    return _over_pet(x, y)


def _body_hit(screen_x, screen_y):
    """Screen-space hit test so DPI scaling cannot shrink the draggable area to nothing."""
    hwnd = Pet.hwnd
    if not hwnd:
        return False
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    inset_x = max(8, int(width * BODY_INSET))
    inset_y = max(8, int(height * BODY_INSET))
    return (
        rect.left + inset_x <= screen_x < rect.right - inset_x
        and rect.top + inset_y <= screen_y < rect.bottom - inset_y
    )


pointer_edge = {"left": False, "recall": False}


def _apply_drag(pet):
    """Move on the UI thread. The hook only records the press and the release."""
    if not drag_state["active"]:
        return False
    pet._sync_drag()
    pet.apply()
    if not drag_state["up"]:
        return True
    if drag_state["moved"]:
        pet.pinned = True
    pet.manual = True
    drag_state["active"] = False
    drag_state["pending"] = False
    drag_state["moved"] = False
    drag_state["up"] = False
    pet.apply()
    return False


@ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
def _mouse_hook_proc(nCode, wParam, lParam):
    if nCode >= 0 and Pet.hwnd:
        info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if (
            wParam == 0x0204
            and pet_ref is not None
            and pet_ref.card_open
            and _over_pet(info.pt.x, info.pt.y)
            and not _on_pet_body(info.pt.x, info.pt.y)
        ):
            return 1
        if wParam == 0x0204 and _on_pet_body(info.pt.x, info.pt.y):
            now = time.time()
            lx, ly = right_click["last_pt"]
            dx = info.pt.x - lx
            dy = info.pt.y - ly
            if (
                right_click["last_down"] > 0
                and now - right_click["last_down"] < RIGHT_DBL_GAP
                and (dx * dx + dy * dy) < RIGHT_DBL_DIST * RIGHT_DBL_DIST
            ):
                click_hole["cycle"] = True
                right_click["pending_poke"] = False
                right_click["block_up_poke"] = True
                right_click["last_down"] = 0.0
            else:
                right_click["last_down"] = now
                right_click["last_pt"] = (info.pt.x, info.pt.y)
                right_click["pending_poke"] = True
                right_click["block_up_poke"] = False
            return 1
        if wParam == 0x0205 and _on_pet_body(info.pt.x, info.pt.y):
            if right_click["pending_poke"] and not right_click["block_up_poke"]:
                click_hole["poke"] = True
            right_click["pending_poke"] = False
            right_click["block_up_poke"] = False
            return 1
        if wParam == 0x0200 and drag_state["active"]:
            drag_state["cur_pt"] = (info.pt.x, info.pt.y)
        if (
            wParam == 0x0201
            and pet_ref is not None
            and pet_ref.card_open
            and _over_pet(info.pt.x, info.pt.y)
            and not _on_pet_body(info.pt.x, info.pt.y)
        ):
            fg = user32.GetForegroundWindow()
            if fg and int(fg) != int(Pet.hwnd or 0):
                card_click["hwnd"] = int(fg)
        if wParam == 0x0201 and _on_pet_body(info.pt.x, info.pt.y):
            drag_state["active"] = True
            drag_state["pending"] = False
            drag_state["moved"] = False
            drag_state["up"] = False
            drag_state["start_pt"] = (info.pt.x, info.pt.y)
            drag_state["cur_pt"] = (info.pt.x, info.pt.y)
            if pet_ref is not None:
                drag_state["start_xy"] = (pet_ref.x, pet_ref.y)
            log("drag")
            return 1
        if wParam == 0x0202 and drag_state["active"]:
            drag_state["cur_pt"] = (info.pt.x, info.pt.y)
            drag_state["up"] = True
            return 1
        elif wParam == 0x0207 and _on_pet_body(info.pt.x, info.pt.y):
            user32.ShowWindow(Pet.hwnd, SW_HIDE)
            click_hole["down"] = True
            click_hole["show_at"] = 0.0
        elif wParam in _BUTTON_UP and click_hole["down"]:
            click_hole["down"] = False
            click_hole["show_at"] = time.time() + 0.06
    return user32.CallNextHookEx(_mouse_hook, nCode, wParam, lParam)


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


def _hook_loop():
    global _mouse_proc, _mouse_hook
    _mouse_proc = _mouse_hook_proc
    _mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, _mouse_proc, None, 0)
    if not _mouse_hook:
        log("click hook failed")
        return
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    message = MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _ctrl_down():
    return key_down(0x11) or key_down(0xA2) or key_down(0xA3)


def _alt_down():
    return key_down(0x12) or key_down(0xA4) or key_down(0xA5)


def chord(key):
    return _ctrl_down() and _alt_down() and key_down(key)


def chord_press(key):
    ctrl = key_down(0x11) or key_down(0xA2) or key_down(0xA3)
    alt = key_down(0x12) or key_down(0xA4) or key_down(0xA5)
    return ctrl and alt and bool(user32.GetAsyncKeyState(key) & 1)


def run_form():
    import clr
    import webview

    lib = Path(webview.__file__).resolve().parent / "lib"
    os.environ["Path"] = str(lib / "runtimes" / "win-x64" / "native") + os.pathsep + os.environ.get("Path", "")
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    clr.AddReference(str(lib / "Microsoft.Web.WebView2.Core.dll"))
    clr.AddReference(str(lib / "Microsoft.Web.WebView2.WinForms.dll"))
    import System.Windows.Forms as WinForms
    from System.Drawing import Color, Point, Size

    noted = {"tick": False, "hook": False, "error": False}

    def on_ui_error(sender, args):
        if not noted["error"]:
            noted["error"] = True
            log(f"ui error: {args.Exception.Message}")

    WinForms.Application.SetUnhandledExceptionMode(WinForms.UnhandledExceptionMode.CatchException)
    WinForms.Application.ThreadException += on_ui_error
    from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties

    clr.AddReference(str(HERE / "PetForm.dll"))
    from WeaveWisp import AlphaView, PetForm

    html = FRAME.read_text(encoding="utf-8")
    form = PetForm()
    form.Text = "Wisp"
    form.FormBorderStyle = getattr(WinForms.FormBorderStyle, "None")
    form.ShowInTaskbar = False
    form.TopMost = True
    form.StartPosition = WinForms.FormStartPosition.Manual
    form.AutoScaleMode = getattr(WinForms.AutoScaleMode, "None")
    point = POINT()
    user32.GetCursorPos(ctypes.byref(point))
    scale = monitor_scale(point.x, point.y)
    form.ClientSize = Size(css_px(WIDTH, scale), css_px(HEIGHT, scale))
    spot = Pet(None).place_near(point.x, point.y, 20)
    form.Location = Point(int(spot[0]), int(spot[1]))
    hwnd = wintypes.HWND(int(form.Handle.ToInt64()))

    view = AlphaView()
    props = CoreWebView2CreationProperties()
    props.UserDataFolder = str(HERE / "webview-cache")
    view.CreationProperties = props
    view.Dock = WinForms.DockStyle.Fill
    view.DefaultBackgroundColor = Color.Transparent
    form.Controls.Add(view)
    form.Show()

    def on_ready(sender, args):
        try:
            if args.IsSuccess:
                sender.CoreWebView2.Settings.IsWebMessageEnabled = True
                sender.CoreWebView2.WebMessageReceived += on_message
                sender.CoreWebView2.NavigateToString(html)
                log("page ready")
            else:
                log(f"webview failed: {args.InitializationException}")
        except Exception as error:
            log(f"ready failed: {error}")

    view.CoreWebView2InitializationCompleted += on_ready
    painted = {"ok": False}

    def on_nav(sender, args):
        painted["ok"] = True
        log("visible")
        if view.CoreWebView2 is not None:
            view.CoreWebView2.ExecuteScriptAsync("reportInk()")

    def on_message(sender, args):
        try:
            data = json.loads(args.WebMessageAsJson)
            if isinstance(data, str):
                data = json.loads(data)
            if not isinstance(data, dict):
                return
            kind = data.get("type")
            if kind == "ink":
                ink_rect["left"] = float(data["left"])
                ink_rect["top"] = float(data["top"])
                ink_rect["right"] = float(data["right"])
                ink_rect["bottom"] = float(data["bottom"])
                ink_rect["ready"] = ink_rect["right"] > ink_rect["left"] and ink_rect["bottom"] > ink_rect["top"]
                return
            if kind == "accept":
                on_accept(data)
                return
            if kind == "ignore":
                on_ignore(data)
                return
        except Exception as error:
            log(f"message {type(error).__name__}")

    view.NavigationCompleted += on_nav
    Pet.hwnd = hwnd
    release_owner(hwnd)
    set_click_through(True)

    focus = FocusWatch()
    pet = Pet(focus)
    pet.x, pet.y = spot
    pet.form = form
    pet.win_point = Point
    pet.win_size = Size
    global pet_ref
    pet_ref = pet
    look_state = {"gx": 0.0, "gy": 0.0, "reading": False, "mood": "", "flip": 1}

    def push_suggestion(summary, quote, source, item_id):
        if view.CoreWebView2 is None:
            return
        message = json.dumps(
            {"summary": summary, "quote": quote, "source": source, "item_id": item_id}
        )
        view.CoreWebView2.ExecuteScriptAsync("weaveSuggest(" + message + ")")

    def open_card(payload):
        with suggest_state.lock:
            if payload.get("seq") != suggest_state.seq:
                return
            if pet.accept_latched:
                suggest_state.pending = False
                return
            if suggest_state.prune_blocked(payload.get("item_id") or "", time.time()):
                suggest_state.pending = False
                return
            already = suggest_state.open
            suggest_state.pending = False
            suggest_state.open = True
            suggest_state.summary = payload.get("summary") or ""
            suggest_state.quote = payload.get("quote") or ""
            suggest_state.source = payload.get("source") or ""
            suggest_state.item_id = payload.get("item_id") or ""
            summary = suggest_state.summary
            quote = suggest_state.quote
            source = suggest_state.source
            item_id = suggest_state.item_id
        if already and pet.card_open:
            message = json.dumps(
                {"summary": summary, "quote": quote, "source": source, "item_id": item_id}
            )
            view.CoreWebView2.ExecuteScriptAsync("weaveSuggestReplace(" + message + ")")
            log("card update")
            return
        pet.card_open = False
        pet.reveal_at = time.time() + ALERT_FOR
        pet.close_at = 0.0
        pet.accept_latched = False
        set_click_through(True)
        pet.apply()
        push_suggestion(summary, quote, source, item_id)
        if not already:
            play_notice()
        log("card alert")

    def reveal_card():
        pet.reveal_at = 0.0
        with suggest_state.lock:
            if not suggest_state.open:
                return
        pet.card_open = True
        set_click_through(False)
        pet.apply()
        if view.CoreWebView2 is not None:
            view.CoreWebView2.ExecuteScriptAsync("weaveSuggestReveal()")
        log("card open")

    def close_card():
        with suggest_state.lock:
            suggest_state.open = False
            if not suggest_state.pending:
                suggest_state.turn_ended = False
        pet.card_open = False
        pet.reveal_at = 0.0
        pet.close_at = 0.0
        pet.accept_latched = False
        set_click_through(True)
        if view.CoreWebView2 is not None:
            view.CoreWebView2.ExecuteScriptAsync("weaveSuggestClose()")
        pet.apply()
        log("card close")

    def drain_cards():
        if not painted["ok"] or view.CoreWebView2 is None:
            return
        while True:
            try:
                kind, payload = ui_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "hide":
                if not pet.accept_latched:
                    close_card()
                continue
            if kind == "show":
                try:
                    open_card(payload)
                except Exception as error:
                    log(f"card open failed: {type(error).__name__}")

    def copy_clipboard(text):
        try:
            WinForms.Clipboard.SetText(text)
            return True
        except Exception:
            return False

    def on_accept(_data):
        if not pet.card_open or pet.accept_latched:
            return
        pet.accept_latched = True
        with suggest_state.lock:
            text = f"{suggest_state.summary}\n{suggest_state.source}: {suggest_state.quote}"
            item_id = suggest_state.item_id
            ended = suggest_state.turn_ended
            suggest_state.open = False
        inserted = draft_watch.append(text) if draft_watch is not None else False
        if not inserted and draft_watch is not None and draft_watch.focus():
            copied = copy_clipboard("\n" + text)
            if copied:
                send_ctrl_end()
                inserted = paste_into_cursor()
        if inserted:
            note = "Added to the prompt you are typing"
            if view.CoreWebView2 is not None:
                view.CoreWebView2.ExecuteScriptAsync("weaveSuggestStatus(" + json.dumps(note) + ")")
            pet.close_at = time.time() + 1.2
            log("accept draft")
            return
        if not ended:
            try:
                write_accept_file(text, item_id)
                log("accept file")
            except OSError:
                log("accept file failed")
            close_card()
            return
        copied = copy_clipboard(text)
        pasted = paste_into_cursor() if copied else False
        note = "Pasted into the chat box" if pasted else "Copied \u2014 paste it into the chat"
        if view.CoreWebView2 is not None:
            view.CoreWebView2.ExecuteScriptAsync("weaveSuggestStatus(" + json.dumps(note) + ")")
        pet.close_at = time.time() + 1.2
        log("accept paste" if pasted else "accept copy")

    def on_ignore(data):
        item_id = ""
        if isinstance(data, dict) and data.get("item_id") is not None:
            item_id = str(data.get("item_id"))
        if not item_id:
            with suggest_state.lock:
                item_id = suggest_state.item_id
        if item_id:
            with suggest_state.lock:
                suggest_state.ignored[item_id] = time.time() + IGNORE_FOR
            log("card ignore")
        if pet.card_open:
            close_card()

    view.EnsureCoreWebView2Async(None)
    log(f"window {hwnd} style {user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF:08x}")

    def tick(sender, event):
        try:
            drain_cards()
            if not noted["hook"]:
                noted["hook"] = True
                log("ticking")
                threading.Thread(target=_hook_loop, name="wisp-click", daemon=True).start()
            if chord(0x57):
                cleanup_port()
                os._exit(0)
            recall_down = chord(0xBE) or chord(0x6E)
            if recall_down and not pointer_edge["recall"]:
                cursor = POINT()
                user32.GetCursorPos(ctypes.byref(cursor))
                pet.recall(cursor.x, cursor.y)
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                log(f"recall {cursor.x} {cursor.y}")
            pointer_edge["recall"] = recall_down
            if _apply_drag(pet):
                return
            if pet.reveal_at and time.time() >= pet.reveal_at:
                reveal_card()
            if pet.close_at and time.time() >= pet.close_at:
                close_card()
            if click_hole["cycle"] and view.CoreWebView2 is not None:
                click_hole["cycle"] = False
                view.CoreWebView2.ExecuteScriptAsync("weaveNext()")
                log("next character")
            if click_hole["poke"] and view.CoreWebView2 is not None:
                click_hole["poke"] = False
                view.CoreWebView2.ExecuteScriptAsync("weavePoke()")
            if click_hole["show_at"] and not click_hole["down"] and time.time() >= click_hole["show_at"]:
                click_hole["show_at"] = 0.0
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                pet.visible = True
            pet.step(view, look_state)
        except Exception as error:
            if not noted["error"]:
                noted["error"] = True
                log(f"tick failed: {error}")

    timer = WinForms.Timer()
    timer.Interval = 16
    timer.Tick += tick
    timer.Start()
    WinForms.Application.Run()


def _cue_from_answer(card):
    """Turn an answer card into the pet dialogue. No evidence means no card."""

    def clean(value, limit):
        text = " ".join(str(value or "").replace("\ufeff", "").split())
        if len(text) <= limit:
            return text
        return text[:limit].rstrip()

    answer = clean(card.get("answer"), 500)
    evidence = card.get("evidence") if isinstance(card.get("evidence"), dict) else {}
    item_id = ""
    for claim in card.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        ids = claim.get("evidence") or []
        if ids:
            item_id = str(ids[0])
            break
    if not item_id and evidence:
        item_id = str(next(iter(evidence)))
    entry = evidence.get(item_id) if item_id else None
    quote = ""
    source = ""
    if isinstance(entry, dict):
        quote = clean(entry.get("quote") or entry.get("summary"), 180)
        source = clean(entry.get("source"), 80)
    if not answer or not item_id:
        return {"state": "unsure", "summary": "", "quote": "", "source": "", "item_id": ""}
    return {"state": "found", "summary": answer, "quote": quote, "source": source, "item_id": item_id}


def _same_draft(cued, current):
    """True when the box still holds the question that was looked up."""

    def norm(value):
        return " ".join((value or "").replace("\ufeff", "").split()).casefold()

    left, right = norm(cued), norm(current)
    if len(right) < 12:
        return False
    if left == right or right.startswith(left) or left.startswith(right):
        return True
    return len(left) >= 12 and (left[:48] in right or right[:48] in left)


class DraftWatch:
    """Reads the Cursor chat box and asks for a note before the prompt is sent."""

    def __init__(self):
        self._proc = None
        self._lines = queue.Queue()
        self._lock = threading.Lock()
        self._token = ""
        self._cue_url = "http://127.0.0.1:8765/wisp/cue"
        self._missed = False
        threading.Thread(target=self._warm, name="wisp-warm", daemon=True).start()
        threading.Thread(target=self._loop, name="wisp-draft", daemon=True).start()

    def _load_token(self):
        if self._token:
            return self._token
        self._load_local_config()
        if self._token:
            return self._token
        for name in (".env.local", ".env", ".env.tokens"):
            path = REPO / name
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        self._token = os.environ.get("WEAVE_VIEWER_TOKEN") or os.environ.get("WEAVE_TOKEN_SAM") or ""
        if os.environ.get("WEAVE_CUE_URL") and self._cue_url == "http://127.0.0.1:8765/wisp/cue":
            self._cue_url = os.environ["WEAVE_CUE_URL"]
        return self._token

    def _load_local_config(self):
        path = Path(__file__).with_name("pet.config.json")
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        if data.get("token"):
            self._token = str(data["token"])
        if data.get("cue_url"):
            self._cue_url = str(data["cue_url"])

    def _start(self):
        if self._proc and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "composer.ps1")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        threading.Thread(target=self._read, args=(self._proc,), name="wisp-draft-read", daemon=True).start()

    def _read(self, proc):
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            self._lines.put(raw)

    def _drop(self):
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        while True:
            try:
                self._lines.get_nowait()
            except queue.Empty:
                break

    def _call(self, command, timeout):
        with self._lock:
            self._start()
            try:
                self._proc.stdin.write((command + "\n").encode("utf-8"))
                self._proc.stdin.flush()
                raw = self._lines.get(timeout=timeout)
            except (OSError, AttributeError, queue.Empty):
                self._drop()
                return ""
        if not raw:
            return ""
        if b"\x00" in raw[:8]:
            return raw.decode("utf-16", "replace").strip()
        return raw.decode("utf-8", "replace").strip()

    def read(self):
        line = self._call("read", 30)
        if not line.startswith("b64:"):
            if not self._missed:
                self._missed = True
                log("draft miss")
            return None
        self._missed = False
        try:
            return base64.b64decode(line[4:], validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            return None

    def focus(self):
        return self._call("focus", 12) == "ok"

    def append(self, text):
        raw = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return self._call("append " + raw, 12) == "set"

    def _post_json(self, url, payload, timeout):
        token = self._load_token()
        if not token:
            return None
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, UnicodeError, json.JSONDecodeError, TimeoutError):
            return None
        return data if isinstance(data, dict) else None

    def fast_cue(self, prompt, timeout=2):
        """Stored excerpt for the card. The caller is not waiting on a model."""
        return self._post_json(self._cue_url, {"prompt": prompt}, timeout)

    def _warm(self):
        started = time.perf_counter()
        self.fast_cue("weave", timeout=12)
        log(f"draft warm {int((time.perf_counter() - started) * 1000)}")

    def _loop(self):
        log("draft watch")
        last = None
        stable_since = 0.0
        last_cued = None
        while True:
            text = self.read()
            now = time.time()
            if text is None:
                time.sleep(0.3)
                continue
            if text != last:
                last = text
                stable_since = now
                time.sleep(0.1)
                continue
            if now - stable_since < 0.2:
                time.sleep(0.1)
                continue
            cleaned = text.replace("\ufeff", "").strip()
            if len(cleaned) < 12 or len(cleaned) > 1500:
                if len(cleaned) < 12:
                    retire_suggestion()
                time.sleep(0.3)
                continue
            if cleaned == last_cued:
                time.sleep(0.3)
                continue
            last_cued = cleaned
            started = time.perf_counter()
            quick = self.fast_cue(cleaned)
            elapsed = int((time.perf_counter() - started) * 1000)
            current = self.read()
            if current is None or " ".join(current.replace("\ufeff", "").split()) != " ".join(cleaned.split()):
                last_cued = None
                continue
            if quick is None:
                log(f"draft down {elapsed}")
                continue
            related = quick.get("state") == "found" and _related(cleaned, quick.get("quote"))
            if related and offer_suggestion(quick):
                log(f"draft shown {elapsed}")
            else:
                retire_suggestion()
                log(f"draft unsure {elapsed}")
            time.sleep(0.1)


def main():
    global draft_watch
    own_the_process()
    log("started")
    start_suggest_server()
    draft_watch = DraftWatch()
    try:
        import clr
        from System.Threading import ApartmentState, Thread, ThreadStart

        thread = Thread(ThreadStart(run_form))
        thread.SetApartmentState(ApartmentState.STA)
        thread.Start()
        thread.Join()
    finally:
        cleanup_port()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"failed: {error}")
        raise
