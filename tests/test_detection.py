"""Regression coverage for real PowerPoint/WPS class/title observations."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import _matches_ppt_slideshow_window as matches

class DetectionTests(unittest.TestCase):
    def test_office_show(self):
        self.assertTrue(matches('screenClass', 'PowerPoint 幻灯片放映 - [test.pptx]', 'POWERPNT.EXE'))

    def test_wps_current_show(self):
        self.assertTrue(matches('Qt5QWindowIcon', 'WPS Presentation Slide Show - [wps-test.pptx]', 'wpp.exe'))

    def test_wps_chinese_title(self):
        self.assertTrue(matches('Qt5QWindowIcon', 'WPS 演示 幻灯片放映 - [test.pptx]', 'wps.exe'))

    def test_wps_legacy_classes(self):
        for cls in ('kslideshow', 'wps_slideshow', 'wpp_slideshow_class'):
            with self.subTest(cls=cls):
                self.assertTrue(matches(cls, '', 'wpp.exe'))

    def test_editor_windows_do_not_start_timer(self):
        for cls, title, process in (
            ('PPTFrameClass', 'test.pptx - PowerPoint', 'powerpnt.exe'),
            ('PP12FrameClass', 'test.pptx - WPS Office', 'wps.exe'),
            ('Qt5QWindowIcon', 'WPS 演示', 'wpp.exe'),
            ('paneClassDC', 'PowerPoint', 'powerpnt.exe'),
            ('mso_subwindow_class', 'test.pptx', 'powerpnt.exe'),
        ):
            with self.subTest(cls=cls):
                self.assertFalse(matches(cls, title, process))

    def test_unrelated_qt_application_does_not_start_timer(self):
        self.assertFalse(matches('Qt5QWindowIcon', 'Slide Show', 'unrelated.exe'))

if __name__ == '__main__':
    unittest.main(verbosity=2)

