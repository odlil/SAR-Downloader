"""Linux launcher, archive integrity, and encrypted settings contracts."""
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dem_jobs import engine_command
from earthdata_auth import EarthdataAuth
from local_secrets import LocalSecrets, SettingsReadError
from scripts.build_release import tar_bundle


class EnginePathTests(unittest.TestCase):
    def test_frozen_engine_name_matches_the_platform(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'dem-engine').mkdir()
            for platform, name in (('linux', 'SAR-DEM'), ('win32', 'SAR-DEM.exe')):
                engine = root/'dem-engine'/name
                engine.write_bytes(b'fixture')
                with patch('dem_jobs.sys.frozen', True, create=True), patch('dem_jobs.sys.platform', platform), patch('dem_jobs.sys.executable', str(root/'SAR')):
                    self.assertEqual(engine_command(), [str(engine)])


@unittest.skipUnless(sys.platform == 'linux', 'Linux permissions and symbolic links')
class LinuxPortableTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.store = LocalSecrets(self.root/'state')

    def tearDown(self):
        self.folder.cleanup()

    def test_saved_session_and_keys_are_encrypted_and_restore_after_restart(self):
        from test_local_secrets import RememberedSettingsTests
        auth = RememberedSettingsTests.login(self)
        self.store.set('basemap', {'provider': 'esri', 'kind': 'satellite', 'keys': {'esri': 'fixture-map-key'}})
        auth.close()
        restored = LocalSecrets(self.store.path.parent)
        self.assertTrue(EarthdataAuth(restored).status()['loggedIn'])
        self.assertEqual(restored.get('basemap')['keys']['esri'], 'fixture-map-key')
        for value in (b'fixture-token', b'fixture-password', b'fixture-map-key'):
            self.assertNotIn(value, self.store.path.read_bytes())
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.path.with_suffix('.key').stat().st_mode & 0o777, 0o600)

    def test_missing_key_never_replaces_encrypted_settings(self):
        self.store.set('basemap', {'fixture': 'keep'})
        original = self.store.path.read_bytes()
        key = self.store.path.with_suffix('.key')
        key.unlink()
        with self.assertRaises(SettingsReadError):
            self.store.set('basemap', {'fixture': 'replace'})
        self.assertFalse(key.exists())
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_public_or_symlinked_key_is_rejected(self):
        self.store.set('basemap', {'fixture': 'keep'})
        original = self.store.path.read_bytes()
        key = self.store.path.with_suffix('.key')
        key.chmod(0o644)
        with self.assertRaises(SettingsReadError):
            self.store.set('basemap', {})
        key.chmod(0o600)
        self.assertEqual(self.store.get('basemap'), {'fixture': 'keep'})
        other = key.with_name('outside-key')
        key.rename(other)
        key.symlink_to(other)
        with self.assertRaises(SettingsReadError):
            self.store.set('basemap', {})
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_tar_preserves_executables_and_internal_links_and_rejects_escape(self):
        bundle = self.root/'bundle'
        bundle.mkdir()
        executable = bundle/'SAR'
        executable.write_bytes(b'fixture ELF')
        executable.chmod(0o755)
        (bundle/'alias').symlink_to('SAR')
        archive = self.root/'portable.tar.gz'
        tar_bundle(archive, bundle)
        with tarfile.open(archive) as stream:
            self.assertEqual(stream.getmember('SAR/SAR').mode & 0o777, 0o755)
            self.assertTrue(stream.getmember('SAR/alias').issym())
        (self.root/'state').mkdir()
        (bundle/'escape').symlink_to(self.root/'state', target_is_directory=True)
        with self.assertRaises(ValueError):
            tar_bundle(archive, bundle)
