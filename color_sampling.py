"""Sample the desktop below a layered timer without capturing its own text."""
import ctypes
from ctypes import wintypes
import sys

from PySide6.QtGui import QColor


def set_capture_excluded(hwnd: int, excluded: bool) -> bool:
    # Exclusion is needed on DWM desktops even when BitBlt omits CAPTUREBLT.
    # Apply only while sampling transparent backgrounds, and restore on disable.
    if sys.platform != 'win32' or sys.getwindowsversion().build < 19041:
        return False
    function = ctypes.WinDLL('user32', use_last_error=True).SetWindowDisplayAffinity
    function.argtypes = [wintypes.HWND, wintypes.DWORD]
    function.restype = wintypes.BOOL
    return bool(function(hwnd, 0x11 if excluded else 0))


def inverse_background(background: QColor, underlay: QColor) -> QColor:
    alpha = background.alphaF()
    channels = [255 - round(fg * alpha + bg * (1 - alpha))
                for fg, bg in zip(background.getRgb()[:3], underlay.getRgb()[:3])]
    return QColor(*channels)


def sample_underlay(hwnd: int, regions, dpr: float):
    """Return median RGB for each client region, or None if capture fails.

    The caller excludes its own window with SetWindowDisplayAffinity. SRCCOPY
    also omits CAPTUREBLT so layered overlays do not contaminate the sample.
    https://learn.microsoft.com/windows/win32/api/wingdi/nf-wingdi-bitblt
    """
    if sys.platform != 'win32':
        return None
    user = ctypes.WinDLL('user32', use_last_error=True)
    gdi = ctypes.WinDLL('gdi32', use_last_error=True)
    user.GetDC.argtypes = [wintypes.HWND]
    user.GetDC.restype = wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi.SelectObject.restype = wintypes.HGDIOBJ
    gdi.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi.GetPixel.restype = wintypes.DWORD
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    origin = wintypes.POINT(0, 0)
    if not user.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    desktop = user.GetDC(None)
    if not desktop:
        return None
    memory = gdi.CreateCompatibleDC(desktop)
    bitmap = None
    previous = None
    try:
        width = max(1, round(max(r.right() + 1 for r in regions) * dpr))
        height = max(1, round(max(r.bottom() + 1 for r in regions) * dpr))
        bitmap = gdi.CreateCompatibleBitmap(desktop, width, height)
        if not memory or not bitmap:
            return None
        previous = gdi.SelectObject(memory, bitmap)
        if not gdi.BitBlt(memory, 0, 0, width, height, desktop, origin.x, origin.y, 0x00CC0020):
            return None
        colors = []
        for region in regions:
            samples = []
            for fx in (0.2, 0.5, 0.8):
                for fy in (0.25, 0.5, 0.75):
                    x = min(width - 1, max(0, round((region.x() + region.width() * fx) * dpr)))
                    y = min(height - 1, max(0, round((region.y() + region.height() * fy) * dpr)))
                    value = gdi.GetPixel(memory, x, y)
                    if value != 0xFFFFFFFF:
                        samples.append((value & 255, (value >> 8) & 255, (value >> 16) & 255))
            if not samples:
                return None
            colors.append(QColor(*(sorted(s[c] for s in samples)[len(samples) // 2] for c in range(3))))
        return colors
    finally:
        if previous:
            gdi.SelectObject(memory, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if memory:
            gdi.DeleteDC(memory)
        user.ReleaseDC(None, desktop)
