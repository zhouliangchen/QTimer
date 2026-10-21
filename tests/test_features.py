import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as q
from PySide6.QtTest import QTest

APP = q.QApplication.instance() or q.QApplication([])
APP.setQuitOnLastWindowClosed(False)

class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_patch = patch.object(q, 'CONFIG_PATH', str(Path(self.temp.name) / 'config.json'))
        self.config_patch.start()
        self.config = q.Config()
        self.config.global_sound = False
        self.config.ppt_auto_start = False
        self.widgets = []
        self.instances = []

    def tearDown(self):
        for instance in self.instances:
            if instance._settings_window:
                instance._settings_window.reject()
            instance.controller._timer.stop()
            instance._ppt_monitor_timer.stop()
            instance._save_timer.stop()
            for shortcut in instance._shortcuts:
                shortcut.setEnabled(False)
            instance.float_bar.close()
        for widget in self.widgets:
            widget.close()
        APP.processEvents()
        self.config_patch.stop()
        self.temp.cleanup()

    def controller(self, stage=None):
        self.config.stages = [stage or q.Stage('', 10, '秒'), q.Stage('next', 5, '秒')]
        ctrl = q.TimerController(self.config)
        ctrl.stop()
        ctrl.toggle_pause()
        ctrl._timer.stop()
        return ctrl

    def bar(self):
        bar = q.FloatBar()
        self.widgets.append(bar)
        return bar

    def instance(self):
        self.config.save()
        with patch.object(q, 'HAS_KEYBOARD', False), patch.object(q, 'is_ppt_slideshow_active', return_value=False):
            instance = q.App()
        self.instances.append(instance)
        return instance

    def test_old_and_new_configuration_roundtrip(self):
        self.assertEqual(q.Stage.from_dict({'minutes': 2}).seconds, 120)
        self.assertEqual(q.Stage.from_dict({}).timeout_display, 'zero')
        self.assertFalse(q.Alert.from_dict({}).keep_flashing)
        self.config.stages = [q.Stage('', 2, '秒', False, 'text', '已超时', '', True)]
        self.config.alerts = [q.Alert(1, '#ff0000', False, True)]
        self.config.save()
        restored = q.Config()
        restored.load()
        self.assertEqual(restored.stages, self.config.stages)
        self.assertEqual(restored.alerts, self.config.alerts)

    def test_timeout_does_not_advance_and_fires_once(self):
        ctrl = self.controller(q.Stage('', 1, '秒', False, 'text', '已超时', '', True))
        expired = []
        ctrl.stage_expired.connect(expired.append)
        ctrl._target_time = 99
        with patch.object(q.time, 'time', return_value=100):
            ctrl._tick()
            ctrl._tick()
        self.assertTrue(ctrl.paused)
        self.assertEqual(ctrl._stage_idx, 0)
        self.assertEqual(len(expired), 1)
        ctrl.next_stage()
        self.assertEqual(ctrl._stage_idx, 1)

    def test_auto_advance_retains_original_flow(self):
        self.config.auto_advance = True
        ctrl = self.controller(q.Stage('', 1, '秒'))
        ctrl._target_time = 99
        with patch.object(q.time, 'time', return_value=100):
            ctrl._tick()
        self.assertEqual(ctrl._stage_idx, 1)
        self.assertFalse(ctrl.paused)

    def test_countup_never_runs_timeout_actions(self):
        ctrl = self.controller(q.Stage('', 1, '秒', True, 'text', 'wrong', '', True))
        expired = []
        ctrl.stage_expired.connect(expired.append)
        ctrl._target_time = 99
        with patch.object(q.time, 'time', return_value=100):
            ctrl._tick()
        self.assertEqual(expired, [])

    def test_muted_alerts_cross_thresholds_in_order(self):
        self.config.alerts = [q.Alert(3, '#ff0000', True, False), q.Alert(7, '#ffff00', True, True)]
        ctrl = self.controller()
        alerts = []
        ctrl.alert_triggered.connect(lambda color, keep: alerts.append((color, keep)))
        ctrl._target_time = 102
        with patch.object(q.time, 'time', return_value=100), patch.object(q, 'play_alert_sound') as sound:
            ctrl._tick()
        self.assertEqual(alerts, [('#ffff00', True), ('#ff0000', False)])
        sound.assert_not_called()

    def test_persistent_flash_survives_previous_stop_timer(self):
        bar = self.bar()
        bar.start_flash('#ffff00', 20)
        bar.start_flash('#ff0000', 0)
        QTest.qWait(50)
        self.assertTrue(bar._flash_timer.isActive())
        bar.start_flash('#ffffff', 20)
        QTest.qWait(50)
        self.assertFalse(bar._flash_timer.isActive())
        bar.start_flash('#ff0000', 0)
        bar.reset_alert()
        self.assertFalse(bar._flash_timer.isActive())
        self.assertEqual(bar._flash_color, bar._text_color)

    def test_next_nonpersistent_node_stops_persistent_flash_immediately(self):
        bar = self.bar()
        bar.apply_alert('#ffff00', True)
        self.assertTrue(bar._flash_timer.isActive())
        bar.apply_alert('#ff0000', False)
        self.assertFalse(bar._flash_timer.isActive())
        self.assertEqual(bar._flash_color, '#ff0000')

    def test_empty_stage_name_and_overtime_text_image_reset(self):
        bar = self.bar()
        bar.update_display('   ', 1)
        self.assertTrue(bar.lbl_stage.isHidden())
        self.assertEqual(bar._spacing_item.sizeHint().width(), 0)
        bar.show_overtime(q.Stage('', timeout_display='text', timeout_text='已超时'))
        self.assertEqual(bar.lbl_time.text(), '已超时')
        image = Path(self.temp.name) / 'timeout.png'
        pixmap = q.QPixmap(160, 40)
        pixmap.fill(q.QColor('red'))
        pixmap.save(str(image))
        bar.show_overtime(q.Stage('', timeout_display='image', timeout_image=str(image)))
        self.assertFalse(bar.lbl_time.pixmap().isNull())
        bar.update_display('next', 120)
        self.assertEqual(bar.lbl_time.text(), '02:00')
        self.assertFalse(bar.lbl_stage.isHidden())

    def test_right_drawer_keeps_right_edge_and_text_order(self):
        bar = self.bar()
        bar.update_display('阶段', 300)
        screen = bar.screen().availableGeometry()
        bar.move_content(q.QPoint(screen.right() - bar._text_width + 1, screen.top() + 30))
        bar._update_drawer_direction()
        self.assertTrue(bar._drawer_left)
        anchor = bar.content_position()
        native_geometry = bar.geometry()
        bar.bar_width = bar._full_width
        bar._text_layout.activate()
        self.assertEqual(bar.content_position(), anchor)
        self.assertEqual(bar.geometry(), native_geometry)
        self.assertEqual(bar.visible_geometry().right(), screen.right())
        self.assertLess(bar._btn_container.x(), bar._text_container.x())
        self.assertLess(bar.lbl_stage.x(), bar.lbl_time.x())
        bar.bar_width = bar._text_width
        self.assertEqual(bar.content_position(), anchor)
        self.assertEqual(bar.geometry(), native_geometry)
        self.assertEqual(bar.visible_geometry().width(), bar._text_width)
        bar.move_content(q.QPoint(screen.left() + 10, screen.top() + 30))
        bar._update_drawer_direction()
        self.assertFalse(bar._drawer_left)
        left = bar.content_position()
        bar.bar_width = bar._full_width
        self.assertEqual(bar.content_position(), left)
        self.assertEqual(bar.visible_geometry().left(), left.x())

    def test_settings_are_nonmodal_and_preserve_drag_on_cancel(self):
        instance = self.instance()
        instance._open_settings()
        dialog = instance._settings_window
        self.assertFalse(dialog.isModal())
        self.assertEqual(dialog.windowModality(), q.Qt.WindowModality.NonModal)
        dialog.switch_show_label.setChecked(False)
        instance.float_bar.move_content(q.QPoint(100, 100))
        # Deliver Qt mouse events while the nonmodal settings window is open.
        bar = instance.float_bar
        start = q.QPointF(bar._drawer_space + 10, 10)
        global_start = q.QPointF(bar.mapToGlobal(start.toPoint()))
        APP.sendEvent(bar, q.QMouseEvent(q.QEvent.Type.MouseButtonPress, start, global_start,
                      q.Qt.MouseButton.LeftButton, q.Qt.MouseButton.LeftButton, q.Qt.KeyboardModifier.NoModifier))
        APP.sendEvent(bar, q.QMouseEvent(q.QEvent.Type.MouseMove, start + q.QPointF(30, 20),
                      global_start + q.QPointF(30, 20), q.Qt.MouseButton.NoButton,
                      q.Qt.MouseButton.LeftButton, q.Qt.KeyboardModifier.NoModifier))
        self.assertEqual(bar.content_position(), q.QPoint(130, 120))
        APP.sendEvent(bar, q.QMouseEvent(q.QEvent.Type.MouseButtonRelease, start + q.QPointF(30, 20),
                      global_start + q.QPointF(30, 20), q.Qt.MouseButton.LeftButton,
                      q.Qt.MouseButton.NoButton, q.Qt.KeyboardModifier.NoModifier))
        instance._on_position_changed(100, 100)
        instance._save_config()
        # Pending preview is not written by dragging.
        saved = json.loads(Path(q.CONFIG_PATH).read_text(encoding='utf-8'))
        self.assertTrue(saved['show_stage_label'])
        dialog.reject()
        self.assertTrue(instance.config.show_stage_label)
        self.assertEqual((instance.config.pos_x, instance.config.pos_y), (100, 100))
        self.assertIsNone(instance._settings_window)

    def test_stage_and_alert_edit_reorder_preserves_new_fields(self):
        self.config.stages = [q.Stage('', 2, '秒', False, 'text', '结束', '', True)]
        self.config.alerts = [q.Alert(1, '#ff0000', False, True)]
        dialog = q.SettingsWindow(self.config)
        self.widgets.append(dialog)
        dialog._rebuild_stage_rows()
        dialog._rebuild_alert_rows()
        updated = dialog.get_config()
        self.assertTrue(updated.stages[0].timeout_close_show)
        self.assertEqual(updated.stages[0].timeout_text, '结束')
        self.assertEqual(updated.stages[0].label, '')
        self.assertTrue(updated.alerts[0].keep_flashing)

    def test_timeout_dispatch_closes_only_on_configured_stage(self):
        instance = self.instance()
        with patch.object(q, 'close_ppt_slideshows') as close_show:
            instance._on_stage_expired(q.Stage('', timeout_display='text', timeout_text='结束', timeout_close_show=True))
            self.assertEqual(instance.float_bar.lbl_time.text(), '结束')
            close_show.assert_called_once()

    def test_restore_defaults_does_not_reapply_stale_preview(self):
        self.config.bg_transparency = 77
        self.config.font_transparency = 42
        dialog = q.SettingsWindow(self.config)
        self.widgets.append(dialog)
        with patch.object(q.QMessageBox, 'question', return_value=q.QMessageBox.StandardButton.Yes), \
             patch.object(q.QMessageBox, 'information'):
            dialog._restore_defaults()
        self.assertEqual(dialog.config.bg_transparency, q.Config().bg_transparency)
        self.assertEqual(dialog.config.font_transparency, q.Config().font_transparency)

    def test_transparency_reset_updates_live_bar_save_and_reopen(self):
        instance = self.instance()
        instance._open_settings()
        dialog = instance._settings_window
        dialog._bg_op_slider.setValue(100)
        dialog._font_trans_slider.setValue(100)
        self.assertEqual(instance.float_bar._bg_color.alpha(), 1)
        self.assertEqual(instance.float_bar._font_transparency, 100)
        dialog.btn_restore.click()
        defaults = q.Config()
        self.assertEqual(dialog._bg_op_slider.value(), defaults.bg_transparency)
        self.assertEqual(dialog._font_trans_slider.value(), defaults.font_transparency)
        self.assertEqual(dialog._bg_op_slider._value_editor.value(), defaults.bg_transparency)
        self.assertEqual(dialog._font_trans_slider._value_editor.value(), defaults.font_transparency)
        self.assertEqual(instance.float_bar._bg_color.alpha(), defaults.bg_qcolor().alpha())
        self.assertEqual(instance.float_bar._font_transparency, 0)
        self.assertEqual(instance.float_bar.windowOpacity(), 1.0)
        self.assertEqual(dialog._stage_vlay.count(), len(defaults.stages))
        dialog._accept_validated()
        restored = q.Config()
        restored.load()
        self.assertEqual(restored.bg_transparency, defaults.bg_transparency)
        self.assertEqual(restored.font_transparency, defaults.font_transparency)
        instance._open_settings()
        self.assertEqual(instance._settings_window._bg_op_slider.value(), defaults.bg_transparency)

    def test_reset_draft_keeps_running_sequence_and_cancel_restores_opacity(self):
        self.config.stages = [q.Stage('keep', 7, '秒')]
        self.config.bg_transparency = 41
        self.config.font_transparency = 23
        instance = self.instance()
        instance._open_settings()
        dialog = instance._settings_window
        dialog._bg_op_slider.setValue(95)
        dialog._font_trans_slider.setValue(85)
        dialog._restore_defaults()
        self.assertEqual(instance.config.stages[0].label, 'keep')
        self.assertEqual(instance.controller._remaining_float, 7)
        dialog.reject()
        self.assertEqual(instance.config.bg_transparency, 41)
        self.assertEqual(instance.config.font_transparency, 23)
        self.assertEqual(instance.float_bar._font_transparency, 23)

    def test_removed_global_opacity_is_ignored_in_old_configuration(self):
        Path(q.CONFIG_PATH).write_text(json.dumps({'global_transparency': 70, 'bg_transparency': 32}), encoding='utf-8')
        restored = q.Config()
        restored.load()
        self.assertFalse(hasattr(restored, 'global_transparency'))
        self.assertEqual(restored.bg_transparency, 32)
        restored.save()
        self.assertNotIn('global_transparency', json.loads(Path(q.CONFIG_PATH).read_text(encoding='utf-8')))

    def test_all_docked_edges_release_after_cumulative_drag_threshold(self):
        for edge in ('left', 'right', 'top', 'bottom'):
            with self.subTest(edge=edge):
                bar = self.bar()
                bar.update_display('', 300)
                screen = bar.screen().availableGeometry()
                x, y = screen.center().x(), screen.center().y()
                if edge == 'left': x = screen.left()
                if edge == 'right': x = screen.right()-bar._text_width+1
                if edge == 'top': y = screen.top()
                if edge == 'bottom': y = screen.bottom()-bar.height()+1
                bar.move_content(q.QPoint(x, y))
                bar._update_drawer_direction()
                start_pos = bar.content_position()
                local = q.QPointF(bar._drawer_space + 10, 10)
                global_start = q.QPointF(bar.mapToGlobal(local.toPoint()))
                def event(kind, delta, button, buttons):
                    APP.sendEvent(bar, q.QMouseEvent(kind, local+delta, global_start+delta,
                                  button, buttons, q.Qt.KeyboardModifier.NoModifier))
                event(q.QEvent.Type.MouseButtonPress, q.QPointF(), q.Qt.MouseButton.LeftButton, q.Qt.MouseButton.LeftButton)
                direction = q.QPointF(1 if edge=='left' else -1, 0) if edge in ('left','right') else q.QPointF(0, 1 if edge=='top' else -1)
                for distance in (5, 10, 19):
                    event(q.QEvent.Type.MouseMove, direction*distance, q.Qt.MouseButton.NoButton, q.Qt.MouseButton.LeftButton)
                    self.assertEqual(bar.content_position(), start_pos)
                event(q.QEvent.Type.MouseMove, direction*21, q.Qt.MouseButton.NoButton, q.Qt.MouseButton.LeftButton)
                self.assertEqual(bar.content_position(), start_pos+direction.toPoint())
                event(q.QEvent.Type.MouseMove, direction*40, q.Qt.MouseButton.NoButton, q.Qt.MouseButton.LeftButton)
                self.assertEqual(bar.content_position(), start_pos+(direction*20).toPoint())
                event(q.QEvent.Type.MouseMove, direction*25, q.Qt.MouseButton.NoButton, q.Qt.MouseButton.LeftButton)
                self.assertEqual(bar.content_position(), start_pos+(direction*5).toPoint())
                event(q.QEvent.Type.MouseButtonRelease, direction*25, q.Qt.MouseButton.LeftButton, q.Qt.MouseButton.NoButton)

    def test_ppt_start_and_exit_pause_are_independent(self):
        instance = self.instance()
        for start in (False, True):
            for pause in (False, True):
                with self.subTest(start=start, pause=pause):
                    instance.config.ppt_auto_start = start
                    instance.config.ppt_auto_pause = pause
                    instance.controller.stop()
                    instance._ppt_was_active = False
                    instance._ppt_stable_active_count = 0
                    with patch.object(q, 'is_ppt_slideshow_active', return_value=True):
                        instance._check_ppt_status()
                        instance._check_ppt_status()
                    self.assertEqual(instance.controller.paused, not start)
                    if instance.controller.paused:
                        instance.controller.toggle_pause()
                    with patch.object(q, 'is_ppt_slideshow_active', return_value=False):
                        instance._check_ppt_status()
                    self.assertEqual(instance.controller.paused, pause)

    def test_ppt_manual_pause_survives_detection_and_resumes_next_show(self):
        instance = self.instance()
        instance.config.ppt_auto_start = True
        with patch.object(q, 'is_ppt_slideshow_active', return_value=True):
            instance._check_ppt_status()
            instance._check_ppt_status()
            instance._handle_user_action(instance.controller.toggle_pause)
            instance._check_ppt_status()
            self.assertTrue(instance.controller.paused)
        with patch.object(q, 'is_ppt_slideshow_active', return_value=False):
            instance._check_ppt_status()
        with patch.object(q, 'is_ppt_slideshow_active', return_value=True):
            instance._check_ppt_status()
            instance._check_ppt_status()
        self.assertFalse(instance.controller.paused)

    def test_old_ppt_switch_migrates_both_actions_and_new_values_roundtrip(self):
        for enabled in (False, True):
            Path(q.CONFIG_PATH).write_text(json.dumps({'ppt_auto_start': enabled}), encoding='utf-8')
            config = q.Config()
            config.load()
            self.assertEqual(config.ppt_auto_pause, enabled)
        config.ppt_auto_start = False
        config.ppt_auto_pause = True
        config.auto_text_color = True
        config.save()
        restored = q.Config()
        restored.load()
        self.assertFalse(restored.ppt_auto_start)
        self.assertTrue(restored.ppt_auto_pause)
        self.assertTrue(restored.auto_text_color)

    def test_presets_preview_render_and_relocatable_identifiers(self):
        dialog = q.SettingsWindow(self.config)
        self.widgets.append(dialog)
        row = dialog._stage_rows[0]
        row['timeout_display'].setCurrentIndex(row['timeout_display'].findData('image'))
        bar = self.bar()
        for key in q.OVERTIME_PRESETS:
            value = 'preset:' + key
            row['timeout_preset'].setCurrentIndex(row['timeout_preset'].findData(value))
            self.assertFalse(row['timeout_image_preview'].pixmap().isNull())
            stage = dialog._stage_from_row(row)
            self.assertEqual(stage.timeout_image, value)
            self.config.stages = [stage]
            self.config.save()
            restored = q.Config()
            restored.load()
            self.assertEqual(restored.stages[0].timeout_image, value)
            bar.show_overtime(restored.stages[0])
            self.assertFalse(bar.lbl_time.pixmap().isNull())
            with patch.object(q.sys, '_MEIPASS', self.temp.name, create=True):
                self.assertTrue(q.timeout_image_path(value).startswith(self.temp.name))

    def test_auto_colors_composite_underlay_and_preserve_alerts(self):
        bar = self.bar()
        bar.update_display('stage', 60)
        bar.show()
        with patch.object(q, 'set_capture_excluded', return_value=True), patch.object(q, 'sample_underlay', return_value=[q.QColor('white'), q.QColor('black')]):
            bar.apply_style('#123456', 'Arial', 32, 'Arial', 18, 1,
                            q.QColor(0, 0, 0, 128), True, True, True, 0, 100, True)
            bar._update_auto_colors()
            self.assertEqual(bar._auto_colors, ['#808080', '#ffffff'])
            bar._update_auto_colors()
            self.assertEqual(bar._auto_colors, ['#808080', '#ffffff'])
            bar.start_flash('#ff0000', 0)
            bar._do_flash()
            bar._update_auto_colors()
            self.assertIn('255,0,0', bar.lbl_time.styleSheet().replace(' ', ''))
            bar.reset_alert()
            self.assertIn('255,255,255', bar.lbl_time.styleSheet().replace(' ', ''))
        previous = list(bar._auto_colors)
        with patch.object(q, 'sample_underlay', return_value=None):
            bar._update_auto_colors()
        self.assertEqual(bar._auto_colors, previous)

    def test_auto_color_preview_cancel_and_defaults(self):
        instance = self.instance()
        instance._open_settings()
        dialog = instance._settings_window
        dialog.switch_auto_text_color.setChecked(True)
        self.assertTrue(instance.config.auto_text_color)
        self.assertFalse(dialog._color_preview.isEnabled())
        dialog.reject()
        self.assertFalse(instance.config.auto_text_color)
        instance._open_settings()
        dialog = instance._settings_window
        dialog.switch_auto_text_color.setChecked(True)
        dialog._restore_defaults()
        self.assertFalse(dialog.switch_auto_text_color.isChecked())
        self.assertTrue(dialog._color_preview.isEnabled())
        dialog.switch_ppt_auto_start.setChecked(False)
        dialog.switch_ppt_auto_pause.setChecked(True)
        saved = dialog.get_config()
        self.assertFalse(saved.ppt_auto_start)
        self.assertTrue(saved.ppt_auto_pause)

    def test_drawer_animation_keeps_text_and_native_frame_stationary(self):
        bar = self.bar()
        bar.update_display('阶段', 300)
        screen = bar.screen().availableGeometry()
        bar.move_content(q.QPoint(screen.right() - bar._text_width + 1, screen.top() + 30))
        bar._update_drawer_direction()
        bar._text_layout.activate()
        anchor = bar.content_position()
        frame = bar.geometry()
        label_positions = [label.mapToGlobal(q.QPoint()) for label in (bar.lbl_stage, bar.lbl_time)]
        for progress in list(range(21)) + list(reversed(range(21))):
            bar.drawer_reveal = progress / 20
            self.assertEqual(bar.geometry(), frame)
            self.assertEqual(bar.content_position(), anchor)
            self.assertEqual([label.mapToGlobal(q.QPoint()) for label in (bar.lbl_stage, bar.lbl_time)], label_positions)
            self.assertEqual(bar.mask().boundingRect(), bar._visible_rect())
            if bar._btn_container.isVisibleTo(bar):
                clip = bar._btn_container.mask().translated(bar._btn_container.pos())
                self.assertFalse(clip.intersects(bar._text_container.geometry()))

    def test_drawer_side_switch_collapses_then_reopens_and_can_reverse(self):
        bar = self.bar()
        bar.update_display('', 60)
        screen = bar.screen().availableGeometry()
        left = q.QPoint(screen.left() + 20, screen.top() + 60)
        right = q.QPoint(screen.right() - bar._text_width + 1, screen.top() + 60)
        bar.move_content(left)
        bar.show()
        bar._set_drawer_open(True)
        QTest.qWait(260)
        self.assertAlmostEqual(bar.drawer_reveal, 1.0)
        bar.move_content(right)
        bar._update_drawer_direction()
        self.assertTrue(bar._side_switching)
        self.assertFalse(bar._drawer_left)
        anchor = bar.content_position()
        frame = bar.geometry()
        QTest.qWait(60)
        self.assertGreater(bar.drawer_reveal, 0)
        self.assertLess(bar.drawer_reveal, 1)
        QTest.qWait(320)
        self.assertTrue(bar._drawer_left)
        self.assertAlmostEqual(bar.drawer_reveal, 1.0)
        self.assertEqual(bar.content_position(), anchor)
        self.assertEqual(bar.geometry(), frame)
        bar.move_content(left)
        bar._update_drawer_direction()
        QTest.qWait(40)
        bar.move_content(right)
        bar._update_drawer_direction()
        QTest.qWait(260)
        self.assertFalse(bar._side_switching)
        self.assertTrue(bar._drawer_left)
        self.assertAlmostEqual(bar.drawer_reveal, 1.0)

    def test_dragging_expanded_controls_across_boundary_keeps_timer_on_pointer(self):
        bar = self.bar()
        bar.update_display('', 60)
        screen = bar.screen().availableGeometry()
        origin = q.QPoint(screen.left()+60, screen.top()+80)
        bar.move_content(origin)
        bar.show()
        bar._set_drawer_open(True)
        QTest.qWait(260)
        local = q.QPointF(bar._drawer_space + 20, 20)
        start = q.QPointF(bar.mapToGlobal(local.toPoint()))
        APP.sendEvent(bar, q.QMouseEvent(q.QEvent.Type.MouseButtonPress, local, start,
                      q.Qt.MouseButton.LeftButton, q.Qt.MouseButton.LeftButton, q.Qt.KeyboardModifier.NoModifier))
        target = q.QPoint(int(screen.left()+screen.width()*0.78), origin.y())
        delta = q.QPointF(target-origin)
        APP.sendEvent(bar, q.QMouseEvent(q.QEvent.Type.MouseMove, local+delta, start+delta,
                      q.Qt.MouseButton.NoButton, q.Qt.MouseButton.LeftButton, q.Qt.KeyboardModifier.NoModifier))
        self.assertEqual(bar.content_position(), target)
        self.assertTrue(bar._drawer_target_left)
        QTest.qWait(380)
        self.assertTrue(bar._drawer_left)
        self.assertEqual(bar.content_position(), target)
        APP.sendEvent(bar, q.QMouseEvent(q.QEvent.Type.MouseButtonRelease, local+delta, start+delta,
                      q.Qt.MouseButton.LeftButton, q.Qt.MouseButton.NoButton, q.Qt.KeyboardModifier.NoModifier))

    def test_settings_header_moves_resizes_and_respects_minimum_size(self):
        window = q.QWidget()
        window.setWindowFlags(q.Qt.WindowType.FramelessWindowHint)
        self.widgets.append(window)
        window.setMinimumSize(200, 200)
        window.setGeometry(100, 100, 700, 400)
        header = q.SettingsDragHandle(window)
        header.resize(700, 40)
        window.show()
        APP.processEvents()
        with patch.object(header, 'grabMouse'):
            for point, delta, expected in (
                (q.QPoint(100, 20), q.QPoint(40, 30), q.QRect(140, 130, 700, 400)),
                (q.QPoint(100, 1), q.QPoint(0, 20), q.QRect(140, 150, 700, 380)),
                (q.QPoint(699, 20), q.QPoint(35, 0), q.QRect(140, 150, 735, 380)),
                (q.QPoint(699, 20), q.QPoint(-1000, 0), q.QRect(140, 150, 200, 380)),
            ):
                pointer = q.QPointF(header.mapToGlobal(point))
                header.mousePressEvent(q.QMouseEvent(q.QEvent.Type.MouseButtonPress,
                    q.QPointF(point), pointer, q.Qt.MouseButton.LeftButton,
                    q.Qt.MouseButton.LeftButton, q.Qt.KeyboardModifier.NoModifier))
                header.mouseMoveEvent(q.QMouseEvent(q.QEvent.Type.MouseMove,
                    q.QPointF(point+delta), pointer+q.QPointF(delta), q.Qt.MouseButton.NoButton,
                    q.Qt.MouseButton.LeftButton, q.Qt.KeyboardModifier.NoModifier))
                self.assertEqual(window.geometry(), expected)
                header.mouseReleaseEvent(q.QMouseEvent(q.QEvent.Type.MouseButtonRelease,
                    q.QPointF(point+delta), pointer+q.QPointF(delta), q.Qt.MouseButton.LeftButton,
                    q.Qt.MouseButton.NoButton, q.Qt.KeyboardModifier.NoModifier))
                self.assertIsNone(header._interaction)

    def test_top_padding_and_sidebar_blank_space_are_draggable(self):
        dialog = q.SettingsWindow(self.config)
        self.widgets.append(dialog)
        dialog.show()
        APP.processEvents()
        header = dialog._title_handle
        self.assertEqual(header.mapTo(dialog, q.QPoint()).y(), 0)
        with patch.object(header, 'grabMouse'):
            for point in (q.QPoint(20, 7), q.QPoint(100, 15), q.QPoint(220, 12),
                          q.QPoint(400, 7), q.QPoint(998, 7)):
                target = dialog.childAt(point) or dialog
                before = dialog.pos()
                pointer = q.QPointF(dialog.mapToGlobal(point))
                QTest.mousePress(target, q.Qt.MouseButton.LeftButton, pos=target.mapFrom(dialog, point))
                self.assertIsNotNone(header._interaction, str(point))
                delta = q.QPointF(30, 20)
                header.mouseMoveEvent(q.QMouseEvent(q.QEvent.Type.MouseMove,
                    q.QPointF(point)+delta, pointer+delta, q.Qt.MouseButton.NoButton,
                    q.Qt.MouseButton.LeftButton, q.Qt.KeyboardModifier.NoModifier))
                self.assertEqual(dialog.pos(), before+delta.toPoint(), str(point))
                header.mouseReleaseEvent(q.QMouseEvent(q.QEvent.Type.MouseButtonRelease,
                    q.QPointF(point)+delta, pointer+delta, q.Qt.MouseButton.LeftButton,
                    q.Qt.MouseButton.NoButton, q.Qt.KeyboardModifier.NoModifier))
            QTest.mouseClick(dialog.btn_close, q.Qt.MouseButton.LeftButton)
            self.assertFalse(dialog.isVisible())

    def test_settings_are_frameless_and_requested_notes_are_removed(self):
        dialog = q.SettingsWindow(self.config)
        self.widgets.append(dialog)
        self.assertTrue(dialog.windowFlags() & q.Qt.WindowType.FramelessWindowHint)
        self.assertIsNotNone(dialog.findChild(q.SettingsDragHandle))
        self.assertIsNotNone(dialog.findChild(q.QSizeGrip))
        for index in (1, 2, 3):
            dialog._change_page(index)
            self.assertTrue(dialog.page_description.isHidden())
        texts = ' '.join(label.text() for label in dialog.findChildren(q.QLabel))
        for note in ('基于 QTimer', '在关键时间节点提醒', '外观更改即时预览', '调整色彩、通透度与字体', '通过键盘控制计时'):
            self.assertNotIn(note, texts)

if __name__ == '__main__':
    unittest.main(verbosity=2)
