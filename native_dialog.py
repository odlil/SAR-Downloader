"""Enlarge only this short-lived picker's own Windows dialogs; no extra dependencies."""
from contextlib import contextmanager
import os
import sys
import threading


def dialog_geometry(work, current):
    """Fit a generous centered dialog inside the current monitor's working area."""
    left, top, right, bottom = work
    width, height = right - left, bottom - top
    target_width = min(max(1, width - 24), max(int(width * .82), current[2] - current[0]))
    target_height = min(max(1, height - 24), max(int(height * .84), current[3] - current[1]))
    return left + (width - target_width) // 2, top + (height - target_height) // 2, target_width, target_height


def enable_dpi_awareness():
    # Called before Tk creates any windows. This affects this picker process only.
    if sys.platform != "win32":
        return
    import ctypes
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        try:
            function = user32.SetProcessDpiAwarenessContext
        except AttributeError:
            user32.SetProcessDPIAware()
        else:
            function.argtypes = [ctypes.c_void_p]
            function.restype = ctypes.c_bool
            function(ctypes.c_void_p(-4))  # Per-monitor DPI awareness v2.
    except OSError:
        pass


def _resize_dialog(title, stop):
    import ctypes
    from ctypes import wintypes as wt

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wt.DWORD), ("monitor", wt.RECT), ("work", wt.RECT), ("flags", wt.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    signatures = {
        "EnumWindows": ([callback_type, wt.LPARAM], wt.BOOL),
        "IsWindowVisible": ([wt.HWND], wt.BOOL),
        "GetWindowThreadProcessId": ([wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
        "GetClassNameW": ([wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowTextW": ([wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowRect": ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
        "MonitorFromWindow": ([wt.HWND, wt.DWORD], wt.HANDLE),
        "GetMonitorInfoW": ([wt.HANDLE, ctypes.POINTER(MonitorInfo)], wt.BOOL),
        "SetWindowPos": ([wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT], wt.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(user32, name)
        function.argtypes, function.restype = arguments, result
    process_id = os.getpid()
    found = []

    @callback_type
    def visit(hwnd, _):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != process_id or not user32.IsWindowVisible(hwnd):
            return True
        class_name, window_title = ctypes.create_unicode_buffer(128), ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        user32.GetWindowTextW(hwnd, window_title, len(window_title))
        if class_name.value == "#32770" and window_title.value == title:
            found.append(hwnd)
            return False
        return True

    # Native modal loops do not consistently run Tk's `after` callbacks.
    for _ in range(200):
        if stop.wait(.08):
            return
        user32.EnumWindows(visit, 0)
        if found:
            break
    if not found or stop.wait(.15):
        return
    hwnd, rectangle, monitor = found[0], wt.RECT(), MonitorInfo()
    monitor.size = ctypes.sizeof(MonitorInfo)
    if not user32.GetWindowRect(hwnd, ctypes.byref(rectangle)):
        return
    handle = user32.MonitorFromWindow(hwnd, 2)  # Nearest monitor, including secondary monitors.
    if not user32.GetMonitorInfoW(handle, ctypes.byref(monitor)):
        return
    current = (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)
    area = (monitor.work.left, monitor.work.top, monitor.work.right, monitor.work.bottom)
    x, y, width, height = dialog_geometry(area, current)
    user32.SetWindowPos(hwnd, None, x, y, width, height, 0x4014)  # Async; keep Z order and activation.


@contextmanager
def large_native_dialog(title):
    if sys.platform != "win32":
        yield
        return
    stop = threading.Event()

    def run():
        try:
            _resize_dialog(title, stop)
        except (OSError, AttributeError):
            pass  # The normal native picker remains usable if sizing is unavailable.

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=.4)
