"""Regression coverage for timing, transactional saves and recoverable state."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_features import q, APP
import config_storage


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'config.json'
        self.config_patch = patch.object(q, 'CONFIG_PATH', str(self.path))
        self.config_patch.start()
        self.instances = []
        self.widgets = []

    def tearDown(self):
        for instance in self.instances:
            if instance._settings_window:
                instance._settings_window.reject()
            instance.controller._timer.stop()
            instance._ppt_monitor_timer.stop()
            instance._save_timer.stop()
            instance.float_bar.close()
        for widget in self.widgets:
            widget.close()
        APP.processEvents()
        self.config_patch.stop()
        self.temp.cleanup()

    def instance(self):
        config = q.Config()
        config.global_sound = False
        config.ppt_auto_start = False
        config.ppt_auto_pause = False
        config.stages = [q.Stage('A', 10, '秒'), q.Stage('B', 20, '秒')]
        self.assertTrue(config.save())
        with patch.object(q, 'HAS_KEYBOARD', False):
            instance = q.App()
        instance._ppt_monitor_timer.stop()
        self.instances.append(instance)
        return instance

    def test_clock_changes_do_not_affect_timer_and_pause_uses_exact_time(self):
        config = q.Config()
        config.global_sound = False
        config.stages = [q.Stage('', 10, '秒')]
        controller = q.TimerController(config)
        controller.stop()
        try:
            with patch.object(q.time, 'time', side_effect=AssertionError('wall clock read')):
                with patch.object(q.time, 'monotonic', return_value=100):
                    controller.toggle_pause()
                controller._timer.stop()
                with patch.object(q.time, 'monotonic', return_value=102.125):
                    controller._tick()
                self.assertAlmostEqual(controller._remaining_float, 7.875)
                with patch.object(q.time, 'monotonic', return_value=102.175):
                    controller.toggle_pause()
                self.assertTrue(controller.paused)
                self.assertAlmostEqual(controller._remaining_float, 7.825)
                with patch.object(q.time, 'monotonic', return_value=1000):
                    controller.toggle_pause()
                with patch.object(q.time, 'monotonic', return_value=1001):
                    controller._tick()
                self.assertAlmostEqual(controller._remaining_float, 6.825)
        finally:
            controller._timer.stop()

    def test_appearance_save_preserves_stage_running_deadline_and_alerts(self):
        instance = self.instance()
        ctrl = instance.controller
        ctrl.next_stage()
        with patch.object(q.time, 'monotonic', return_value=100):
            ctrl.toggle_pause()
            ctrl._remaining_float = 7.3
            ctrl._target_time = 107.3
            ctrl._triggered = {10}
            instance.float_bar.apply_alert('#ff0000', True)
            instance._open_settings()
            dialog = instance._settings_window
            dialog._font_trans_slider.setValue(20)
            dialog._accept_validated()
        self.assertEqual(ctrl._stage_idx, 1)
        self.assertFalse(ctrl.paused)
        self.assertTrue(ctrl._timer.isActive())
        self.assertEqual(ctrl._target_time, 107.3)
        self.assertEqual(ctrl._remaining_float, 7.3)
        self.assertEqual(ctrl._triggered, {10})
        self.assertTrue(instance.float_bar._flash_timer.isActive())
        self.assertIsNone(instance._settings_window)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['font_transparency'], 20)

    def test_changing_flow_resets_timer_only_after_successful_save(self):
        instance = self.instance()
        instance.controller.next_stage()
        instance.controller.toggle_pause()
        instance._open_settings()
        dialog = instance._settings_window
        dialog._stage_rows[0]['spin'].setValue(15)
        dialog._accept_validated()
        self.assertEqual(instance.controller._stage_idx, 0)
        self.assertTrue(instance.controller.paused)
        self.assertEqual(instance.controller._remaining_float, 15)

    def test_failed_save_keeps_editor_open_and_flow_unchanged(self):
        instance = self.instance()
        instance.controller.next_stage()
        instance.controller.toggle_pause()
        before = self.path.read_bytes()
        instance._open_settings()
        dialog = instance._settings_window
        dialog._stage_rows[0]['name'].setText('unsaved')
        with patch.object(q, 'write_config', return_value=(False, 'disk full')), patch.object(q.QMessageBox, 'warning') as warning:
            dialog._accept_validated()
        warning.assert_called_once()
        self.assertTrue(dialog.isVisible())
        self.assertIs(instance._settings_window, dialog)
        self.assertEqual(instance.config.stages[0].label, 'A')
        self.assertEqual(instance.controller._stage_idx, 1)
        self.assertFalse(instance.controller.paused)
        self.assertEqual(self.path.read_bytes(), before)

    def test_empty_flow_cannot_be_committed(self):
        instance = self.instance()
        instance._open_settings()
        dialog = instance._settings_window
        dialog._clear_form_rows(dialog._stage_vlay, dialog._stage_rows)
        with patch.object(q.QMessageBox, 'warning') as warning:
            dialog._accept_validated()
        warning.assert_called_once()
        self.assertIs(instance._settings_window, dialog)
        self.assertEqual(len(instance.config.stages), 2)

    def test_malformed_fields_do_not_poison_config_or_widgets(self):
        payload = {'stages': None, 'alerts': None, 'font_size': -5, 'stage_font_size': 999999,
                   'ui_scale': None, 'bg_transparency': 'oops', 'font_transparency': 900,
                   'color': [], 'bg_color': 'not-a-color', 'ppt_auto_start': 'false', 'pos_x': '-1200'}
        self.path.write_text(json.dumps(payload), encoding='utf-8')
        config = q.Config()
        config.load()
        self.assertEqual(config.font_size, 20)
        self.assertEqual(config.stage_font_size, 36)
        self.assertEqual(config.ui_scale, 100)
        self.assertFalse(config.ppt_auto_start)
        self.assertFalse(config.ppt_auto_pause)
        self.assertEqual(config.pos_x, -1200)
        self.assertTrue(config.bg_qcolor().isValid())
        self.assertGreater(len(config.stages), 0)
        dialog = q.SettingsWindow(config)
        self.widgets.append(dialog)
        payload['stages'] = [None, 1, {'duration': 0, 'unit': [], 'timeout_image': {}, 'count_up': 'false'}]
        self.path.write_text(json.dumps(payload), encoding='utf-8')
        config.load()
        self.assertEqual(config.stages[0].duration, 1)
        self.assertEqual(config.stages[0].unit, '分')
        self.assertEqual(config.stages[0].timeout_image, '')
        self.assertFalse(config.stages[0].count_up)

    def test_corrupt_primary_recovers_backup_and_preserves_original_bytes(self):
        config = q.Config()
        config.font_size = 32
        self.assertTrue(config.save())
        config.font_size = 40
        self.assertTrue(config.save())
        corrupt = b'{unreadable config'
        self.path.write_bytes(corrupt)
        restored = q.Config()
        restored.load()
        self.assertEqual(restored.font_size, 32)
        self.assertIn('备份', restored._load_notice)
        self.assertEqual(self.path.read_bytes(), corrupt)
        preserved = list(self.path.parent.glob('config-corrupt-*.json'))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), corrupt)
        self.assertTrue(restored.save())
        self.assertEqual(json.loads(Path(str(self.path)+'.bak').read_text(encoding='utf-8'))['font_size'], 32)

    def test_failed_atomic_replace_retains_last_saved_config(self):
        config = q.Config()
        self.assertTrue(config.save())
        before = self.path.read_bytes()
        original_replace = config_storage.os.replace
        def fail_primary(source, destination):
            if str(destination) == str(self.path):
                raise PermissionError('primary blocked')
            return original_replace(source, destination)
        config.font_size = 50
        with patch.object(config_storage.os, 'replace', side_effect=fail_primary):
            self.assertFalse(config.save())
        self.assertIn('primary blocked', config._last_error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('.qtimer-*.tmp')), [])

    def test_nonfinite_legacy_numbers_are_repaired_and_get_a_valid_backup(self):
        self.path.write_text('{"font_size": Infinity, "ui_scale": NaN}', encoding='utf-8')
        config = q.Config()
        config.load()
        self.assertEqual(config.font_size, 32)
        self.assertEqual(config.ui_scale, 100)
        self.assertTrue(config.save())
        backup = json.loads(Path(str(self.path)+'.bak').read_text(encoding='utf-8'))
        self.assertEqual(backup['font_size'], 32)

    def test_unreadable_config_is_not_overwritten(self):
        config = q.Config()
        self.assertTrue(config.save())
        original = self.path.read_bytes()
        with patch.object(config_storage, '_read_object', side_effect=PermissionError('cannot read')), patch.object(config_storage, '_atomic_write') as write:
            self.assertFalse(config.save())
        write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_single_ppt_dropout_does_not_pause_or_clear_manual_pause(self):
        instance = self.instance()
        instance.config.ppt_auto_start = True
        instance.config.ppt_auto_pause = True
        def detect(active, count=1):
            with patch.object(q, 'is_ppt_slideshow_active', return_value=active):
                for _ in range(count):
                    instance._check_ppt_status()
        detect(True, 2)
        detect(False)
        self.assertFalse(instance.controller.paused)
        detect(True, 2)
        instance.controller.toggle_pause()
        instance._current_ppt_session_paused_by_user = True
        detect(False)
        detect(True, 2)
        self.assertTrue(instance.controller.paused)
        detect(False, 2)
        detect(True, 2)
        self.assertFalse(instance.controller.paused)
        detect(False, 2)
        self.assertTrue(instance.controller.paused)

    def test_negative_monitor_coordinates_are_restored_and_visible_positions_retained(self):
        instance = self.instance()
        instance.config.pos_x, instance.config.pos_y = -1200, 100
        with patch.object(instance.float_bar, 'move_content') as move:
            instance._restore_position()
        move.assert_called_once_with(q.QPoint(-1200, 100))
        instance.float_bar.move_content(q.QPoint(-1200, 100))
        left_monitor = SimpleNamespace(availableGeometry=lambda: q.QRect(-1920, 0, 1920, 1080))
        with patch.object(q.QApplication, 'screens', return_value=[left_monitor]), patch.object(instance.float_bar, 'move_content') as move:
            instance._recover_window_position()
        move.assert_not_called()

    def test_offscreen_timer_is_recovered_even_when_boundary_limit_disabled(self):
        instance = self.instance()
        instance.config.prevent_offscreen = False
        instance.float_bar._prevent_offscreen = False
        instance.float_bar.move_content(q.QPoint(100000, 100000))
        instance._recover_window_position()
        expected = APP.primaryScreen().availableGeometry().topLeft() + q.QPoint(40, 40)
        self.assertEqual(instance.float_bar.content_position(), expected)
        self.assertEqual((instance.config.pos_x, instance.config.pos_y), (expected.x(), expected.y()))

    def test_exit_flushes_position_before_delayed_save_fires(self):
        instance = self.instance()
        instance.float_bar.move_content(q.QPoint(300, 200))
        instance._on_position_changed(300, 200)
        self.assertTrue(instance._save_timer.isActive())
        with patch.object(q, 'HAS_KEYBOARD', False), patch.object(q.QApplication, 'quit'):
            instance._exit()
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual((saved['pos_x'], saved['pos_y']), (300, 200))
        self.assertFalse(instance._save_timer.isActive())


if __name__ == '__main__':
    unittest.main(verbosity=2)
