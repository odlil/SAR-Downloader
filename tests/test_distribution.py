from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_runtime import StateLock, default_state_dir, migrate_legacy_state, picker_command
from scripts.build_release import source_files


class DistributionTests(unittest.TestCase):
    def test_per_user_state_is_independent_of_source_or_exe_directory(self):
        with patch.dict(os.environ, {'SAR_STATE_DIR': str(ROOT / 'tests' / 'profile')}):
            self.assertEqual(default_state_dir(), (ROOT / 'tests' / 'profile').resolve())
        if sys.platform == 'win32':
            with patch.dict(os.environ, {'LOCALAPPDATA': str(ROOT / 'tests' / 'user-data')}, clear=True):
                self.assertEqual(default_state_dir(), ROOT / 'tests' / 'user-data' / 'SARDownloader')

    def test_legacy_migration_preserves_source_and_never_replaces_existing_queue(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            root = Path(folder)
            legacy = root / 'old.json'
            original = json.dumps({'version': 1, 'settings': {'output': 'original-data'}, 'jobs': [{'completed': 123}]}).encode()
            legacy.write_bytes(original)
            destination = root / 'user'
            with StateLock(destination):
                self.assertTrue(migrate_legacy_state(destination, legacy))
                self.assertEqual(legacy.read_bytes(), original)
                self.assertEqual((destination / 'downloads.json').read_bytes(), original)
                (destination / 'downloads.json').write_text('{"newQueue":true}')
                self.assertFalse(migrate_legacy_state(destination, legacy))
                self.assertEqual((destination / 'downloads.json').read_text(), '{"newQueue":true}')

    def test_same_state_directory_cannot_have_two_writers_and_unlocks_on_exit(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            with StateLock(folder):
                with self.assertRaises(RuntimeError):
                    with StateLock(folder):pass
            with StateLock(folder):pass

    def test_frozen_picker_reenters_the_executable_with_an_explicit_child_mode(self):
        with patch('app_runtime.sys.frozen', True, create=True), patch('app_runtime.sys.executable', 'SAR.exe'):
            self.assertEqual(picker_command(True), ['SAR.exe', '--native-dialog', '--folder'])
            self.assertEqual(picker_command(check=True), ['SAR.exe', '--native-dialog', '--check'])
        self.assertIn('sar_app.py', picker_command()[2])

    def test_source_archive_inputs_exclude_machine_data_and_include_distribution_entrypoints(self):
        names = [path.relative_to(ROOT).as_posix() for path in source_files()]
        for expected in ('首次配置.cmd', '启动界面.cmd', 'sar_app.py', 'LICENSE', 'requirements-lock.txt', 'scripts/smoke_release.py',
                         'local_secrets.py', 'storage.py', 'task_scheduler.py', 'web/api.js',
                         'web/preferences.js', 'web/search-controller.js', 'web/auxiliary-controller.js',
                         'preflight.py', 'web/preflight-ui.js', 'web/help/manual.pdf', 'dem_sources.py', 'web/dem-settings.js',
                         'updates.py', 'update_config.json', 'web/updates-ui.js'):
            self.assertIn(expected, names)
        self.assertFalse(any('.runtime/' in path or '.venv/' in path or path.endswith('.jpg') for path in names))


if __name__ == '__main__':unittest.main()
