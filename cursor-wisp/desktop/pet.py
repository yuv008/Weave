"""A desktop companion. It wanders, and hops next to where you click or type.

It notices that a key or click happened. It does not read or store what you typed.
Clicks pass through it, so the page underneath keeps focus.
Right-click the character, or press Ctrl+Alt+C, to change character.
Ctrl+Alt+W closes it.
"""

import ctypes
import math
import os
import queue
import random
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
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.EnumChildWindows.restype = wintypes.BOOL

HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
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
    return point.x, point.y, max(height, 16)


def fullscreen(our_hwnd):
    """A real full-screen app covers the whole monitor, including the taskbar.

    A maximized window stops above the taskbar, so the companion stays visible.
    """
    foreground = user32.GetForegroundWindow()
    if not foreground or foreground == our_hwnd:
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
        self._lock = threading.Lock()
        self._proc = None
        self._lines = queue.Queue()
        threading.Thread(target=self._loop, name="wisp-focus", daemon=True).start()

    def snapshot(self):
        with self._lock:
            return self.last_type, self.point, self.updated

    def _keys(self):
        edge = False
        held = False
        for vk in TYPE_KEYS:
            state = user32.GetAsyncKeyState(vk)
            if state & 1:
                edge = True
            if state & 0x8000:
                held = True
        if edge or held:
            with self._lock:
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

    def _cursor(self):
        point = POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        return point.x, point.y, 20

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
        if width > 2000 or height > 400:
            return None
        caret_x = x + min(width, 12)
        return caret_x, y, height

    def _click(self):
        if not (user32.GetAsyncKeyState(0x01) & 1):
            return
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        with self._lock:
            self.point = (point.x, point.y, 20)
            self.updated = time.time()
            self.last_type = time.time()

    def _loop(self):
        while True:
            self._keys()
            self._click()
            with self._lock:
                recent = time.time() - self.last_type < 1.4
            if recent:
                found = caret_from_win32(Pet.hwnd) or self._cursor()
                caret = self._ask()
                if caret:
                    found = caret
                if found:
                    with self._lock:
                        self.point = found
                        self.updated = time.time()
            time.sleep(0.12 if recent else 0.05)


class Pet:
    hwnd = None

    def __init__(self, focus):
        self.focus = focus
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        self.x, self.y = self.place_near(point.x, point.y, 20)
        self.tx, self.ty = self.x, self.y
        self.mode = "wander"
        self.jump_from = (self.x, self.y)
        self.jump_to = (self.x, self.y)
        self.jump_started = 0.0
        self.next_wander = 0.0
        self.visible = True

    def place_near(self, caret_x, caret_y, caret_h):
        x = caret_x - 20
        y = caret_y - HEIGHT - 4
        return clamp(x, y)

    def begin_jump(self, x, y):
        self.mode = "jump"
        self.jump_from = (self.x, self.y)
        self.jump_to = (x, y)
        self.jump_started = time.time()

    def choose_wander(self):
        left, top, right, bottom = work_area(self.x + WIDTH / 2, self.y + HEIGHT / 2)
        self.tx = min(max(self.x + random.uniform(-160, 160), left + 16), right - WIDTH - 16)
        self.ty = min(max(self.y + random.uniform(-90, 70), top + 16), bottom - HEIGHT - 16)
        self.next_wander = time.time() + random.uniform(4.5, 8.5)

    def step(self):
        now = time.time()
        last_type, point, updated = self.focus.snapshot()
        typing = now - last_type < 1.35 and point and now - updated < 1.6
        if typing and self.mode != "jump":
            tx, ty = self.place_near(*point)
            distance = math.hypot(tx - self.x, ty - self.y)
            if distance > 120:
                self.begin_jump(tx, ty)
            else:
                self.mode = "follow"
                self.tx, self.ty = tx, ty
        elif self.mode != "jump":
            if self.mode != "wander" or now >= self.next_wander:
                self.mode = "wander"
                self.choose_wander()

        if self.mode == "jump":
            t = min(1.0, (now - self.jump_started) / 0.42)
            ease = 1 - (1 - t) ** 3
            self.x = self.jump_from[0] + (self.jump_to[0] - self.jump_from[0]) * ease
            hop = math.sin(math.pi * t) * 54
            self.y = self.jump_from[1] + (self.jump_to[1] - self.jump_from[1]) * ease - hop
            if t >= 1:
                self.mode = "follow"
                self.x, self.y = self.jump_to
        else:
            rate = 7.5 if self.mode == "follow" else 1.15
            blend = min(1.0, 0.016 * rate)
            self.x += (self.tx - self.x) * blend
            self.y += (self.ty - self.y) * blend

        self.x, self.y = clamp(self.x, self.y)
        self.apply()

    def apply(self):
        hwnd = Pet.hwnd
        if not hwnd:
            return
        covered = fullscreen(hwnd)
        if covered and self.visible:
            user32.ShowWindow(hwnd, SW_HIDE)
            self.visible = False
        elif not covered and not self.visible:
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            self.visible = True
        if self.visible:
            user32.SetWindowPos(hwnd, HWND_TOPMOST, int(self.x), int(self.y), 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)

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
click_hole = {"down": False, "show_at": 0.0, "cycle": False}
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


@ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
def _mouse_hook_proc(nCode, wParam, lParam):
    if nCode >= 0 and Pet.hwnd:
        info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if wParam == 0x0204 and _over_pet(info.pt.x, info.pt.y):
            click_hole["cycle"] = True
            return 1
        if wParam in (0x0201, 0x0207) and _over_pet(info.pt.x, info.pt.y):
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


def chord(key):
    ctrl = key_down(0x11) or key_down(0xA2) or key_down(0xA3)
    alt = key_down(0x12) or key_down(0xA4) or key_down(0xA5)
    return ctrl and alt and key_down(key)


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

    view.NavigationCompleted += on_nav
    Pet.hwnd = hwnd
    view.EnsureCoreWebView2Async(None)
    log(f"window {hwnd} style {user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF:08x}")

    focus = FocusWatch()
    pet = Pet(focus)
    pet.x, pet.y = spot
    cycle_was_down = False

    def tick(sender, event):
        nonlocal cycle_was_down
        try:
            if not noted["hook"]:
                noted["hook"] = True
                log("ticking")
                threading.Thread(target=_hook_loop, name="wisp-click", daemon=True).start()
            if chord(0x57):
                os._exit(0)
            held = chord(0x43)
            if (click_hole["cycle"] or (held and not cycle_was_down)) and view.CoreWebView2 is not None:
                click_hole["cycle"] = False
                view.CoreWebView2.ExecuteScriptAsync("weaveNext()")
                log("next character")
            cycle_was_down = held
            if click_hole["show_at"] and not click_hole["down"] and time.time() >= click_hole["show_at"]:
                click_hole["show_at"] = 0.0
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            pet.step()
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
