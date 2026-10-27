from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_dialog import dialog_geometry, large_native_dialog


class DialogSizeTests(unittest.TestCase):
    def assert_inside(self, work, result):
        x, y, width, height = result
        self.assertGreater(width, 0); self.assertGreater(height, 0)
        self.assertGreaterEqual(x, work[0]); self.assertGreaterEqual(y, work[1])
        self.assertLessEqual(x + width, work[2]); self.assertLessEqual(y + height, work[3])

    def test_large_screen_uses_work_area_and_enlarges_both_dimensions(self):
        work = (0, 0, 1920, 1032)
        result = dialog_geometry(work, (0, 0, 950, 480))
        self.assert_inside(work, result)
        self.assertGreater(result[2], 1500); self.assertGreater(result[3], 850)
        self.assertLessEqual(abs(2 * result[0] + result[2] - 1920), 1)

    def test_small_screen_and_offscreen_old_window_stay_within_monitor(self):
        work = (0, 0, 1280, 680)
        self.assert_inside(work, dialog_geometry(work, (-500, -300, 2000, 1500)))

    def test_secondary_monitor_with_negative_coordinates(self):
        work = (-1920, -1080, 0, -48)
        result = dialog_geometry(work, (-1800, -1000, -850, -520))
        self.assert_inside(work, result)
        self.assertLess(result[0], 0); self.assertLess(result[1], 0)

    def test_already_larger_dialog_is_preserved_if_it_fits(self):
        result = dialog_geometry((0, 0, 1920, 1032), (0, 0, 1750, 950))
        self.assertEqual(result[2:], (1750, 950))

    def test_non_windows_does_not_start_a_resizing_thread(self):
        with patch('native_dialog.sys.platform', 'linux'), patch('native_dialog.threading.Thread') as thread:
            with large_native_dialog('test'):
                pass
            thread.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
