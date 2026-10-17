"""Windows coordinates, capture-excluded click-through windows, and desktop hotkeys."""
import ctypes
import queue
import threading
from ctypes import wintypes


class WindowsDesktop:
    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        try:
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            try:
                ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                self.user32.SetProcessDPIAware()
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        self._get_style = getattr(self.user32, "GetWindowLongPtrW", self.user32.GetWindowLongW)
        self._set_style = getattr(self.user32, "SetWindowLongPtrW", self.user32.SetWindowLongW)
        self._get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_style.restype = ctypes.c_ssize_t
        self._set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._set_style.restype = ctypes.c_ssize_t

    def bounds(self):
        x, y, w, h = [self.user32.GetSystemMetrics(i) for i in (76, 77, 78, 79)]
        return x, y, x + w, y + h

    def handle(self, window):
        return self.user32.GetAncestor(window.winfo_id(), 2)

    def place(self, window, rect, *, passive=False):
        x, y, right, bottom = rect
        window.geometry(f"{right - x}x{bottom - y}+0+0")
        window.update_idletasks()
        self.user32.SetWindowPos(self.handle(window), wintypes.HWND(-1), x, y, right - x, bottom - y,
                                0x0010 if passive else 0)

    def exclude_capture(self, window):
        window.update_idletasks()
        return bool(self.user32.SetWindowDisplayAffinity(self.handle(window), 0x11))

    def click_through(self, window):
        hwnd = self.handle(window)
        style = self._get_style(hwnd, -20)
        self._set_style(hwnd, -20, style | 0x00080000 | 0x00000020 | 0x08000000 | 0x00000080)


class Hotkeys:
    """A dedicated message loop keeps Windows hotkeys independent of Tk's event handling."""
    KEYS = {1: (ord("R"), "select"), 2: (ord("T"), "translate"),
            3: (ord("O"), "peek"), 4: (ord("S"), "toggle")}

    def __init__(self, events: queue.Queue):
        self.events = events
        self.thread_id = None
        self.stopping = threading.Event()
        threading.Thread(target=self._run, name="desktop-hotkeys", daemon=True).start()

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create this thread's message queue
        self.thread_id = kernel32.GetCurrentThreadId()
        registered = []
        for key_id, (key, _action) in self.KEYS.items():
            if user32.RegisterHotKey(None, key_id, 0x4003, key):  # Ctrl+Alt, no key repeat
                registered.append(key_id)
        if len(registered) != len(self.KEYS):
            self.events.put(("hotkey_warning", "일부 단축키를 다른 앱이 사용 중이에요. 창의 버튼을 사용해 주세요."))
        try:
            while not self.stopping.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312 and msg.wParam in self.KEYS:
                    self.events.put(("hotkey", self.KEYS[msg.wParam][1]))
        finally:
            for key_id in registered:
                user32.UnregisterHotKey(None, key_id)

    def close(self):
        self.stopping.set()
        if self.thread_id:
            ctypes.WinDLL("user32").PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
