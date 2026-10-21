"""Native timer/settings smoke test using an isolated temporary configuration."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'windows'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as q
from PySide6.QtTest import QTest

app = q.QApplication([])
app.setStyle('Fusion')
with tempfile.TemporaryDirectory() as root, patch.object(q, 'CONFIG_PATH', str(Path(root)/'config.json')):
    config = q.Config()
    config.global_sound = config.ppt_auto_start = config.ppt_auto_pause = False
    config.stages = [q.Stage('Native', 30, '秒')]
    assert config.save()
    with patch.object(q, 'HAS_KEYBOARD', False):
        instance = q.App()
    try:
        instance.controller.toggle_pause()
        deadline = instance.controller._target_time
        QTest.qWait(300)
        instance._open_settings()
        dialog = instance._settings_window
        dialog._font_trans_slider.setValue(12)
        QTest.mouseClick(dialog.btn_save, q.Qt.MouseButton.LeftButton)
        assert instance._settings_window is None
        assert instance.controller._target_time == deadline
        assert not instance.controller.paused
        QTest.qWait(300)
        assert 28 < instance.controller._remaining_float < 30
        print('native settings save: deadline preserved, timer continues')
        instance.float_bar.move_content(q.QPoint(180, 160))
        instance._on_position_changed(180, 160)
        with patch.object(q, 'HAS_KEYBOARD', False), patch.object(q.QApplication, 'quit'):
            instance._exit()
        restored = q.Config()
        restored.load()
        assert (restored.pos_x, restored.pos_y) == (180, 160)
        print('native shutdown: pending position saved')
    finally:
        if instance._settings_window:
            instance._settings_window.reject()
        instance.controller._timer.stop()
        instance._ppt_monitor_timer.stop()
        instance._save_timer.stop()
        instance.float_bar.close()
