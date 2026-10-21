import copy
import ctypes
from ctypes import wintypes
import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from functools import lru_cache, partial

from PySide6.QtCore import *
from PySide6.QtGui import *
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import *
from settings_ui import AppearancePreview, OptionCheckBox, SETTINGS_STYLE, ToggleSwitch, SettingsDragHandle
from color_sampling import inverse_background, sample_underlay, set_capture_excluded

APP_ID = "QTimer"  # Stable storage/instance identity preserves existing installations.
__app_name__ = "超智能计时器"
__version__ = "1.7.1-guide.1"
FORK_URL = "https://github.com/zhouliangchen/QTimer"
__author__ = "QwejayHuang"
__company__ = "QwejayHuang"
__description__ = "超智能计时器 · 演示与会议计时"

try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False
    print("💡 提示：推荐执行 'pip install keyboard' 以启用真正的全局热键支持。")

def get_app_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def resource_path(name: str) -> str:
    return os.path.join(getattr(sys, '_MEIPASS', get_app_dir()), name)


OVERTIME_PRESETS = {
    "overdue-red": ("已超时 · 红色", "overdue-red.png"),
    "time-up-blue": ("时间到 · 蓝色", "time-up-blue.png"),
    "please-finish-amber": ("请结束 · 琥珀色", "please-finish-amber.png"),
}


def timeout_image_path(value: str) -> str:
    if value.startswith("preset:"):
        preset = OVERTIME_PRESETS.get(value.removeprefix("preset:"))
        return resource_path("assets/overtime/" + preset[1]) if preset else ""
    return value

def get_config_path() -> str:
    loc = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    if not loc:
        loc = os.path.join(os.path.expanduser("~"), f".{APP_ID.lower()}")
    else:
        loc = os.path.join(loc, APP_ID)
    os.makedirs(loc, exist_ok=True)
    return os.path.join(loc, "config.json")

CONFIG_PATH = get_config_path()
WNDENUMPROC_TYPE = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

def _matches_ppt_slideshow_window(class_name: str, title: str, process_name: str) -> bool:
    """Identify show windows without mistaking a generic Qt/editor window for a show."""
    if process_name.lower() not in {"powerpnt.exe", "wpp.exe", "wps.exe"}:
        return False
    cls = class_name.lower()
    if cls in {"screenclass", "kslideshow", "wps_slideshow", "wpp_slideshow_class"}:
        return True
    # Current WPS uses Qt5QWindowIcon rather than its older slideshow class.
    show_title = any(token in title.lower() for token in (
        "slide show", "slideshow", "幻灯片放映", "幻灯片播放", "演示放映"
    ))
    return show_title and (
        cls.startswith(("qt5qwindow", "qt6qwindow"))
        or cls in {"paneclassdc", "mso_subwindow_class"}
    )

def find_ppt_slideshow_windows() -> list[int]:
    if platform.system() != "Windows":
        return []
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # Explicit pointer-sized types avoid truncating HWND/HANDLE on 64-bit Windows.
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        
        def check_hwnd(hwnd: int) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return False
            buf_cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf_cls, 256)
            cls = buf_cls.value.lower()
            if cls not in {
                "screenclass", "kslideshow", "wps_slideshow", "wpp_slideshow_class",
                "paneclassdc", "mso_subwindow_class"
            } and not cls.startswith(("qt5qwindow", "qt6qwindow")):
                return False
            title = ctypes.create_unicode_buffer(1024)
            user32.GetWindowTextW(hwnd, title, len(title))
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            handle = kernel32.OpenProcess(0x1000, False, pid.value)
            if not handle:
                return False
            try:
                image = ctypes.create_unicode_buffer(32768)
                length = wintypes.DWORD(len(image))
                if not kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                    return False
                return _matches_ppt_slideshow_window(cls, title.value, os.path.basename(image.value))
            finally:
                kernel32.CloseHandle(handle)

        active = []
        def enum_cb(hwnd, lparam):
            if check_hwnd(hwnd):
                active.append(hwnd)
            return True
            
        ctypes.windll.user32.EnumWindows(WNDENUMPROC_TYPE(enum_cb), 0)
        return active
    except Exception as e:
        print(f"PPT检测异常: {e}")
        return []

def is_ppt_slideshow_active() -> bool:
    return bool(find_ppt_slideshow_windows())

def close_ppt_slideshows() -> None:
    """Send Escape only to verified slideshow windows, leaving editors open."""
    if platform.system() != "Windows":
        return
    post = ctypes.windll.user32.PostMessageW
    post.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    for hwnd in find_ppt_slideshow_windows():
        post(hwnd, 0x0100, 0x1B, 0x00010001)  # WM_KEYDOWN / Escape
        post(hwnd, 0x0101, 0x1B, 0xC0010001)  # WM_KEYUP

def play_alert_sound(file_path: str = "", duration_ms: int = 200) -> None:
    if file_path and os.path.exists(file_path):
        sys_name = platform.system()
        if sys_name == "Windows":
            def _play_win():
                try:
                    import winsound
                    winsound.PlaySound(file_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                except Exception:
                    pass
            threading.Thread(target=_play_win, daemon=True).start()
            return
        elif sys_name == "Darwin":
            def _play_mac():
                try:
                    subprocess.call(['afplay', file_path])
                except Exception:
                    pass
            threading.Thread(target=_play_mac, daemon=True).start()
            return

    sys_name = platform.system()
    if sys_name == "Windows":
        def _beep():
            try:
                import winsound
                winsound.Beep(1500, duration_ms)
            except Exception:
                pass
        threading.Thread(target=_beep, daemon=True).start()
    elif sys_name == "Darwin":
        def _mac_beep():
            try:
                subprocess.call(['afplay', '/System/Library/Sounds/Glass.aiff'])
            except Exception:
                pass
        threading.Thread(target=_mac_beep, daemon=True).start()
    else:
        QApplication.beep()

def get_rgba_color(hex_color: str, alpha_pct: int) -> str:
    col = QColor(hex_color)
    return f"rgba({col.red()}, {col.green()}, {col.blue()}, {alpha_pct / 100:.2f})"

SVG_ICONS: dict[str, str] = {
    "play":     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><polygon points="6,4 20,12 6,20" fill="{color}"/></svg>',
    "pause":    '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><rect x="5" y="4" width="4" height="16" rx="1" fill="{color}"/><rect x="15" y="4" width="4" height="16" rx="1" fill="{color}"/></svg>',
    "restart":  '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z" fill="{color}"/></svg>',
    "prev":     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><polygon points="19,4 7,12 19,20" fill="{color}"/><rect x="3" y="4" width="3" height="16" rx="1" fill="{color}"/></svg>',
    "next":     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><polygon points="5,4 17,12 5,20" fill="{color}"/><rect x="18" y="4" width="3" height="16" rx="1" fill="{color}"/></svg>',
    "settings": '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M19.14 12.94c.04-.3.06-.61.06-.94s-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z" fill="{color}"/></svg>',
    "close":    '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><line x1="4" y1="4" x2="20" y2="20" stroke="{color}" stroke-width="2.5" stroke-linecap="round"/><line x1="20" y1="4" x2="4" y2="20" stroke="{color}" stroke-width="2.5" stroke-linecap="round"/></svg>',
    "up":       '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M7.41 15.41L12 10.83l4.59 4.58L18 14l-6-6-6 6z" fill="{color}"/></svg>',
    "down":     '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z" fill="{color}"/></svg>',
    "trash":    '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" fill="{color}"/></svg>',
}

@lru_cache(maxsize=128)
def _render_svg_icon(name: str, size: int, color: str, dpr: float) -> QIcon:
    svg_str = SVG_ICONS.get(name, "").replace("{color}", color)
    if not svg_str:
        return QIcon()
    renderer = QSvgRenderer(svg_str.encode("utf-8"))
    physical_size = int(size * dpr)
    physical_size = max(physical_size, 1)
    pixmap = QPixmap(physical_size, physical_size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(painter, QRectF(0, 0, physical_size, physical_size))
    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return QIcon(pixmap)

def get_svg_icon(name: str, size: int, color: str = "white") -> QIcon:
    app = QApplication.instance()
    widget = app.activeWindow() or (app.topLevelWidgets()[0] if app.topLevelWidgets() else None)
    dpr = widget.devicePixelRatioF() if widget else 1.0
    return _render_svg_icon(name, size, color, dpr)

@dataclass
class Stage:
    label: str = "说课时间"
    duration: int = 5
    unit: str = "分"
    count_up: bool = False
    timeout_display: str = "zero"
    timeout_text: str = "已超时"
    timeout_image: str = ""
    timeout_close_show: bool = False

    @property
    def seconds(self) -> int:
        return self.duration * 60 if self.unit == "分" else self.duration

    @classmethod
    def from_dict(cls, d: dict) -> 'Stage':
        return cls(label=d.get("label", ""), duration=d.get("duration", d.get("minutes", 5)),
                   unit=d.get("unit", "分"), count_up=d.get("count_up", False),
                   timeout_display=d.get("timeout_display", "zero"),
                   timeout_text=d.get("timeout_text", "已超时"),
                   timeout_image=d.get("timeout_image", ""),
                   timeout_close_show=d.get("timeout_close_show", False))

@dataclass
class Alert:
    seconds: int = 30
    color: str = "#ffaa00"
    play_sound: bool = True
    keep_flashing: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> 'Alert':
        return cls(seconds=d.get("seconds", 30), color=d.get("color", "#ffaa00"),
                   play_sound=d.get("play_sound", True), keep_flashing=d.get("keep_flashing", False))

class Config:
    def __init__(self):
        self.auto_advance: bool = False
        self.global_sound: bool = True
        self.countdown_10s_sound: bool = True
        self.alert_sound_path: str = ""
        self.end_sound_path: str = ""
        self.pos_x: int = -1
        self.pos_y: int = -1
        self.always_on_top: bool = True
        self.prevent_offscreen: bool = True
        self.show_stage_label: bool = True
        self.ppt_auto_start: bool = True
        self.ppt_auto_pause: bool = True
        self.double_click_toggle: bool = True
        self.stages: list[Stage] = [Stage("说课时间", 5, "分", False), Stage("答辩时间", 2, "分", False)]
        self.alerts: list[Alert] = [Alert(30, "#ffaa00", True), Alert(10, "#ff4444", True)]
        self.color: str = "#ffffff"
        self.auto_text_color: bool = False
        self.font: str = "微软雅黑"
        self.font_size: int = 32
        self.stage_font: str = "微软雅黑"
        self.stage_font_size: int = 18
        self.bg_color: str = "#141414"
        self.bg_transparency: int = 18         
        self.font_transparency: int = 0         
        self.shortcut_toggle: str = "Ctrl+Space"
        self.shortcut_reset: str = "Ctrl+Return"
        self.shortcut_prev: str = "Ctrl+Left"
        self.shortcut_next: str = "Ctrl+Right"
        self.ui_scale: int = 100

    def save(self) -> None:
        data = {
            "auto_advance": self.auto_advance,
            "global_sound": self.global_sound,
            "countdown_10s_sound": self.countdown_10s_sound,
            "alert_sound_path": self.alert_sound_path,
            "end_sound_path": self.end_sound_path,
            "pos_x": self.pos_x,
            "pos_y": self.pos_y,
            "always_on_top": self.always_on_top,
            "prevent_offscreen": self.prevent_offscreen,
            "show_stage_label": self.show_stage_label,
            "ppt_auto_start": self.ppt_auto_start,
            "ppt_auto_pause": self.ppt_auto_pause,
            "double_click_toggle": self.double_click_toggle,
            "stages": [asdict(s) for s in self.stages],
            "alerts": [asdict(a) for a in self.alerts],
            "color": self.color,
            "auto_text_color": self.auto_text_color,
            "font": self.font,
            "font_size": self.font_size,
            "stage_font": self.stage_font,
            "stage_font_size": self.stage_font_size,
            "bg_color": self.bg_color,
            "bg_transparency": self.bg_transparency,
            "font_transparency": self.font_transparency,
            "shortcut_toggle": self.shortcut_toggle,
            "shortcut_reset": self.shortcut_reset,
            "shortcut_prev": self.shortcut_prev,
            "shortcut_next": self.shortcut_next,
            "ui_scale": self.ui_scale,
        }
        try:
            tmp_path = CONFIG_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if os.path.exists(tmp_path):
                os.replace(tmp_path, CONFIG_PATH)
        except Exception:
            pass

    def load(self) -> None:
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict): return
            # The previous single switch controlled both actions.
            if "ppt_auto_pause" not in d and "ppt_auto_start" in d:
                d["ppt_auto_pause"] = d["ppt_auto_start"]
            
            for key in self.__dict__.keys():
                if key in d and not key.startswith('_'):
                    setattr(self, key, d[key])
                    
            if "stages" in d:
                self.stages = [Stage.from_dict(s) for s in d["stages"] if isinstance(s, dict)]
            if "alerts" in d:
                self.alerts = [Alert.from_dict(a) for a in d["alerts"] if isinstance(a, dict)]
        except Exception as e:
            print(f"加载配置失败，将使用默认值: {e}")

    def bg_qcolor(self) -> QColor:
        c = QColor(self.bg_color)
        opacity_pct = max(0, min(100, 100 - self.bg_transparency))
        alpha = int(opacity_pct / 100 * 255)
        if self.bg_transparency == 100:
            alpha = 1
        c.setAlpha(alpha)
        return c

class TimerController(QObject):
    tick = Signal(str, int)
    stage_changed = Signal(int, str)
    alert_triggered = Signal(str, bool)
    stage_expired = Signal(object)
    loop_restarted = Signal()
    state_changed = Signal(bool)

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self.running: bool = False
        self.paused: bool = True
        self._stage_idx: int = 0
        self._remaining_float: float = 0.0
        self._target_time: float = 0.0
        self._last_displayed: int = -1
        self._triggered = set()
        self._zero_triggered: bool = False

    def toggle_pause(self) -> None:
        if self._remaining_float <= 0:
            return
        if not self.running:
            self.running = True
        self.paused = not self.paused
        if not self.paused:
            self._target_time = time.time() + self._remaining_float
            self._timer.start()
        else:
            self._timer.stop()
        self.state_changed.emit(not self.paused)

    def restart_stage(self) -> None:
        self._load_stage()
        self._stop_and_wait()

    def prev_stage(self) -> None:
        if not self.config.stages:
            return
        self._stage_idx = (self._stage_idx - 1) % len(self.config.stages)
        self._load_stage()
        self._stop_and_wait()

    def next_stage(self) -> None:
        self._advance_idx()
        self._load_stage()
        self._stop_and_wait()

    def stop(self) -> None:
        self._timer.stop()
        self._stage_idx = 0
        self._load_stage()
        self._stop_and_wait()
        self.running = False

    def _stop_and_wait(self) -> None:
        self._timer.stop()
        self.paused = True
        self.state_changed.emit(False)

    def _load_stage(self) -> None:
        if not self.config.stages:
            return
        st = self.config.stages[self._stage_idx]
        self._remaining_float = float(st.seconds)
        self._last_displayed = st.seconds
        self._triggered.clear()
        self._zero_triggered = False
        self.stage_changed.emit(self._stage_idx, st.label)
        self.tick.emit(st.label, 0 if st.count_up else st.seconds)

    def _tick(self) -> None:
        if self.paused or not self.config.stages:
            return
        now = time.time()
        self._remaining_float = self._target_time - now
        rem_sec = max(0, int(math.ceil(self._remaining_float)))
        if rem_sec != self._last_displayed:
            previous = self._last_displayed
            self._last_displayed = rem_sec
            st = self.config.stages[self._stage_idx]
            display_sec = max(0, st.seconds - rem_sec) if st.count_up else rem_sec
            self.tick.emit(st.label, display_sec)
            if self.config.global_sound and self.config.countdown_10s_sound and 0 < rem_sec <= 10:
                play_alert_sound(self.config.alert_sound_path, 200)
            for a in sorted(self.config.alerts, key=lambda alert: alert.seconds, reverse=True):
                if rem_sec <= a.seconds < previous and a.seconds not in self._triggered:
                    self._triggered.add(a.seconds)
                    self.alert_triggered.emit(a.color, a.keep_flashing)
                    if self.config.global_sound and a.play_sound:
                        play_alert_sound(self.config.alert_sound_path, 200)
        if self._remaining_float <= 0 and not self._zero_triggered:
            self._zero_triggered = True
            self._remaining_float = 0.0
            expired_stage = self.config.stages[self._stage_idx]
            if not expired_stage.count_up:
                self.stage_expired.emit(expired_stage)
            if self.config.global_sound:
                play_alert_sound(self.config.end_sound_path, 2000)
            if self.config.auto_advance:
                self._advance_idx()
                self._load_stage()
                self._target_time = time.time() + self._remaining_float
            else:
                self._stop_and_wait()

    def _advance_idx(self) -> None:
        if not self.config.stages:
            return
        self._stage_idx += 1
        if self._stage_idx >= len(self.config.stages):
            self._stage_idx = 0
            self.loop_restarted.emit()

class FloatBar(QWidget):
    SNAP_DISTANCE = 12
    SNAP_RELEASE_DISTANCE = 20
    request_settings = Signal()
    request_exit = Signal()
    double_clicked = Signal()
    wheel_scrolled = Signal(int)
    position_changed = Signal(int, int) 

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._drag_pos = None
        self._drag_axes = {}
        self._drawer_left = False
        self._drawer_target_left = False
        self._drawer_reveal = 0.0
        self._drawer_goal = 0.0
        self._side_switching = False
        self._drawer_space = 0
        self._geometry_ready = False
        self._adjusting_geometry = False
        self._overtime = None
        self._text_color = "#ffffff"
        self._auto_text_color = False
        self._auto_colors = ["#ffffff", "#ffffff"]
        self._capture_excluded = False
        self._sampling_hwnd = None
        self._alert_active = False
        self._font_family = "微软雅黑"
        self._font_size = 32
        self._stage_font_family = "微软雅黑"
        self._stage_font_size = 18
        self._bg_color = QColor(20, 20, 20, 210)
        self._show_stage_label = True
        self._prevent_offscreen = True
        self._font_transparency = 0
        self._target_opacity = 1.0
        self._ui_scale = 100
        self._icon_size = 34
        self._is_running = False
        self._current_stage_text = ""
        self._text_width = 150
        self._full_width = 300
        self._flash_timer = QTimer(self)
        self._flash_state = False
        self._flash_color = "#ff4444"
        self._flash_timer.timeout.connect(self._do_flash)
        self._flash_stop_timer = QTimer(self)
        self._flash_stop_timer.setSingleShot(True)
        self._flash_stop_timer.timeout.connect(self.stop_flash)
        self._width_anim = QPropertyAnimation(self, b"drawer_reveal")
        self._width_anim.setDuration(220)
        self._width_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._btn_opacity = QGraphicsOpacityEffect()
        self._anim_group = QParallelAnimationGroup(self)
        self._anim_group.addAnimation(self._width_anim)
        self._anim_group.finished.connect(self._finish_drawer_animation)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._do_hide_anim)
        self._build_ui()
        self._update_size()
        self._color_sample_timer = QTimer(self)
        self._color_sample_timer.setInterval(200)
        self._color_sample_timer.timeout.connect(self._update_auto_colors)

    @Property(int)
    def bar_width(self) -> int:
        return self._text_width + round(self._drawer_space * self._drawer_reveal)

    @bar_width.setter
    def bar_width(self, w: int) -> None:
        self.drawer_reveal = (w - self._text_width) / max(1, self._drawer_space)

    @Property(float)
    def drawer_reveal(self) -> float:
        return self._drawer_reveal

    @drawer_reveal.setter
    def drawer_reveal(self, value: float) -> None:
        self._drawer_reveal = max(0.0, min(1.0, value))
        self._btn_opacity.setOpacity(self._drawer_reveal)
        self._position_canvas()

    def content_position(self) -> QPoint:
        return self.mapToGlobal(QPoint(self._drawer_space, 0))

    def move_content(self, position: QPoint) -> None:
        self.move(position.x() - self._drawer_space, position.y())

    def _visible_rect(self) -> QRect:
        extent = round(self._drawer_space * self._drawer_reveal)
        return QRect(self._drawer_space - (extent if self._drawer_left else 0), 0,
                     self._text_width + extent, self.height())

    def visible_geometry(self) -> QRect:
        rect = self._visible_rect()
        return QRect(self.mapToGlobal(rect.topLeft()), rect.size())

    def _position_canvas(self) -> None:
        if not self._geometry_ready:
            return
        # The native window and text never move/resize during drawer animation.
        # Only the painted/hit-test region and the clipped button strip change.
        extent = round(self._drawer_space * self._drawer_reveal)
        button_width = self._btn_container.width()
        gap = self._drawer_space - button_width
        if self._drawer_left:
            self._btn_container.move(self._drawer_space - extent, 0)
            clip = QRect(0, 0, max(0, extent - gap), self.height())
        else:
            self._btn_container.move(self._drawer_space + self._text_width + extent - button_width, 0)
            clip = QRect(self._drawer_space - extent, 0, max(0, extent - gap), self.height())
        self._btn_container.setVisible(clip.width() > 0)
        if clip.width() > 0:
            self._btn_container.setMask(QRegion(clip))
        self._text_container.raise_()
        self.setMask(QRegion(self._visible_rect()))
        self.update()

    def _update_drawer_direction(self) -> None:
        if not hasattr(self, '_text_container') or self._adjusting_geometry:
            return
        content_left = self.content_position().x()
        center = QPoint(content_left + self._text_width // 2, self.y() + self.height() // 2)
        screen = QApplication.screenAt(center) or self.screen()
        if not screen:
            return
        geometry = screen.availableGeometry()
        left = center.x() >= geometry.left() + geometry.width() * 2 / 3
        if left == self._drawer_target_left:
            return
        self._drawer_target_left = left
        if self._side_switching:
            if left == self._drawer_left:
                self._side_switching = False
                self._animate_reveal(self._drawer_goal, 180)
            return
        if self.isVisible() and self._drawer_reveal > 0:
            self._side_switching = True
            self._animate_reveal(0.0, 140)
        else:
            self._drawer_left = left
            self._position_canvas()

    def _animate_reveal(self, goal: float, duration: int = 220) -> None:
        self._anim_group.stop()
        self._width_anim.setDuration(duration)
        self._width_anim.setStartValue(self._drawer_reveal)
        self._width_anim.setEndValue(goal)
        self._anim_group.start()

    def _finish_drawer_animation(self) -> None:
        if self._side_switching:
            self._side_switching = False
            self._drawer_left = self._drawer_target_left
            self._position_canvas()
            if self._drawer_goal:
                self._animate_reveal(self._drawer_goal, 180)

    def _set_drawer_open(self, opened: bool) -> None:
        self._drawer_goal = 1.0 if opened else 0.0
        if not self._side_switching:
            self._animate_reveal(self._drawer_goal)

    def _build_ui(self) -> None:
        self._canvas = QWidget(self)
        self._canvas.move(0, 0)
        self._text_container = QWidget(self._canvas)
        self._text_layout = QHBoxLayout(self._text_container)
        self._text_layout.setContentsMargins(18, 0, 18, 0)
        self._text_layout.setSpacing(0)
        
        self.lbl_stage = QLabel("环节名称")
        self.lbl_stage.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_time = QLabel("00:00")
        self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self._text_layout.addWidget(self.lbl_stage, 0, Qt.AlignmentFlag.AlignVCenter)
        self._spacing_item = QSpacerItem(12, 0, QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._text_layout.addItem(self._spacing_item)
        self._text_layout.addWidget(self.lbl_time, 0, Qt.AlignmentFlag.AlignVCenter)
        self._btn_container = QWidget(self._canvas)
        btn_lay = QHBoxLayout(self._btn_container)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        btn_lay.setSpacing(2)
        self.btn_toggle = self._make_icon_btn("play", "播放 / 暂停")
        self.btn_restart = self._make_icon_btn("restart", "重置当前环节")
        self.btn_prev = self._make_icon_btn("prev", "上一环节")
        self.btn_next = self._make_icon_btn("next", "下一环节")
        self.btn_settings = self._make_icon_btn("settings", "设置")
        self.btn_close = self._make_icon_btn("close", "退出")
        for b in (self.btn_toggle, self.btn_restart, self.btn_prev, self.btn_next, self.btn_settings, self.btn_close):
            btn_lay.addWidget(b)
        self._btn_container.setGraphicsEffect(self._btn_opacity)
        self._btn_opacity.setOpacity(0.0)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_ctx_menu)
        
        shadow_stage = QGraphicsDropShadowEffect(self)
        shadow_stage.setBlurRadius(4)
        shadow_stage.setColor(QColor(0, 0, 0, 180))
        shadow_stage.setOffset(1, 1)
        self.lbl_stage.setGraphicsEffect(shadow_stage)

        shadow_time = QGraphicsDropShadowEffect(self)
        shadow_time.setBlurRadius(4)
        shadow_time.setColor(QColor(0, 0, 0, 180))
        shadow_time.setOffset(1, 1)
        self.lbl_time.setGraphicsEffect(shadow_time)

    def _make_icon_btn(self, icon_name: str, tip: str) -> QPushButton:
        b = QPushButton()
        b.setProperty("icon_name", icon_name)
        b.setToolTip(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        return b

    def apply_style(self, color: str, font: str, size: int, stage_font: str, stage_size: int,
                    opacity: float, bg_color: QColor, always_on_top: bool,
                    show_stage_label: bool, prevent_offscreen: bool, font_transparency: int, ui_scale: int = 100,
                    auto_text_color: bool = False) -> None:
        self._text_color = color
        self._auto_text_color = auto_text_color
        self._font_family = font
        self._stage_font_family = stage_font
        self._bg_color = bg_color
        self._show_stage_label = show_stage_label
        self._prevent_offscreen = prevent_offscreen
        self._font_transparency = font_transparency
        self._ui_scale = ui_scale
        scale = self._ui_scale / 100.0
        self._font_size = max(10, int(size * scale))
        self._stage_font_size = max(8, int(stage_size * scale))
        self._icon_size = max(16, int(34 * scale))
        icon_inner = max(10, self._icon_size - 8)
        btn_radius = max(3, int(7 * scale))
        self._text_layout.setContentsMargins(int(18 * scale), 0, int(18 * scale), 0)
        self._btn_container.layout().setSpacing(int(2 * scale))
        for b in (self.btn_toggle, self.btn_restart, self.btn_prev, self.btn_next, self.btn_settings, self.btn_close):
            b.setFixedSize(self._icon_size, self._icon_size)
            i_name = b.property("icon_name")
            if i_name:
                b.setIcon(get_svg_icon(i_name, icon_inner))
            b.setIconSize(QSize(icon_inner, icon_inner))
            b.setStyleSheet(f"QPushButton {{ background: rgba(255,255,255,15); border: none; border-radius: {btn_radius}px; }} "
                            f"QPushButton:hover {{ background: rgba(255,255,255,40); }}")
        self._target_opacity = 1.0
        self.setWindowOpacity(1.0)
        flags = self.windowFlags()
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        if flags != self.windowFlags():
            was_visible = self.isVisible()
            pos_before = self.pos()
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            if was_visible:
                self.move(pos_before)
                self.show()
        self._refresh_labels()
        self._update_size()
        self._sync_capture_exclusion()
        if auto_text_color:
            self._color_sample_timer.start()
            self._update_auto_colors()
        else:
            self._color_sample_timer.stop()
        self.update()

    def _sync_capture_exclusion(self) -> None:
        needed = self._auto_text_color and self._bg_color.alpha() < 255
        hwnd = int(self.winId())
        if self._sampling_hwnd != hwnd or needed != self._capture_excluded:
            self._capture_excluded = needed and set_capture_excluded(hwnd, True)
            if not needed:
                set_capture_excluded(hwnd, False)
            self._sampling_hwnd = hwnd

    def _update_auto_colors(self) -> None:
        if not self._auto_text_color or not self.isVisible():
            return
        labels = (self.lbl_stage, self.lbl_time)
        regions = [QRect(label.mapTo(self, QPoint(0, 0)), label.size()) for label in labels]
        if self._bg_color.alpha() < 255:
            underlay = (sample_underlay(int(self.winId()), regions, self.devicePixelRatioF())
                        if self._capture_excluded else None)
        else:
            underlay = [QColor('white')] * 2
        if underlay is None:
            # Retain the last valid color if the desktop temporarily cannot be read.
            return
        colors = [inverse_background(self._bg_color, color).name() for color in underlay]
        if colors != self._auto_colors:
            self._auto_colors = colors
            self._refresh_labels()

    def _refresh_labels(self, stage_color: str = None, time_color: str = None) -> None:
        bases = self._auto_colors if self._auto_text_color else [self._text_color] * 2
        if self._alert_active and (not self._flash_timer.isActive() or self._flash_state):
            bases = [self._flash_color] * 2
        sc = stage_color or bases[0]
        tc = time_color or bases[1]
        opacity_pct = max(0, min(100, 100 - self._font_transparency))
        rgba_sc = get_rgba_color(sc, opacity_pct)
        rgba_tc = get_rgba_color(tc, opacity_pct)
        self.lbl_stage.setStyleSheet(
            f"color:{rgba_sc}; font-family:'{self._stage_font_family}'; "
            f"font-size:{self._stage_font_size}px; font-weight:600; background:transparent; padding:0px; margin:0px;")
        self.lbl_time.setStyleSheet(
            f"color:{rgba_tc}; font-family:'{self._font_family}'; "
            f"font-size:{self._font_size}px; font-weight:900; background:transparent; padding:0px; margin:0px;")
        self.lbl_stage.style().polish(self.lbl_stage)
        self.lbl_time.style().polish(self.lbl_time)

    def _update_size(self) -> None:
        content = self.content_position() if self._geometry_ready else self.pos()
        if hasattr(self, '_anim_group'):
            self._anim_group.stop()
        self._side_switching = False
        self._drawer_left = self._drawer_target_left
        
        scale = getattr(self, '_ui_scale', 100) / 100.0
        fm_stage = self.lbl_stage.fontMetrics()
        fm_time = self.lbl_time.fontMetrics()
        
        text = self._current_stage_text
        sample_text = (self.lbl_time.text() if self._overtime else
                       ("88:88:88" if len(self.lbl_time.text()) > 5 else "88:88"))
        time_w = fm_time.horizontalAdvance(sample_text) + int(24 * scale)
        if self._overtime and self._overtime.timeout_display == "image" and not self.lbl_time.pixmap().isNull():
            time_w = self.lbl_time.pixmap().width() + int(24 * scale)
        self.lbl_time.setFixedWidth(time_w)
        
        btn_count = 6
        btn_w = (getattr(self, '_icon_size', 34) + int(2 * scale)) * btn_count
        self._btn_container.setFixedWidth(btn_w)
        
        base_margin = int(18 * scale)
        stage_spacing = int(12 * scale)
        
        if self._show_stage_label and text.strip():
            self.lbl_stage.show()
            self._spacing_item.changeSize(stage_spacing, 0, QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            stage_w = fm_stage.horizontalAdvance(text) + int(20 * scale)
            self.lbl_stage.setText(text)
            self.lbl_stage.setFixedWidth(stage_w)
            self._text_width = base_margin + stage_w + stage_spacing + time_w + base_margin
        else:
            self.lbl_stage.hide()
            self._spacing_item.changeSize(0, 0, QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            self._text_width = base_margin + time_w + base_margin

        self._full_width = self._text_width + btn_w + int(16 * scale)
        self._drawer_space = btn_w + int(16 * scale)
        h = max(self._font_size + int(24 * scale), int(50 * scale))
        self.lbl_stage.setFixedHeight(int(h))
        self.lbl_time.setFixedHeight(int(h))
        self._text_container.setFixedSize(int(self._text_width), int(h))
        self._btn_container.setFixedHeight(int(h))
        self._text_container.move(self._drawer_space, 0)
        self._adjusting_geometry = True
        self._canvas.setFixedSize(self._text_width + 2 * self._drawer_space, int(h))
        self.setFixedSize(self._canvas.size())
        self.move_content(content)
        self._geometry_ready = True
        self._adjusting_geometry = False
        self._position_canvas()
        if self.isVisible() and self._drawer_reveal != self._drawer_goal:
            self._animate_reveal(self._drawer_goal)
        QTimer.singleShot(10, self._ensure_onscreen)

    def _ensure_onscreen(self) -> None:
        if not getattr(self, '_prevent_offscreen', True):
            return
        screen = self.screen()
        if not screen:
            return
        avail_geo = screen.availableGeometry()
        geo = (QRect(self.content_position(), QSize(self._text_width, self.height()))
               if self._side_switching else self.visible_geometry())
        new_x = max(avail_geo.left(), min(geo.x(), avail_geo.right() - geo.width() + 1))
        new_y = max(avail_geo.top(), min(geo.y(), avail_geo.bottom() - geo.height() + 1))
        if new_x != geo.x() or new_y != geo.y():
            self.move(self.pos() + QPoint(new_x - geo.x(), new_y - geo.y()))

    def update_display(self, stage: str, display_sec: int) -> None:
        was_overtime = self._overtime is not None
        if self._overtime:
            self._overtime = None
            self.lbl_time.clear()
        if display_sec >= 3600:
            hh, rem = divmod(display_sec, 3600)
            mm, ss = divmod(rem, 60)
            time_text = f"{hh:02d}:{mm:02d}:{ss:02d}"
        else:
            mm, ss = divmod(display_sec, 60)
            time_text = f"{mm:02d}:{ss:02d}"
            
        if was_overtime or self._current_stage_text != stage or (len(time_text) != len(self.lbl_time.text())):
            self._current_stage_text = stage
            self.lbl_time.setText(time_text)
            self._update_size()
        else:
            self.lbl_time.setText(time_text)

    def set_running(self, running: bool) -> None:
        self._is_running = running
        icon_name = "pause" if running else "play"
        self.btn_toggle.setProperty("icon_name", icon_name)
        icon_inner = max(10, getattr(self, '_icon_size', 34) - 8)
        self.btn_toggle.setIcon(get_svg_icon(icon_name, icon_inner))

    def start_flash(self, color: str, auto_stop_ms: int = 3000) -> None:
        self._alert_active = True
        self._flash_stop_timer.stop()
        self._flash_color = color
        self._flash_state = False
        self._flash_timer.start(400)
        if auto_stop_ms > 0:
            self._flash_stop_timer.start(auto_stop_ms)

    def stop_flash(self) -> None:
        self._flash_stop_timer.stop()
        self._flash_timer.stop()
        self._refresh_labels(stage_color=self._flash_color, time_color=self._flash_color)

    def apply_alert(self, color: str, keep_flashing: bool) -> None:
        was_persistent = self._flash_timer.isActive() and not self._flash_stop_timer.isActive()
        if was_persistent and not keep_flashing:
            self._flash_color = color
            self.stop_flash()
        else:
            self.start_flash(color, 0 if keep_flashing else 3000)

    def reset_alert(self) -> None:
        self._alert_active = False
        self._flash_stop_timer.stop()
        self._flash_timer.stop()
        self._flash_color = self._text_color
        self._refresh_labels()

    def show_overtime(self, stage: Stage) -> None:
        self._overtime = stage
        self.lbl_time.clear()
        if stage.timeout_display == "image":
            pixmap = QPixmap(timeout_image_path(stage.timeout_image))
            if not pixmap.isNull():
                height = max(20, int(self._font_size + 8 * self._ui_scale / 100))
                pixmap = pixmap.scaled(300, height, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                self.lbl_time.setPixmap(pixmap)
            else:
                self.lbl_time.setText(stage.timeout_text or "已超时")
        elif stage.timeout_display == "text":
            self.lbl_time.setText(stage.timeout_text or "已超时")
        else:
            self.lbl_time.setText("00:00")
        self._update_size()

    def _do_flash(self) -> None:
        self._flash_state = not self._flash_state
        self._refresh_labels()

    def enterEvent(self, e) -> None:
        self._update_drawer_direction()
        self._hide_timer.stop()
        self._set_drawer_open(True)
        super().enterEvent(e)
        
    def leaveEvent(self, e) -> None:
        self._hide_timer.start(1000)
        super().leaveEvent(e)
        
    def _do_hide_anim(self) -> None:
        if self._drag_pos is not None:
            return
        self._set_drawer_open(False)

    def _show_ctx_menu(self, pos: QPoint) -> None:
        m = QMenu(self)
        m.setStyleSheet("""
            QMenu { background:#2a2a2a; color:#eee; border:1px solid #444; border-radius:6px; padding:4px; }
            QMenu::item { padding:6px 20px; border-radius:4px; }
            QMenu::item:selected { background:#3d3d3d; }
            QMenu::separator { background:#444; height:1px; margin:4px 8px; }
        """)
        m.addAction("播放 / 暂停").triggered.connect(self.btn_toggle.click)
        m.addAction("重置当前环节").triggered.connect(self.btn_restart.click)
        m.addAction("上一环节").triggered.connect(self.btn_prev.click)
        m.addAction("下一环节").triggered.connect(self.btn_next.click)
        m.addSeparator()
        m.addAction("设置").triggered.connect(self.request_settings.emit)
        m.addAction("退出").triggered.connect(self.request_exit.emit)
        m.exec(self.mapToGlobal(pos))

    def wheelEvent(self, e: QWheelEvent) -> None:
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = e.angleDelta().y()
            if delta > 0:
                self.wheel_scrolled.emit(5)
            elif delta < 0:
                self.wheel_scrolled.emit(-5)
            e.accept()
        else:
            super().wheelEvent(e)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(self._bg_color)
        p.setPen(Qt.PenStyle.NoPen)
        radius = int(12 * getattr(self, '_ui_scale', 100) / 100.0)
        p.drawRoundedRect(self._visible_rect(), radius, radius)

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(e)

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._hide_timer.stop()
            self._drag_pos = e.globalPosition().toPoint() - self.pos()
            self._drag_start_global = e.globalPosition().toPoint()
            self._drag_content_origin = self.content_position()
            screen = (QApplication.screenAt(self._drag_start_global) or self.screen()).availableGeometry()
            self._drag_screen = screen
            self._drag_axes = {}
            for axis, value, low, high, pointer in (
                ('x', self.content_position().x(), screen.left(), screen.right()-self._text_width+1, self._drag_start_global.x()),
                ('y', self.y(), screen.top(), screen.bottom()-self.height()+1, self._drag_start_global.y()),
            ):
                edge = 'low' if abs(value-low) <= 2 else ('high' if abs(value-high) <= 2 else None)
                self._drag_axes[axis] = dict(edge=edge, pointer=pointer, released=False, bias=0)

    def _magnetic_drag_axis(self, value: int, low: int, high: int, axis: str, pointer: int) -> int:
        state = self._drag_axes[axis]
        if state['edge']:
            direction = 1 if state['edge'] == 'low' else -1
            if (pointer-state['pointer']) * direction <= self.SNAP_RELEASE_DISTANCE:
                return low if direction == 1 else high
            # Consume the release distance to avoid a jump at the threshold.
            state['bias'] -= direction * self.SNAP_RELEASE_DISTANCE
            state['edge'] = None
            state['released'] = True
        value += state['bias']
        if not state['released']:
            edge = 'low' if abs(value-low) < self.SNAP_DISTANCE else ('high' if abs(value-high) < self.SNAP_DISTANCE else None)
            if edge:
                bound = low if edge == 'low' else high
                state.update(edge=edge, pointer=pointer, bias=state['bias'] + bound-value)
                value = bound
        return max(low, min(value, high))

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self._drag_pos is not None and e.buttons() == Qt.MouseButton.LeftButton:
            pointer = e.globalPosition().toPoint()
            delta = pointer - self._drag_start_global
            content = self._drag_content_origin + delta
            new_pos = QPoint(content)
            if self._prevent_offscreen:
                screen_obj = QApplication.screenAt(e.globalPosition().toPoint())
                screen = screen_obj.availableGeometry() if screen_obj else self.screen().availableGeometry()
                new_x = new_pos.x()
                new_y = new_pos.y()

                if screen != self._drag_screen:
                    for state in self._drag_axes.values():
                        state.update(edge=None, released=True, bias=0)
                    self._drag_screen = screen
                # Anchor snapping to the timer, not a toolbar still changing sides.
                # Otherwise the old right-hand toolbar can block entry into the
                # screen's right third before the side-switch animation starts.
                new_x = self._magnetic_drag_axis(new_x, screen.left(), screen.right()-self._text_width+1, 'x', pointer.x())
                new_y = self._magnetic_drag_axis(new_y, screen.top(), screen.bottom()-self.height()+1, 'y', pointer.y())
                new_pos = QPoint(new_x, new_y)
            self.move_content(new_pos)
            self._update_drawer_direction()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        self._drag_pos = None
        self._drag_axes = {}
        QToolTip.hideText()
        self._ensure_onscreen()
        content = self.content_position()
        self.position_changed.emit(content.x(), content.y())

    def moveEvent(self, e: QMoveEvent) -> None:
        super().moveEvent(e)
        self._update_drawer_direction()

    def showEvent(self, e: QShowEvent) -> None:
        super().showEvent(e)
        self._update_drawer_direction()
        if hasattr(self, '_sampling_hwnd'):
            self._sync_capture_exclusion()

class SettingsWindow(QDialog):
    preview_requested = Signal()
    
    _SS = SETTINGS_STYLE
    PREVIEW_FIELDS = ("show_stage_label", "always_on_top", "prevent_offscreen", "color", "auto_text_color", "bg_color",
                      "bg_transparency", "font_transparency", "font", "font_size", "stage_font",
                      "stage_font_size", "ui_scale")

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self._original_config = config
        self.config = copy.deepcopy(config)
        self._backup_attrs = copy.deepcopy(config.__dict__)
        self.setWindowTitle("设置")
        self.resize(1040, 790)
        self.setMinimumSize(920, 650)
        self.setObjectName("settingsDialog")
        self.setWindowIcon(QIcon(resource_path("icon.ico")))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setStyleSheet(self._SS)
        self.setWindowFlags((self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
                            | Qt.WindowType.FramelessWindowHint)
        self._stage_rows = []
        self._alert_rows = []
        self._build_ui()
        self._populate()
        self._connect_live_preview_triggers()

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        self.btn_close = QPushButton("×")
        self.btn_close.setAccessibleName("关闭设置")
        self.btn_close.setFixedSize(30, 26)
        self.btn_close.setProperty("role", "quiet")
        self.btn_close.setStyleSheet("QPushButton { padding:0; min-height:0; font-size:22px; border:none; border-radius:5px; color:#718097; background:transparent; } QPushButton:hover { color:#b5303b; background:#fee8eb; }")
        self.btn_close.clicked.connect(self.reject)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar_panel = QWidget()
        sidebar_panel.setObjectName("settingsSidebar")
        sidebar_panel.setFixedWidth(206)
        sidebar = QVBoxLayout(sidebar_panel)
        sidebar.setContentsMargins(20, 28, 20, 20)
        sidebar.setSpacing(16)
        logo = QLabel()
        logo.setPixmap(QPixmap(resource_path("assets/guide-official-logo.png")).scaled(
            150, 44, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        sidebar.addWidget(logo)
        brand = QLabel(__app_name__)
        brand.setObjectName("brandName")
        brand.setAccessibleName(__app_name__)
        brand.setToolTip(__app_name__)
        wordmark = QPixmap(resource_path("assets/super-smart-timer-wordmark.png"))
        if not wordmark.isNull():
            brand.setPixmap(wordmark.scaled(
                166, 56, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        sidebar.addWidget(brand)
        sidebar.addSpacing(12)
        self.sidebar = QListWidget()
        self.sidebar.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.sidebar.addItems(["流程配置", "预警与声音", "外观与显示", "快捷操作"])
        self.sidebar.setAccessibleName("设置分区")
        sidebar.addWidget(self.sidebar, 1)
        version = QLabel(f'<a href="{FORK_URL}" style="color:#7b899e; text-decoration:none;">版本 {__version__}</a>')
        version.setObjectName("settingsHint")
        version.setAccessibleName("查看 fork 仓库版本")
        version.setToolTip(FORK_URL)
        version.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        version.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        version.setOpenExternalLinks(True)
        sidebar.addWidget(version)
        root.addWidget(sidebar_panel)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(28, 25, 8, 18)
        body.setSpacing(14)
        self.page_title = QLabel()
        self.page_title.setObjectName("pageTitle")
        self.page_title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        handle = SettingsDragHandle()
        handle.setAccessibleName("拖动或缩放设置窗口")
        handle.setToolTip("拖动标题行移动窗口，上沿或右沿可缩放；双击最大化或还原")
        handle_layout = QHBoxLayout(handle)
        handle_layout.setContentsMargins(0, 0, 0, 0)
        handle_layout.addWidget(self.page_title)
        handle_layout.addStretch()
        handle_layout.addWidget(self.btn_close, 0, Qt.AlignmentFlag.AlignTop)
        self.page_description = QLabel()
        self.page_description.setObjectName("pageDescription")
        body.addWidget(handle)
        body.addWidget(self.page_description)
        body.addSpacing(5)
        self.pages = QStackedWidget()
        self.pages.setContentsMargins(0, 0, 16, 0)
        self.pages.addWidget(self._build_page_stages())
        self.pages.addWidget(self._build_page_alerts())
        self.pages.addWidget(self._build_page_appearance())
        self.pages.addWidget(self._build_page_shortcuts())
        body.addWidget(self.pages, 1)
        self.status_label = QLabel()
        self.status_label.setObjectName("settingsStatus")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        body.addWidget(self.status_label)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.btn_restore = QPushButton("恢复默认")
        self.btn_restore.setProperty("role", "quiet")
        self.btn_restore.setToolTip("将所有设置恢复为默认值，保存后生效；取消可撤销。")
        self.btn_restore.clicked.connect(self._restore_defaults)
        actions.addWidget(self.btn_restore)
        actions.addStretch()
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        actions.addWidget(self.btn_cancel)
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setProperty("role", "primary")
        self.btn_save.setMinimumWidth(112)
        self.btn_save.clicked.connect(self._accept_validated)
        actions.addWidget(self.btn_save)
        actions.addWidget(QSizeGrip(self))
        actions.addSpacing(16)
        body.addLayout(actions)
        root.addWidget(content, 1)
        self.sidebar.currentRowChanged.connect(self._change_page)
        self.sidebar.setCurrentRow(0)
        self.sidebar.setFocus()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not getattr(self, '_initial_focus_set', False):
            self._initial_focus_set = True
            self.sidebar.setFocus()
            QTimer.singleShot(0, self._scroll_page_to_top)

    def _scroll_page_to_top(self) -> None:
        page = self.pages.currentWidget()
        if isinstance(page, QAbstractScrollArea):
            page.verticalScrollBar().setValue(0)

    def _change_page(self, idx: int) -> None:
        titles = [
            ("流程配置", "规划计时环节，安排放映联动与超时行为。"),
            ("预警与声音", ""),
            ("外观与显示", ""),
            ("快捷操作", ""),
        ]
        self.pages.setCurrentIndex(idx)
        self.page_title.setText(titles[idx][0])
        self.page_description.setText(titles[idx][1])
        self.page_description.setVisible(bool(titles[idx][1]))

    def _make_setting_row(self, title: str, subtitle: str, control: QWidget) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 5, 0, 5)
        layout.setSpacing(20)
        text = QVBoxLayout()
        text.setSpacing(4)
        heading = QLabel(title)
        heading.setObjectName("rowTitle")
        text.addWidget(heading)
        if subtitle:
            description = QLabel(subtitle)
            description.setObjectName("rowDescription")
            description.setWordWrap(True)
            text.addWidget(description)
        layout.addLayout(text, 1)
        control.setAccessibleName(title)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _make_card(self, title: str, layout: QLayout) -> QWidget:
        card = QFrame()
        card.setObjectName("settingsCard")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 17, 18, 18)
        outer.setSpacing(14)
        if title:
            heading = QLabel(title)
            heading.setObjectName("cardTitle")
            outer.addWidget(heading)
        inner = QWidget()
        inner.setObjectName("cardBody")
        inner.setLayout(layout)
        outer.addWidget(inner)
        return card

    def _make_sound_picker_row(self, title: str, line_edit: QLineEdit, default_duration: int = 200) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        
        lbl = QLabel(title)
        lbl.setStyleSheet("font-weight: bold; color: #333;")
        lbl.setFixedWidth(90)
        lay.addWidget(lbl)
        
        line_edit.setMinimumWidth(160)
        lay.addWidget(line_edit, 1)
        
        btn_browse = QPushButton("浏览...")
        btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        def _on_browse():
            ext = "*.wav *.mp3 *.aiff" if platform.system() == "Darwin" else "*.wav"
            path, _ = QFileDialog.getOpenFileName(self, f"选择{title}文件", "", f"音频文件 ({ext});;所有文件 (*.*)")
            if path:
                line_edit.setText(path)

        btn_browse.clicked.connect(_on_browse)
        lay.addWidget(btn_browse)
        
        btn_test = QPushButton("试听")
        btn_test.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_test.clicked.connect(lambda: play_alert_sound(line_edit.text().strip(), default_duration))
        lay.addWidget(btn_test)
        
        btn_clear = QPushButton("重置")
        btn_clear.setToolTip("重置为默认蜂鸣")
        btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear.clicked.connect(lambda: line_edit.clear())
        lay.addWidget(btn_clear)
        
        return w

    def _build_page_stages(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background: transparent; border: none;}")
        w = QWidget()
        w.setObjectName("scrollWidget")
        w.setStyleSheet("#scrollWidget{background: transparent;}")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.switch_show_label = ToggleSwitch()
        self.switch_auto_advance = ToggleSwitch()
        self.switch_ppt_auto_start = ToggleSwitch()
        self.switch_ppt_auto_pause = ToggleSwitch()
        self.switch_double_click = ToggleSwitch()
        behav_lay = QVBoxLayout()
        behav_lay.setSpacing(12)
        behav_lay.addWidget(self._make_setting_row("显示环节名称", "在主悬浮球中同步显示各小节文本标题", self.switch_show_label))
        behav_lay.addWidget(self._make_setting_row("自动进入下一环节", "关闭后，到期停留在当前环节，需手动切换；开启后自动切换并继续计时", self.switch_auto_advance))
        behav_lay.addWidget(self._make_setting_row("PPT放映自动开始/恢复计时", "进入 PowerPoint / WPS 全屏放映时生效", self.switch_ppt_auto_start))
        behav_lay.addWidget(self._make_setting_row("退出PPT自动暂停计时", "退出全屏放映时生效，可独立开关", self.switch_ppt_auto_pause))
        behav_lay.addWidget(self._make_setting_row("双击快捷操作", "双击悬浮窗空白区域快速进行开始/暂停", self.switch_double_click))
        lay.addWidget(self._make_card("行为习惯", behav_lay))
        list_lay = QVBoxLayout()
        list_lay.setSpacing(10)
        self._stage_container = QWidget()
        self._stage_vlay = QVBoxLayout(self._stage_container)
        self._stage_vlay.setContentsMargins(0, 0, 0, 0)
        self._stage_vlay.setSpacing(10)
        list_lay.addWidget(self._stage_container)
        btn_add = QPushButton("＋ 添加阶段")
        btn_add.clicked.connect(lambda: self._add_stage_row())
        list_lay.addWidget(btn_add)
        lay.addWidget(self._make_card("计时序列规划", list_lay))
        lay.addStretch()
        scroll.setWidget(w)
        return scroll

    def _build_page_alerts(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background: transparent; border: none;}")
        w = QWidget()
        w.setObjectName("scrollWidget")
        w.setStyleSheet("#scrollWidget{background: transparent;}")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        
        self.switch_global_sound = ToggleSwitch()
        self.switch_10s_sound = ToggleSwitch()
        snd_lay = QVBoxLayout()
        snd_lay.setSpacing(12)
        snd_lay.addWidget(self._make_setting_row("启用所有提示音", "全局控制蜂鸣与音频的播放许可", self.switch_global_sound))
        snd_lay.addWidget(self._make_setting_row("临近结束敲击音", "在倒计时最后10秒时，每秒触发一次提示", self.switch_10s_sound))
        
        self.txt_alert_sound = QLineEdit()
        self.txt_alert_sound.setReadOnly(True)
        self.txt_alert_sound.setPlaceholderText("系统默认蜂鸣音")
        snd_lay.addWidget(self._make_sound_picker_row("节点预警音效", self.txt_alert_sound, default_duration=200))
        
        self.txt_end_sound = QLineEdit()
        self.txt_end_sound.setReadOnly(True)
        self.txt_end_sound.setPlaceholderText("系统默认长蜂鸣")
        snd_lay.addWidget(self._make_sound_picker_row("环节结束音效", self.txt_end_sound, default_duration=2000))
        
        lay.addWidget(self._make_card("声音反馈", snd_lay))
        
        list_lay = QVBoxLayout()
        list_lay.setSpacing(10)
        self._alert_container = QWidget()
        self._alert_vlay = QVBoxLayout(self._alert_container)
        self._alert_vlay.setContentsMargins(0, 0, 0, 0)
        self._alert_vlay.setSpacing(10)
        list_lay.addWidget(self._alert_container)
        btn_add = QPushButton("＋ 新增时间阈值")
        btn_add.clicked.connect(lambda: self._add_alert_row())
        list_lay.addWidget(btn_add)
        lay.addWidget(self._make_card("时间节点变色/预警", list_lay))
        lay.addStretch()
        scroll.setWidget(w)
        return scroll

    def _build_page_appearance(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("scrollWidget")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(16)
        preview_layout = QVBoxLayout()
        self.appearance_preview = AppearancePreview()
        preview_layout.addWidget(self.appearance_preview)
        layout.addWidget(self._make_card("外观预览", preview_layout))
        colors = QVBoxLayout()
        colors.setSpacing(12)
        self.switch_auto_text_color = ToggleSwitch()
        self.switch_auto_text_color.setToolTip("透明背景自动取色需要 Windows 10 2004 或更新版本。\n该模式会在部分系统截图和录屏中隐藏计时条；不透明背景不受影响。")
        colors.addWidget(self._make_setting_row("自动文字颜色", "实时读取文字背后的颜色并取反，包含背景通透度的影响", self.switch_auto_text_color))
        self._color_preview = QPushButton()
        self._color_preview.setMinimumWidth(140)
        self._color_preview.clicked.connect(lambda: self._pick_color(self._color_preview, "_cur_color"))
        self._bg_color_preview = QPushButton()
        self._bg_color_preview.setMinimumWidth(140)
        self._bg_color_preview.clicked.connect(lambda: self._pick_color(self._bg_color_preview, "_cur_bg_color"))
        colors.addWidget(self._make_setting_row("文字颜色", "用于计时数字与环节名称", self._color_preview))
        colors.addWidget(self._make_setting_row("背景颜色", "悬浮计时条的背景底色", self._bg_color_preview))
        self._bg_op_slider = QSlider(Qt.Orientation.Horizontal)
        self._bg_op_slider.setRange(0, 100)
        self._font_trans_slider = QSlider(Qt.Orientation.Horizontal)
        self._font_trans_slider.setRange(0, 100)
        colors.addWidget(self._make_setting_row("背景通透度", "0% 不透明，100% 完全透明", self._make_slider_control(self._bg_op_slider, "%", "背景通透度")))
        colors.addWidget(self._make_setting_row("字体通透度", "0% 清晰可见，100% 完全透明", self._make_slider_control(self._font_trans_slider, "%", "字体通透度")))
        layout.addWidget(self._make_card("色彩与材质", colors))
        typography = QVBoxLayout()
        typography.setSpacing(10)
        self._font_cmb = self._create_fast_font_combobox(self.config.font)
        self._font_cmb.setMinimumWidth(250)
        self._stage_font_cmb = self._create_fast_font_combobox(self.config.stage_font)
        self._stage_font_cmb.setMinimumWidth(250)
        self._size_slider = QSlider(Qt.Orientation.Horizontal)
        self._size_slider.setRange(20, 60)
        self._stage_size_slider = QSlider(Qt.Orientation.Horizontal)
        self._stage_size_slider.setRange(12, 36)
        self._ui_scale_slider = QSlider(Qt.Orientation.Horizontal)
        self._ui_scale_slider.setRange(50, 300)
        typography.addWidget(self._make_setting_row("数字字体", "计时数字的字体", self._font_cmb))
        typography.addWidget(self._make_setting_row("数字字号", "调整倒计时的大小", self._make_slider_control(self._size_slider, " px", "数字字号")))
        typography.addWidget(self._make_setting_row("环节字体", "环节名称的字体", self._stage_font_cmb))
        typography.addWidget(self._make_setting_row("环节字号", "调整环节名称的大小", self._make_slider_control(self._stage_size_slider, " px", "环节字号")))
        typography.addWidget(self._make_setting_row("界面缩放", "同比例缩放计时条与控制按钮", self._make_slider_control(self._ui_scale_slider, "%", "界面缩放")))
        layout.addWidget(self._make_card("排版与尺寸", typography))
        windows = QVBoxLayout()
        windows.setSpacing(12)
        self.switch_always_on_top = ToggleSwitch()
        self.switch_prevent_offscreen = ToggleSwitch()
        windows.addWidget(self._make_setting_row("窗口置顶", "全屏放映时保持计时条可见", self.switch_always_on_top))
        windows.addWidget(self._make_setting_row("边缘吸附与防出界", "靠近屏幕边缘自动贴齐，避免拖出屏幕", self.switch_prevent_offscreen))
        layout.addWidget(self._make_card("窗口行为", windows))
        layout.addStretch()
        scroll.setWidget(content)
        return scroll

    def _make_slider_control(self, slider: QSlider, unit: str, name: str) -> QWidget:
        widget = QWidget()
        widget.setMinimumWidth(270)
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        slider.setAccessibleName(name)
        value = QSpinBox()
        value.setRange(slider.minimum(), slider.maximum())
        value.setSuffix(unit)
        value.setFixedWidth(78)
        value.setAccessibleName(name + "数值")
        # Keep the Python receiver alive: PySide bound-method connections alone
        # do not retain a wrapper owned only by the Qt widget hierarchy.
        slider._value_editor = value
        slider.valueChanged.connect(value.setValue)
        value.valueChanged.connect(slider.setValue)
        row.addWidget(slider, 1)
        row.addWidget(value)
        return widget

    def _build_page_shortcuts(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        txt = "点击下方输入框后在键盘上直接按下组合键即可。\n在全屏演示中也可使用，快捷键请避免与演示软件冲突。"
        help_banner = QLabel(txt)
        help_banner.setStyleSheet("color: #005a9e; background-color: #f0f7ff; border: 1px solid #cce3f5; border-radius: 6px; padding: 12px; font-size: 12px; line-height: 1.4;")
        lay.addWidget(help_banner)
        lay.addSpacing(16)
        hk_lay = QFormLayout()
        hk_lay.setSpacing(20)
        hk_lay.setVerticalSpacing(16)
        hk_lay.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.ks_toggle = QKeySequenceEdit()
        self.ks_reset = QKeySequenceEdit()
        self.ks_prev = QKeySequenceEdit()
        self.ks_next = QKeySequenceEdit()
        hk_lay.addRow(self._styled_label("播放 / 暂停 计时"), self.ks_toggle)
        hk_lay.addRow(self._styled_label("重置当前环节"), self.ks_reset)
        hk_lay.addRow(self._styled_label("切入上一小节"), self.ks_prev)
        hk_lay.addRow(self._styled_label("跃进下一小节"), self.ks_next)
        lay.addWidget(self._make_card("全局控制按键", hk_lay))
        lay.addStretch()
        return w

    def _styled_label(self, txt: str) -> QLabel:
        lbl = QLabel(txt)
        lbl.setStyleSheet(
            "font-weight: bold; "
            "color: #242424; "
            "background: transparent;"
        )
        return lbl

    def _create_fast_font_combobox(self, default_font: str) -> QComboBox:
        cmb = QComboBox()
        cmb.setMinimumWidth(100)
        cmb.addItems(QFontDatabase.families())
        self._set_combobox_font(cmb, default_font)
        return cmb

    def _set_combobox_font(self, cmb: QComboBox, font_name: str) -> None:
        idx = cmb.findText(font_name)
        if idx >= 0:
            cmb.setCurrentIndex(idx)
        else:
            cmb.addItem(font_name)
            cmb.setCurrentIndex(cmb.count() - 1)

    def _populate(self) -> None:
        source = copy.deepcopy(self.config)
        self._populating = True
        try:
            self._clear_form_rows(self._stage_vlay, self._stage_rows)
            self._clear_form_rows(self._alert_vlay, self._alert_rows)
            self.switch_auto_advance.setChecked(source.auto_advance)
            self.switch_show_label.setChecked(source.show_stage_label)
            self.switch_ppt_auto_start.setChecked(source.ppt_auto_start)
            self.switch_ppt_auto_pause.setChecked(source.ppt_auto_pause)
            self.switch_double_click.setChecked(source.double_click_toggle)
            for s in source.stages:
                self._add_stage_row(**asdict(s))
            if not source.stages:
                self._add_stage_row()
            self.switch_global_sound.setChecked(source.global_sound)
            self.switch_10s_sound.setChecked(source.countdown_10s_sound)
            self.txt_alert_sound.setText(source.alert_sound_path)
            self.txt_end_sound.setText(source.end_sound_path)
            for a in source.alerts:
                self._add_alert_row(**asdict(a))
            self.switch_always_on_top.setChecked(source.always_on_top)
            self.switch_prevent_offscreen.setChecked(source.prevent_offscreen)
            self._cur_color = source.color
            self.switch_auto_text_color.setChecked(source.auto_text_color)
            self._color_preview.setEnabled(not source.auto_text_color)
            self._set_color_preview(self._color_preview, self._cur_color)
            self._cur_bg_color = source.bg_color
            self._set_color_preview(self._bg_color_preview, self._cur_bg_color)
            self._bg_op_slider.setValue(source.bg_transparency)
            self._font_trans_slider.setValue(source.font_transparency)
            self._set_combobox_font(self._font_cmb, source.font)
            self._size_slider.setValue(source.font_size)
            self._set_combobox_font(self._stage_font_cmb, source.stage_font)
            self._stage_size_slider.setValue(source.stage_font_size)
            self._ui_scale_slider.setValue(source.ui_scale)
            for slider in (self._bg_op_slider, self._font_trans_slider, self._size_slider,
                           self._stage_size_slider, self._ui_scale_slider):
                slider._value_editor.setValue(slider.value())
            self.ks_toggle.setKeySequence(QKeySequence(source.shortcut_toggle))
            self.ks_reset.setKeySequence(QKeySequence(source.shortcut_reset))
            self.ks_prev.setKeySequence(QKeySequence(source.shortcut_prev))
            self.ks_next.setKeySequence(QKeySequence(source.shortcut_next))
        finally:
            self._populating = False
        self.appearance_preview.set_config(self.config)

    @staticmethod
    def _clear_form_rows(layout: QVBoxLayout, rows: list) -> None:
        rows.clear()
        while layout.count():
            widget = layout.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()

    def _add_stage_row(self, label: str = "新阶段", duration: int = 3, unit: str = "分", count_up: bool = False,
                       timeout_display: str = "zero", timeout_text: str = "已超时",
                       timeout_image: str = "", timeout_close_show: bool = False) -> None:
        row_widget = QWidget()
        row_widget.setObjectName("stageRow")

        card = QVBoxLayout(row_widget)
        card.setContentsMargins(12, 8, 12, 8)
        h = QHBoxLayout()
        card.addLayout(h)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        name = QLineEdit()
        name.setText(str(label))
        name.setPlaceholderText("环节名称（留空则隐藏）")
        name.setMaxLength(100)
        h.addWidget(name, 2)
        spin = QSpinBox()
        spin.setRange(1, 9999)
        spin.setValue(duration)
        spin.setMinimumWidth(65)
        h.addWidget(spin)
        unit_cmb = QComboBox()
        unit_cmb.addItems(["分", "秒"])
        unit_cmb.setCurrentText(str(unit))
        unit_cmb.setMinimumWidth(55)
        h.addWidget(unit_cmb)
        dir_cmb = QComboBox()
        dir_cmb.addItems(["倒计时", "正计时"])
        dir_cmb.setCurrentText("正计时" if count_up else "倒计时")
        dir_cmb.setMinimumWidth(75)
        h.addWidget(dir_cmb)
        row = {"widget": row_widget, "name": name, "spin": spin, "unit_cmb": unit_cmb, "dir_cmb": dir_cmb}
        self._add_row_controls(h, self._stage_rows, row, self._rebuild_stage_rows)
        overtime = QGroupBox("超时行为（仅倒计时）")

        grid = QGridLayout(overtime)
        display = QComboBox()
        for text, value in (("显示 00:00", "zero"), ("显示文字", "text"), ("显示图片", "image")):
            display.addItem(text, value)
        display.setCurrentIndex(max(0, display.findData(timeout_display)))
        grid.addWidget(QLabel("到零后显示"), 0, 0)
        grid.addWidget(display, 0, 1)
        close_show = OptionCheckBox("超时关闭放映（PowerPoint / WPS）")
        close_show.setChecked(timeout_close_show)
        grid.addWidget(close_show, 0, 2)
        text_edit = QLineEdit(timeout_text)
        text_edit.setMaxLength(100)
        text_edit.setPlaceholderText("已超时")
        grid.addWidget(text_edit, 1, 0, 1, 3)
        image_panel = QWidget()
        image_layout = QVBoxLayout(image_panel)
        image_layout.setContentsMargins(0, 0, 0, 0)
        source_layout = QHBoxLayout()
        image_preset = QComboBox()
        image_preset.addItem("自定义图片", "")
        for key, (title, filename) in OVERTIME_PRESETS.items():
            value = "preset:" + key
            image_preset.addItem(QIcon(timeout_image_path(value)), title, value)
        image_preset.setAccessibleName("超时图片来源")
        image_preset.setCurrentIndex(max(0, image_preset.findData(timeout_image)))
        image_edit = QLineEdit(timeout_image)
        image_edit.setReadOnly(True)
        image_edit.setPlaceholderText("选择超时后显示的图片")
        image_pick = QPushButton("选择图片…")
        source_layout.addWidget(image_preset, 1)
        source_layout.addWidget(image_pick)
        image_layout.addLayout(source_layout)
        image_layout.addWidget(image_edit)
        image_preview = QLabel()
        image_preview.setAccessibleName("超时图片预览")
        image_preview.setMinimumHeight(48)
        image_layout.addWidget(image_preview)
        def refresh_image_preview():
            value = image_edit.text()
            image_edit.setVisible(not value.startswith("preset:"))
            pixmap = QPixmap(timeout_image_path(value))
            image_preview.clear()
            if not pixmap.isNull():
                image_preview.setPixmap(pixmap.scaled(210, 64, Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation))
        def choose_preset():
            value = image_preset.currentData()
            if value or image_edit.text().startswith("preset:"):
                image_edit.setText(value)
            refresh_image_preview()
        image_preset.currentIndexChanged.connect(choose_preset)
        image_edit.textChanged.connect(refresh_image_preview)
        refresh_image_preview()
        grid.addWidget(image_panel, 2, 0, 1, 3)
        hint = QLabel("关闭“自动进入下一环节”可保留超时显示；退出放映不会关闭文稿或软件。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666; font-size:11px;")
        grid.addWidget(hint, 3, 0, 1, 3)
        def select_image():
            path, _ = QFileDialog.getOpenFileName(self, "选择超时图片", image_edit.text(),
                                                 "图片 (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.svg)")
            if path:
                image_preset.setCurrentIndex(0)
                image_edit.setText(path)
        image_pick.clicked.connect(select_image)
        def update_timeout_controls():
            overtime.setEnabled(dir_cmb.currentText() == "倒计时")
            text_edit.setVisible(display.currentData() == "text")
            image_panel.setVisible(display.currentData() == "image")
        display.currentIndexChanged.connect(update_timeout_controls)
        dir_cmb.currentIndexChanged.connect(update_timeout_controls)
        update_timeout_controls()
        card.addWidget(overtime)
        row.update(timeout_display=display, timeout_text=text_edit,
                   timeout_image=image_edit, timeout_close_show=close_show,
                   timeout_preset=image_preset, timeout_image_preview=image_preview)
        self._stage_rows.append(row)
        self._stage_vlay.addWidget(row_widget)

    def _rebuild_stage_rows(self) -> None:
        self._rebuild_rows(self._stage_vlay, self._stage_rows,
                           lambda r: self._add_stage_row(**asdict(self._stage_from_row(r))))

    @staticmethod
    def _stage_from_row(row: dict) -> Stage:
        return Stage(row["name"].text(), row["spin"].value(), row["unit_cmb"].currentText(),
                     row["dir_cmb"].currentText() == "正计时", row["timeout_display"].currentData(),
                     row["timeout_text"].text(), row["timeout_image"].text(),
                     row["timeout_close_show"].isChecked())

    def _add_alert_row(self, seconds: int = 20, color: str = "#ffaa00", play_sound: bool = True,
                       keep_flashing: bool = False) -> None:
        row_widget = QWidget()
        row_widget.setObjectName("alertRow")

        h = QHBoxLayout(row_widget)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        lbl = QLabel("剩余时长：")
        h.addWidget(lbl)
        spin = QSpinBox()
        spin.setRange(1, 9999)
        spin.setSuffix(" 秒")
        spin.setValue(seconds)
        spin.setMinimumWidth(85)
        h.addWidget(spin)
        cbtn = QPushButton()
        cbtn.setFixedSize(32, 32)
        cbtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_color_preview(cbtn, color)
        chk_sound = OptionCheckBox("伴随音效")
        chk_sound.setChecked(play_sound)
        chk_flash = OptionCheckBox("保持闪烁")
        chk_flash.setChecked(keep_flashing)
        row = {"widget": row_widget, "spin": spin, "color": color, "cbtn": cbtn,
               "chk_sound": chk_sound, "chk_flash": chk_flash}
        cbtn.clicked.connect(partial(self._pick_alert_color, row))
        h.addWidget(cbtn)
        h.addWidget(chk_sound)
        h.addWidget(chk_flash)
        h.addStretch()
        self._add_row_controls(h, self._alert_rows, row, self._rebuild_alert_rows)
        self._alert_rows.append(row)
        self._alert_vlay.addWidget(row_widget)

    def _pick_alert_color(self, row: dict) -> None:
        c = QColorDialog.getColor(QColor(row["color"]), self)
        if c.isValid():
            row["color"] = c.name()
            self._set_color_preview(row["cbtn"], c.name())

    def _rebuild_alert_rows(self) -> None:
        self._rebuild_rows(self._alert_vlay, self._alert_rows,
                           lambda r: self._add_alert_row(r["spin"].value(), r["color"], r["chk_sound"].isChecked(), r["chk_flash"].isChecked()))

    def _add_row_controls(self, layout: QHBoxLayout, rows: list, row: dict, rebuild_fn) -> None:
        layout.addSpacing(10)
        btn_up = QPushButton()
        btn_up.setFixedSize(28, 28)
        btn_up.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_up.setIcon(get_svg_icon("up", 14, "#666666"))
        btn_up.setStyleSheet("QPushButton { background:transparent; border:none; border-radius:4px; padding:0; } QPushButton:hover { background:#e1e1e1; }")
        btn_up.clicked.connect(partial(self._move_row, rows, row, -1, rebuild_fn))
        layout.addWidget(btn_up)
        btn_down = QPushButton()
        btn_down.setFixedSize(28, 28)
        btn_down.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_down.setIcon(get_svg_icon("down", 14, "#666666"))
        btn_down.setStyleSheet("QPushButton { background:transparent; border:none; border-radius:4px; padding:0; } QPushButton:hover { background:#e1e1e1; }")
        btn_down.clicked.connect(partial(self._move_row, rows, row, 1, rebuild_fn))
        layout.addWidget(btn_down)
        btn_del = QPushButton()
        btn_del.setFixedSize(28, 28)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setIcon(get_svg_icon("trash", 14, "#d13438"))
        btn_del.setStyleSheet("QPushButton { background:transparent; border:none; border-radius:4px; padding:0; } QPushButton:hover { background:#fde7e9; }")
        btn_del.clicked.connect(partial(self._delete_row, rows, row, rebuild_fn))
        layout.addWidget(btn_del)

    def _move_row(self, rows: list, row: dict, delta: int, rebuild_fn) -> None:
        idx = rows.index(row)
        new_idx = idx + delta
        if 0 <= new_idx < len(rows):
            rows[idx], rows[new_idx] = rows[new_idx], rows[idx]
            rebuild_fn()

    def _delete_row(self, rows: list, row: dict, rebuild_fn) -> None:
        if len(rows) > 1:
            rows.remove(row)
            rebuild_fn()

    def _rebuild_rows(self, layout: QVBoxLayout, rows: list, add_fn) -> None:
        saved = list(rows)
        rows.clear()
        while layout.count():
            w = layout.takeAt(0).widget()
            if w: w.deleteLater()
        for r in saved: 
            add_fn(r)

    def _pick_color(self, btn: QPushButton, attr_name: str) -> None:
        current_color = getattr(self, attr_name)
        c = QColorDialog.getColor(QColor(current_color), self)
        if c.isValid():
            setattr(self, attr_name, c.name())
            self._set_color_preview(btn, c.name())
            self._trigger_live_preview() 

    @staticmethod
    def _set_color_preview(widget: QWidget, color: str) -> None:
        swatch = QPixmap(18, 18)
        swatch.fill(QColor(color))
        widget.setIcon(QIcon(swatch))
        widget.setToolTip(color.upper())
        if widget.width() <= 40:
            widget.setText("")
            widget.setStyleSheet("padding:0;")
        else:
            widget.setText(color.upper())

    def _restore_defaults(self) -> None:
        # Restore only the draft. Existing timers/flows keep running until Save.
        positions = {key: getattr(self.config, key) for key in ("pos_x", "pos_y")}
        self.config.__dict__.clear()
        self.config.__dict__.update(copy.deepcopy(Config().__dict__))
        self.config.__dict__.update(positions)
        self._populate()
        self._trigger_live_preview()
        self.status_label.setText("已恢复默认值。保存设置后生效，取消可撤销。")
        self.status_label.show()

    def _validate_shortcuts(self) -> bool:
        mapping = {
            "【播放 / 暂停】": self.ks_toggle.keySequence().toString(),
            "【重置当前小节】": self.ks_reset.keySequence().toString(),
            "【上一环节】": self.ks_prev.keySequence().toString(),
            "【下一环节】": self.ks_next.keySequence().toString()
        }
        active_shortcuts = {name: seq for name, seq in mapping.items() if seq.strip()}
        seen = {}
        for name, seq in active_shortcuts.items():
            if seq in seen:
                QMessageBox.critical(
                    self, "快捷键冲突", 
                    f"保存失败：您的“{name}”与“{seen[seq]}”指定了同一组按键：【{seq}】。\n\n请修改不一致的按键后再行应用。"
                )
                return False
            seen[seq] = name
        return True

    def _connect_live_preview_triggers(self) -> None:
        self.switch_show_label.toggled.connect(self._trigger_live_preview)
        self.switch_always_on_top.toggled.connect(self._trigger_live_preview)
        self.switch_prevent_offscreen.toggled.connect(self._trigger_live_preview)
        self.switch_auto_text_color.toggled.connect(self._trigger_live_preview)
        self._bg_op_slider.valueChanged.connect(self._trigger_live_preview)
        self._font_trans_slider.valueChanged.connect(self._trigger_live_preview)
        self._size_slider.valueChanged.connect(self._trigger_live_preview)
        self._stage_size_slider.valueChanged.connect(self._trigger_live_preview)
        self._ui_scale_slider.valueChanged.connect(self._trigger_live_preview)
        self._font_cmb.currentTextChanged.connect(self._trigger_live_preview)
        self._stage_font_cmb.currentTextChanged.connect(self._trigger_live_preview)

    def _trigger_live_preview(self) -> None:
        if getattr(self, '_populating', False):
            return
        self.config.show_stage_label = self.switch_show_label.isChecked()
        self.config.always_on_top = self.switch_always_on_top.isChecked()
        self.config.prevent_offscreen = self.switch_prevent_offscreen.isChecked()
        self.config.color = self._cur_color
        self.config.auto_text_color = self.switch_auto_text_color.isChecked()
        self._color_preview.setEnabled(not self.config.auto_text_color)
        self.config.bg_color = self._cur_bg_color
        self.config.bg_transparency = self._bg_op_slider.value()
        self.config.font_transparency = self._font_trans_slider.value()
        self.config.font = self._font_cmb.currentText()
        self.config.font_size = self._size_slider.value()
        self.config.stage_font = self._stage_font_cmb.currentText()
        self.config.stage_font_size = self._stage_size_slider.value()
        self.config.ui_scale = self._ui_scale_slider.value()
        self.appearance_preview.set_config(self.config)
        self.preview_requested.emit()

    def restore_backup(self) -> None:
        for key in self.PREVIEW_FIELDS:
            setattr(self._original_config, key, copy.deepcopy(self._backup_attrs[key]))

    def reject(self) -> None:
        self.restore_backup()
        super().reject()

    def _accept_validated(self) -> None:
        if not self._validate_shortcuts():
            return
        for row in self._stage_rows:
            stage = self._stage_from_row(row)
            if not stage.count_up and stage.timeout_display == "image":
                if not QImageReader(timeout_image_path(stage.timeout_image)).canRead():
                    QMessageBox.warning(self, "图片不可用", "请选择有效的超时图片，或改为显示文字。")
                    return
        self.accept()

    def get_config(self) -> Config:
        self.config.auto_advance = self.switch_auto_advance.isChecked()
        self.config.show_stage_label = self.switch_show_label.isChecked()
        self.config.ppt_auto_start = self.switch_ppt_auto_start.isChecked()
        self.config.ppt_auto_pause = self.switch_ppt_auto_pause.isChecked()
        self.config.double_click_toggle = self.switch_double_click.isChecked()
        self.config.global_sound = self.switch_global_sound.isChecked()
        self.config.countdown_10s_sound = self.switch_10s_sound.isChecked()
        self.config.alert_sound_path = self.txt_alert_sound.text().strip()
        self.config.end_sound_path = self.txt_end_sound.text().strip()
        self.config.always_on_top = self.switch_always_on_top.isChecked()
        self.config.prevent_offscreen = self.switch_prevent_offscreen.isChecked()
        self.config.stages = [self._stage_from_row(r) for r in self._stage_rows]
        self.config.alerts = [Alert(r["spin"].value(), r["color"], r["chk_sound"].isChecked(), r["chk_flash"].isChecked()) for r in self._alert_rows]
        self.config.color = self._cur_color
        self.config.auto_text_color = self.switch_auto_text_color.isChecked()
        self.config.bg_color = self._cur_bg_color
        self.config.bg_transparency = self._bg_op_slider.value()
        self.config.font_transparency = self._font_trans_slider.value()
        self.config.font = self._font_cmb.currentText()
        self.config.font_size = self._size_slider.value()
        self.config.stage_font = self._stage_font_cmb.currentText()
        self.config.stage_font_size = self._stage_size_slider.value()
        self.config.ui_scale = self._ui_scale_slider.value()
        self.config.shortcut_toggle = self.ks_toggle.keySequence().toString()
        self.config.shortcut_reset = self.ks_reset.keySequence().toString()
        self.config.shortcut_prev = self.ks_prev.keySequence().toString()
        self.config.shortcut_next = self.ks_next.keySequence().toString()
        return self.config

class HotkeyBridge(QObject):
    toggle = Signal()
    reset = Signal()
    prev = Signal()
    next = Signal()

class App(QObject):
    def __init__(self):
        super().__init__()
        self.config = Config()
        self.config.load()
        self._settings_window = None
        QApplication.instance().setWindowIcon(QIcon(resource_path("icon.ico")))
        self.float_bar = FloatBar()
        self.float_bar.setWindowTitle(__app_name__)
        self.controller = TimerController(self.config)
        self._shortcuts = []
        self.hotkey_bridge = HotkeyBridge()
        self.hotkey_bridge.toggle.connect(lambda: self._handle_user_action(self.controller.toggle_pause))
        self.hotkey_bridge.reset.connect(lambda: self._handle_user_action(self.controller.restart_stage, True))
        self.hotkey_bridge.prev.connect(lambda: self._handle_user_action(self.controller.prev_stage, True))
        self.hotkey_bridge.next.connect(lambda: self._handle_user_action(self.controller.next_stage, True))
        self._ppt_was_active = False
        self._ppt_stable_active_count = 0
        self._current_ppt_session_paused_by_user = False
        self._ppt_monitor_timer = QTimer(self)
        self._ppt_monitor_timer.timeout.connect(self._check_ppt_status)
        self._ppt_monitor_timer.start(600)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_config)
        self._connect_signals()
        self._apply_style()
        self._apply_shortcuts()
        self.controller.stop()
        
        if self.config.pos_x >= 0 and self.config.pos_y >= 0:
            self.float_bar.move_content(QPoint(self.config.pos_x, self.config.pos_y))
        else:
            screen = self.float_bar.screen().availableGeometry()
            self.float_bar.move_content(QPoint(screen.left() + 40, screen.top() + 40))
            
        self.float_bar.show()

    def _check_ppt_status(self) -> None:
        if not (self.config.ppt_auto_start or self.config.ppt_auto_pause):
            self._ppt_was_active = False
            self._ppt_stable_active_count = 0
            self._current_ppt_session_paused_by_user = False
            return
        is_active = is_ppt_slideshow_active()
        if is_active:
            self._ppt_stable_active_count += 1
            if self._ppt_stable_active_count >= 2 and not self._ppt_was_active:
                self._ppt_was_active = True
                self._current_ppt_session_paused_by_user = False
                if self.config.ppt_auto_start and self.controller.paused and self.controller._remaining_float > 0:
                    self.controller.toggle_pause()
            elif self._ppt_stable_active_count >= 2:
                if self.config.ppt_auto_start and self.controller.paused and self.controller._remaining_float > 0:
                    if not self._current_ppt_session_paused_by_user:
                        self.controller.toggle_pause()
        else:
            self._ppt_stable_active_count = 0
            if self._ppt_was_active:
                self._ppt_was_active = False
                self._current_ppt_session_paused_by_user = False
                if self.config.ppt_auto_pause and not self.controller.paused:
                    self.controller.toggle_pause()

    def _on_user_interaction(self):
        if is_ppt_slideshow_active():
            self._current_ppt_session_paused_by_user = True

    def _connect_signals(self) -> None:
        fb = self.float_bar
        ctrl = self.controller
        for btn in (fb.btn_toggle, fb.btn_restart, fb.btn_prev, fb.btn_next):
            btn.clicked.connect(self._on_user_interaction)
        fb.btn_toggle.clicked.connect(ctrl.toggle_pause)
        fb.btn_restart.clicked.connect(ctrl.restart_stage)
        fb.btn_prev.clicked.connect(ctrl.prev_stage)
        fb.btn_next.clicked.connect(ctrl.next_stage)
        fb.btn_restart.clicked.connect(self._on_stage_reset)
        fb.btn_prev.clicked.connect(self._on_stage_reset)
        fb.btn_next.clicked.connect(self._on_stage_reset)
        fb.btn_settings.clicked.connect(self._open_settings)
        fb.btn_close.clicked.connect(self._exit)
        fb.request_settings.connect(self._open_settings)
        fb.request_exit.connect(self._exit)
        fb.double_clicked.connect(self._on_double_clicked)
        fb.wheel_scrolled.connect(self._on_wheel_scrolled)
        fb.position_changed.connect(self._on_position_changed)
        ctrl.tick.connect(lambda lbl, rem: fb.update_display(lbl, rem))
        ctrl.alert_triggered.connect(fb.apply_alert)
        ctrl.stage_changed.connect(lambda *_: fb.reset_alert())
        ctrl.stage_expired.connect(self._on_stage_expired)
        ctrl.loop_restarted.connect(lambda: fb.start_flash(self.config.color, 1500))
        ctrl.state_changed.connect(fb.set_running)

    def _on_stage_expired(self, stage: Stage) -> None:
        if not self.config.auto_advance:
            self.float_bar.show_overtime(stage)
        if stage.timeout_close_show:
            close_ppt_slideshows()

    def _save_config(self) -> None:
        # Do not persist unaccepted live-preview settings when the bar is dragged.
        if self._settings_window is None:
            self.config.save()

    def _handle_user_action(self, action, reset: bool = False) -> None:
        self._on_user_interaction()
        action()
        if reset:
            self._on_stage_reset()

    def _on_position_changed(self, x: int, y: int) -> None:
        self.config.pos_x = x
        self.config.pos_y = y
        self._save_timer.start(500)

    def _on_wheel_scrolled(self, delta: int) -> None:
        new_scale = max(50, min(300, self.config.ui_scale + delta))
        if new_scale != self.config.ui_scale:
            self.config.ui_scale = new_scale
            self._apply_style()
            self._save_timer.start(500) 

    def _on_double_clicked(self) -> None:
        if self.config.double_click_toggle:
            self._on_user_interaction()
            self.controller.toggle_pause()

    def _apply_style(self) -> None:
        c = self.config
        opacity_float = 1.0
        self.float_bar.apply_style(c.color, c.font, c.font_size, c.stage_font, c.stage_font_size,
                                   opacity_float, c.bg_qcolor(), c.always_on_top, c.show_stage_label, 
                                   c.prevent_offscreen, c.font_transparency, c.ui_scale, c.auto_text_color)

    def _apply_shortcuts(self) -> None:
        for sc in self._shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._shortcuts.clear()
        if HAS_KEYBOARD:
            keyboard.unhook_all()
            mapping = [
                (self.config.shortcut_toggle, self.hotkey_bridge.toggle),
                (self.config.shortcut_reset, self.hotkey_bridge.reset),
                (self.config.shortcut_prev, self.hotkey_bridge.prev),
                (self.config.shortcut_next, self.hotkey_bridge.next),
            ]
            for key_str, signal in mapping:
                if key_str:
                    k_str = key_str.lower().replace("return", "enter")
                    try:
                        keyboard.add_hotkey(k_str, signal.emit, suppress=False)
                    except Exception:
                        pass
        else:
            mapping_qt = [
                (self.config.shortcut_toggle, lambda: self._handle_user_action(self.controller.toggle_pause)),
                (self.config.shortcut_reset, lambda: self._handle_user_action(self.controller.restart_stage, True)),
                (self.config.shortcut_prev, lambda: self._handle_user_action(self.controller.prev_stage, True)),
                (self.config.shortcut_next, lambda: self._handle_user_action(self.controller.next_stage, True)),
            ]
            for key_str, slot_func in mapping_qt:
                if key_str:
                    sc = QShortcut(QKeySequence(key_str), self.float_bar)
                    sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
                    sc.activated.connect(slot_func)
                    self._shortcuts.append(sc)

    def _open_settings(self) -> None:
        if self._settings_window is not None:
            self._settings_window.show()
            self._settings_window.raise_()
            self._settings_window.activateWindow()
            return
        dlg = SettingsWindow(self.config)
        self._settings_window = dlg
        dlg.preview_requested.connect(lambda: self._preview_settings(dlg))
        dlg.finished.connect(lambda res: self._finish_settings(dlg, res))
        dlg.show()

    def _preview_settings(self, dlg: SettingsWindow) -> None:
        for key in dlg.PREVIEW_FIELDS:
            setattr(self.config, key, copy.deepcopy(getattr(dlg.config, key)))
        self._apply_style()

    def _finish_settings(self, dlg: SettingsWindow, res: int) -> None:
        if res == QDialog.DialogCode.Rejected:
            self._apply_style()
        else:
            self._save_settings(dlg)
        self._settings_window = None
        self.config.save()
        dlg.deleteLater()

    def _save_settings(self, dlg: SettingsWindow) -> None:
        if not dlg._validate_shortcuts():
            return
        updated = dlg.get_config()
        for key, value in updated.__dict__.items():
            if key not in {"pos_x", "pos_y"}:
                setattr(self.config, key, copy.deepcopy(value))
        self.config.save()
        self._apply_style()
        self._apply_shortcuts()
        self.controller.config = self.config
        self.controller.stop()

    def _exit(self) -> None:
        if self._settings_window is not None:
            self._settings_window.reject()
        self.controller.stop()
        self._ppt_monitor_timer.stop()
        if HAS_KEYBOARD:
            keyboard.unhook_all()
        for sc in self._shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self.float_bar.close()
        QApplication.quit()

    def _on_stage_reset(self) -> None:
        self._current_ppt_session_paused_by_user = False
        if is_ppt_slideshow_active() and self.config.ppt_auto_start:
            if self.controller.paused and self.controller._remaining_float > 0:
                self.controller.toggle_pause()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    app.setApplicationName(__app_name__)
    app.setApplicationDisplayName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    translator = QTranslator()
    locale = QLocale.system().name()  
    if translator.load(f"qt_{locale}", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)
    shared_mem_key = f"{APP_ID}_SingleInstance_MemoryLock"
    shared_mem = QSharedMemory(shared_mem_key)
    
    if not shared_mem.create(1):
        if shared_mem.attach():
            QMessageBox.warning(None, "程序已启动", f"{__app_name__} 已在运行中！")
            sys.exit(0)
        else:
            shared_mem.create(1)
            
    main = App()
    exit_code = app.exec()
    if shared_mem.isAttached():
        shared_mem.detach()
    sys.exit(exit_code)
