"""A desktop companion. It hangs beside the text caret while you type.

It notices that keys were pressed. It does not read or store what you typed.
Left-click and drag the body to move it. Right-click nudges it.
Double right-click changes character. Ctrl+Alt+. brings it to the mouse.
Ctrl+Alt+W closes it.
"""

import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
if not user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
    user32.SetProcessDPIAware()

HERE = Path(__file__).resolve().parent
FRAME = HERE.parent / "overlay" / "wisp-frame.html"
LOG = HERE / "pet.log"
WIDTH = 300
HEIGHT = 260

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


def own_the_process():
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.restype = wintypes.DWORD
    kernel32.CreateMutexW(None, False, "Local\\WeaveWispDesktop")
    if kernel32.GetLastError() == 183:
        sys.exit(0)


def work_area(x, y):
    monitor = user32.MonitorFromPoint(POINT(int(x), int(y)), 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    rect = info.rcWork
    return rect.left, rect.top, rect.right, rect.bottom


def clamp(x, y):
    left, top, right, bottom = work_area(x + WIDTH / 2, y + HEIGHT / 2)
    x = min(max(x, left + 8), right - WIDTH - 8)
    y = min(max(y, top + 8), bottom - HEIGHT - 8)
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
    """A real full-screen app covers the whole monitor, including the taskbar.

    A maximized window stops above the taskbar, so the companion stays visible.
    The desktop window also covers the monitor, and must not count.
    """
    foreground = user32.GetForegroundWindow()
    if not foreground or foreground == our_hwnd:
        return False
    if _window_class(foreground) in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
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

    def place_near(self, caret_x, caret_y, caret_h):
        x = caret_x - 20
        y = caret_y - HEIGHT - 4
        return clamp(x, y)

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
        self.x, self.y = clamp(screen_x - 24, screen_y - HEIGHT - 12)
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
        if away and typing:
            mood = "peek"
            reading = False
            flip = -1 if look[0] < cx else 1
        elif away or (self.pinned and not typing):
            mood = "sad"
            reading = False
            flip = 1
            gx *= 0.25
            gy = 0.8
        else:
            mood = ""
            reading = typing
            flip = 1
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
                log("hide fullscreen")
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
            nx, ny = int(self.x), int(self.y)
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            needs_top = not (style & 0x8)
            needs_move = abs(rect.left - nx) > 1 or abs(rect.top - ny) > 1
            needs_size = abs(w - WIDTH) > 2 or abs(h - HEIGHT) > 2
            if not (needs_top or needs_move or needs_size):
                return
            flags = SWP_NOACTIVATE
            if not needs_move:
                flags |= SWP_NOMOVE
            if not needs_size:
                flags |= SWP_NOSIZE
            if needs_top:
                flags |= SWP_FRAMECHANGED
            user32.SetWindowPos(hwnd, HWND_TOPMOST, nx, ny, WIDTH, HEIGHT, flags)

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
        if wParam == 0x0204 and _over_pet(info.pt.x, info.pt.y):
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
        if wParam == 0x0205 and _over_pet(info.pt.x, info.pt.y):
            if right_click["pending_poke"] and not right_click["block_up_poke"]:
                click_hole["poke"] = True
            right_click["pending_poke"] = False
            right_click["block_up_poke"] = False
            return 1
        if wParam == 0x0200 and drag_state["active"]:
            drag_state["cur_pt"] = (info.pt.x, info.pt.y)
        if wParam == 0x0201 and _over_pet(info.pt.x, info.pt.y):
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
        elif wParam == 0x0207 and _over_pet(info.pt.x, info.pt.y):
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

    lib = Path(r"C:\Users\yuvra\AppData\Roaming\Python\Python312\site-packages\webview\lib")
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
    form.ClientSize = Size(WIDTH, HEIGHT)
    point = POINT()
    user32.GetCursorPos(ctypes.byref(point))
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
            if data.get("type") != "ink":
                return
            ink_rect["left"] = float(data["left"])
            ink_rect["top"] = float(data["top"])
            ink_rect["right"] = float(data["right"])
            ink_rect["bottom"] = float(data["bottom"])
            ink_rect["ready"] = ink_rect["right"] > ink_rect["left"] and ink_rect["bottom"] > ink_rect["top"]
        except (TypeError, ValueError, KeyError):
            pass

    view.NavigationCompleted += on_nav
    Pet.hwnd = hwnd
    release_owner(hwnd)
    view.EnsureCoreWebView2Async(None)
    log(f"window {hwnd} style {user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF:08x}")

    focus = FocusWatch()
    pet = Pet(focus)
    pet.x, pet.y = spot
    global pet_ref
    pet_ref = pet
    look_state = {"gx": 0.0, "gy": 0.0, "reading": False, "mood": "", "flip": 1}

    def tick(sender, event):
        try:
            if not noted["hook"]:
                noted["hook"] = True
                log("ticking")
                threading.Thread(target=_hook_loop, name="wisp-click", daemon=True).start()
            if chord(0x57):
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


def main():
    own_the_process()
    log("started")
    import clr
    from System.Threading import ApartmentState, Thread, ThreadStart

    thread = Thread(ThreadStart(run_form))
    thread.SetApartmentState(ApartmentState.STA)
    thread.Start()
    thread.Join()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"failed: {error}")
        raise
