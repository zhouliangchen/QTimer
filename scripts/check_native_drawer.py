"""Verify native/high-DPI drawer frames against owned Qt-rendered text pixels."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys

os.environ['QT_QPA_PLATFORM'] = 'windows'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as q
from PySide6.QtTest import QTest

app = q.QApplication([])
bar = q.FloatBar()
bar.apply_style('#ffffff', 'Microsoft YaHei', 32, 'Microsoft YaHei', 18,
                1.0, q.QColor('#141414'), True, True, True, 0)
bar.update_display('Presentation', 300)
screen = app.primaryScreen().availableGeometry()
bar.move_content(q.QPoint(screen.right() - bar._text_width + 1, screen.top() + 90))
back = q.QWidget()
back.setWindowFlags(q.Qt.WindowType.FramelessWindowHint | q.Qt.WindowType.WindowStaysOnTopHint)
back.setGeometry(bar.x()-10, bar.y()-10, bar.width()+20, bar.height()+20)
back.setStyleSheet('background:#edf2f9')
back.show()
bar.show()
bar.raise_()
QTest.qWait(50)
bar._anim_group.stop()
bar._hide_timer.stop()
bar.drawer_reveal = 0
frame = bar.geometry()
anchor = bar.content_position()
positions = [label.mapToGlobal(q.QPoint()) for label in (bar.lbl_stage, bar.lbl_time)]

def text_pixels():
    return [bar.grab(q.QRect(label.mapTo(bar, q.QPoint()), label.size())).toImage()
            for label in (bar.lbl_stage, bar.lbl_time)]

reference = text_pixels()
try:
    user = ctypes.windll.user32
    user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user.WindowFromPoint.argtypes = [wintypes.POINT]
    user.WindowFromPoint.restype = wintypes.HWND
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    def hit_at(x):
        point = wintypes.POINT(round(x*bar.devicePixelRatioF()), round(bar.height()/2*bar.devicePixelRatioF()))
        assert user.ClientToScreen(int(bar.winId()), ctypes.byref(point))
        return user.GetAncestor(user.WindowFromPoint(point), 2)
    assert hit_at(10) == int(back.winId()), 'hidden drawer intercepts clicks'
    assert hit_at(bar._drawer_space+30) == int(bar.winId()), 'timer is not clickable'
    for progress in list(range(21)) + list(reversed(range(21))):
        bar.drawer_reveal = progress / 20
        app.processEvents()
        assert bar.geometry() == frame
        assert bar.content_position() == anchor
        assert [label.mapToGlobal(q.QPoint()) for label in (bar.lbl_stage, bar.lbl_time)] == positions
        assert text_pixels() == reference, 'text pixels changed at reveal ' + str(progress)
        assert bar.mask().boundingRect() == bar._visible_rect()
    bar.drawer_reveal = 1
    app.processEvents()
    assert hit_at(10) == int(bar.winId()), 'expanded left drawer is not clickable'
    print('DPR',bar.devicePixelRatioF(),': 42 identical text frames; native hit testing excludes hidden controls')
finally:
    bar.close()
    back.close()
