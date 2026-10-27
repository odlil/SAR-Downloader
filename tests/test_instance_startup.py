"""Local port selection and duplicate launches without touching the default service."""
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_runtime import APP_ID, VERSION, REVISION, StateLock
from server import Handler, bind_local_server, existing_instance, main


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.root = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_occupied_preferred_port_falls_back_to_loopback_ephemeral_port(self):
        occupied = bind_local_server(0)
        try:
            server = bind_local_server(preferred_port=occupied.server_port)
            try:
                self.assertEqual(server.server_address[0], '127.0.0.1')
                self.assertNotEqual(server.server_port, occupied.server_port)
            finally:
                server.server_close()
        finally:
            occupied.server_close()

    def test_explicit_occupied_port_is_not_silently_changed(self):
        server = bind_local_server(0)
        try:
            with self.assertRaises(OSError):
                bind_local_server(server.server_port)
        finally:
            server.server_close()

    def test_busy_legacy_state_is_not_reused_or_modified(self):
        queuefile = self.root / 'downloads.json'
        queuefile.write_bytes(b'original fixture queue')
        with StateLock(self.root), patch('server.existing_instance', return_value=None), \
             patch('server.bind_local_server') as bind, patch('server.webbrowser.open') as browser, \
             patch.object(sys, 'argv', ['SAR', '--port', '0', '--state-dir', str(self.root)]):
            self.assertEqual(main(), 1)
        bind.assert_not_called()
        browser.assert_not_called()
        self.assertEqual(queuefile.read_bytes(), b'original fixture queue')

    def test_foreign_or_malformed_instance_address_never_reaches_network(self):
        base = {'app': APP_ID, 'version': VERSION, 'revision': REVISION, 'instanceId': 'a' * 32}
        for url in ('https://127.0.0.1:1234', 'http://example.com:1234', 'http://127.0.0.1:1234/path',
                    'http://user:password@127.0.0.1:1234', 'http://127.0.0.1:99999'):
            with self.subTest(url=url):
                (self.root / 'instance.json').write_text(json.dumps({**base, 'url': url}))
                with patch('server.build_opener') as opener:
                    self.assertIsNone(existing_instance(self.root, attempts=1))
                    opener.return_value.open.assert_not_called()

    def test_health_must_match_revision_nonce_and_capabilities(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT / 'web')))
        server.instance_id = 'b' * 32
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        record = {'app': APP_ID, 'version': VERSION, 'revision': REVISION, 'instanceId': server.instance_id, 'url': url}
        try:
            for change in ({}, {'instanceId': 'c' * 32}, {'revision': 'old'}, {'app': 'another-app'}):
                (self.root / 'instance.json').write_text(json.dumps({**record, **change}))
                self.assertEqual(existing_instance(self.root, attempts=1), None if change else url)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_duplicate_process_reuses_same_service_and_does_not_change_queue(self):
        env = {**os.environ, 'SAR_STATE_DIR': str(self.root), 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        command = [sys.executable, '-u', '-B', str(ROOT / 'sar_app.py'), '--no-browser', '--port', '0', '--state-dir', str(self.root)]
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   encoding='utf-8', env=env, **options)
        lines = queue.Queue()
        def read():
            for line in process.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            base = None
            for _ in range(40):
                try:
                    line = lines.get(timeout=.5)
                except queue.Empty:
                    continue
                if line is None:
                    break
                match = re.search(r'http://127\.0\.0\.1:\d+', line)
                if match:
                    base = match.group(); break
            self.assertIsNotNone(base, 'The isolated service did not start')
            info = (self.root / 'instance.json').read_bytes()
            second = subprocess.run(command, env=env, capture_output=True, encoding='utf-8', timeout=15, **options)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn(base, second.stdout)
            self.assertIn('复用', second.stdout)
            self.assertIsNone(process.poll())
            self.assertEqual((self.root / 'instance.json').read_bytes(), info)
            opener = build_opener(ProxyHandler({}))
            with opener.open(base + '/api/downloads', timeout=5) as response:
                self.assertEqual(json.load(response)['tasks'], [])
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            reader.join(timeout=3)
            process.stdout.close()


if __name__ == '__main__':
    unittest.main()
