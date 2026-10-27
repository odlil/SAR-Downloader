from pathlib import Path
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earthdata_auth import AuthError, EarthdataAuth, TransferError
from local_secrets import BasemapSettings, LocalSecrets, SettingsReadError, protect
from test_earthdata_auth import response


@unittest.skipUnless(sys.platform == 'win32', 'Windows current-user DPAPI')
class RememberedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.store = LocalSecrets(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def login(self):
        auth = EarthdataAuth(self.store)
        with patch('earthdata_auth.ScopedSession') as factory:
            session = factory.return_value
            session.request.side_effect = [response(data={'access_token': 'fixture-token'}), response()]
            session.http.cookies = requests.cookies.RequestsCookieJar()
            session.http.cookies.set('asf-urs', 'fixture-cookie', domain='.asf.alaska.edu', secure=True)
            session.http.cookies.set('irrelevant', 'fixture-third-party', domain='untrusted.example')
            session.http.cookies.set('expired', 'fixture-expired', domain='.asf.alaska.edu', expires=int(time.time())-30)
            auth.login('fixture-user', 'fixture-password')
        return auth

    def test_real_encryption_and_restart_restore_login_and_cookie_scopes(self):
        auth = self.login()
        auth.close()
        restored = EarthdataAuth(LocalSecrets(self.folder.name))
        self.assertEqual(restored.status(), {'loggedIn': True, 'username': 'fixture-user', 'burstReady': True})
        with restored.lock:
            self.assertEqual([(c.name, c.domain, c.secure) for c in restored.cookies], [('asf-urs', '.asf.alaska.edu', True)])
        transfer = restored.session(burst=True)
        self.assertEqual(transfer.token, 'fixture-token')
        transfer.close()
        raw = self.store.path.read_bytes()
        for secret in ('fixture-token', 'fixture-cookie', 'fixture-user', 'fixture-password'):
            self.assertNotIn(secret.encode(), raw)
        self.assertNotIn('fixture-password', str(self.store.get('earthdata')))

    def test_expired_burst_cookie_requires_authorization_again(self):
        self.login().close()
        saved = self.store.get('earthdata')
        for cookie in saved['cookies']:
            cookie['expires'] = int(time.time())-1
        self.store.set('earthdata', saved)
        restored = EarthdataAuth(self.store)
        self.assertFalse(restored.status()['burstReady'])
        with self.assertRaises(AuthError):
            restored.session(burst=True)

    def test_logout_removes_login_but_preserves_both_provider_keys(self):
        auth = self.login()
        basemap = BasemapSettings(self.store)
        basemap.save({'provider': 'tianditu', 'kind': 'satellite', 'key': 'fixture-tdt-key'})
        basemap.save({'provider': 'esri', 'kind': 'satellite', 'key': 'fixture-esri-key'})
        basemap.save({'provider': 'esri', 'kind': 'osm'})
        transfer = auth.session()
        auth.logout()
        with self.assertRaises(AuthError):
            transfer.guard()
        transfer.close()
        self.assertFalse(EarthdataAuth(LocalSecrets(self.folder.name)).status()['loggedIn'])
        restored = BasemapSettings(LocalSecrets(self.folder.name)).get()
        self.assertEqual(restored, {'provider': 'esri', 'kind': 'osm', 'keys': {'tianditu': 'fixture-tdt-key', 'esri': 'fixture-esri-key'}})
        self.assertNotIn(b'fixture-tdt-key', self.store.path.read_bytes())

    def test_save_failure_does_not_destroy_previous_settings(self):
        self.store.set('basemap', {'fixture': 'previous'})
        with patch('local_secrets.protect', side_effect=OSError('fixture-error')):
            with self.assertRaises(OSError):
                self.store.set('basemap', {'fixture': 'replacement'})
        self.assertEqual(self.store.get('basemap'), {'fixture': 'previous'})

    def test_corruption_allows_start_but_never_overwrites_original_settings(self):
        self.store.path.write_bytes(b'corrupt encrypted data')
        self.assertFalse(EarthdataAuth(self.store).status()['loggedIn'])
        basemap = BasemapSettings(self.store)
        self.assertEqual(basemap.get()['kind'], 'osm')
        with self.assertRaises(SettingsReadError):
            basemap.save({'provider': 'tianditu', 'kind': 'satellite', 'key': 'fixture-new-key'})
        self.assertEqual(self.store.path.read_bytes(), b'corrupt encrypted data')
        self.assertIn('原文件已保留', basemap.get()['warning'])

    def test_transient_decryption_failure_preserves_saved_login_and_keys(self):
        self.login()
        original = self.store.path.read_bytes()
        def failed_read(data, *, decrypt=False):
            if decrypt:
                raise OSError('fixture-read-failure')
            return protect(data)
        with patch('local_secrets.protect', side_effect=failed_read):
            with self.assertRaises(SettingsReadError):
                BasemapSettings(self.store).save({'provider': 'esri', 'kind': 'satellite', 'key': 'fixture-key'})
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertTrue(EarthdataAuth(self.store).status()['loggedIn'])

    def test_invalid_basemap_does_not_replace_existing_key(self):
        basemap = BasemapSettings(self.store)
        basemap.save({'provider': 'tianditu', 'kind': 'satellite', 'key': 'fixture-key'})
        for payload in ({'provider': 'other', 'kind': 'satellite', 'key': 'fixture'},
                        {'provider': 'esri', 'kind': 'satellite'},
                        {'provider': 'tianditu', 'kind': 'satellite', 'key': ''}):
            with self.assertRaises(TransferError):
                basemap.save(payload)
        self.assertEqual(basemap.get()['keys'], {'tianditu': 'fixture-key'})

    def test_concurrent_provider_saves_keep_both_keys_and_login(self):
        self.login()
        original_get = self.store.get
        barrier = threading.Barrier(2)
        failures = []
        def stale_read(name):
            result = original_get(name)
            barrier.wait(timeout=3)
            return result
        def save(provider):
            try:
                BasemapSettings(self.store).save({'provider': provider, 'kind': 'satellite', 'key': 'fixture-' + provider})
            except Exception as error:
                failures.append(error)
        with patch.object(self.store, 'get', side_effect=stale_read):
            threads = [threading.Thread(target=save, args=(provider,)) for provider in ('tianditu', 'esri')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(BasemapSettings(self.store).get()['keys'], {'tianditu': 'fixture-tianditu', 'esri': 'fixture-esri'})
        self.assertTrue(EarthdataAuth(self.store).status()['loggedIn'])
