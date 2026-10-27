from pathlib import Path
from contextlib import nullcontext
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aoi_dialog import pick_and_read, pick_folder


class NativePickerTests(unittest.TestCase):
    def setUp(self):
        for target in ('aoi_dialog.enable_dpi_awareness', 'aoi_dialog.large_native_dialog'):
            patcher = patch(target, return_value=nullcontext())
            patcher.start()
            self.addCleanup(patcher.stop)

    @patch("tkinter.filedialog.askopenfilename")
    @patch("tkinter.Tk")
    def test_picker_offers_direct_formats_and_one_shp_is_sufficient(self, create_root, choose):
        choose.return_value = str(ROOT / "tests" / "fixtures" / "aoi.shp")
        result = pick_and_read()
        patterns = " ".join(pattern for _, pattern in choose.call_args.kwargs["filetypes"])
        for extension in ("*.shp", "*.kml", "*.kmz", "*.geojson"):
            self.assertIn(extension, patterns)
        self.assertNotIn("*.zip", patterns)
        self.assertEqual(result["format"], "SHP")
        create_root.return_value.destroy.assert_called_once()

    @patch("tkinter.filedialog.askdirectory")
    @patch("tkinter.Tk")
    def test_folder_picker_returns_selected_path_or_cancel(self, create_root, choose):
        for choice, expected in ((str(ROOT), {"output": str(ROOT)}), ("", {"cancelled": True})):
            choose.return_value = choice
            self.assertEqual(pick_folder(), expected)
            self.assertTrue(choose.call_args.kwargs['mustexist'])

    @patch("tkinter.filedialog.askopenfilename", return_value="")
    @patch("tkinter.Tk")
    def test_cancel_does_not_attempt_to_read_files(self, create_root, choose):
        with patch("aoi_dialog.read_aoi") as reader:
            self.assertEqual(pick_and_read(), {"cancelled": True})
            reader.assert_not_called()
        create_root.return_value.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
