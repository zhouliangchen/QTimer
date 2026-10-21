"""Local Windows compositor check against owned solid-color test windows."""
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
back = q.QWidget()
back.setWindowFlags(q.Qt.WindowType.FramelessWindowHint | q.Qt.WindowType.WindowStaysOnTopHint)
screen = app.primaryScreen().availableGeometry()
back.setGeometry(screen.x()+100, screen.y()+100, 750, 300)
bar = q.FloatBar()
bar.update_display('Test', 60)
bar.move(screen.x()+200, screen.y()+170)
try:
    back.show()
    bar.apply_style('#ffffff', 'Arial', 32, 'Arial', 18, 1,
                    q.QColor(0, 0, 0, 1), True, True, True, 0, 100, True)
    bar.show()
    bar.raise_()
    for color in ('#dc2850', '#1ea0dc', '#ffffff', '#000000'):
        back.setStyleSheet('background:' + color)
        QTest.qWait(400)
        expected = q.inverse_background(q.QColor(0, 0, 0, 1), q.QColor(color)).name()
        bar._update_auto_colors()
        assert bar._auto_colors == [expected, expected], (color, bar._auto_colors, expected)
        QTest.qWait(400)
        assert bar._auto_colors == [expected, expected], 'self-capture feedback detected'
        print(color, '->', expected, 'stable')
    bar.apply_style('#abcdef', 'Arial', 32, 'Arial', 18, 1,
                    q.QColor('black'), True, True, True, 0, 100, False)
    get_affinity = ctypes.windll.user32.GetWindowDisplayAffinity
    get_affinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    affinity = wintypes.DWORD()
    assert get_affinity(int(bar.winId()), ctypes.byref(affinity))
    assert affinity.value == 0, 'capture exclusion was not removed'
    print('manual mode restores capture visibility')
finally:
    bar.close()
    back.close()
