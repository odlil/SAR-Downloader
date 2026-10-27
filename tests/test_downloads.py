"""Exercise real streamed bytes and HTTP Range against a local, deterministic source."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from downloads import DownloadManager, filename_for
from earthdata_auth import AuthError, ScopedSession, TransferError

DATA = bytes(range(256)) * 16384
URL = "https://datapool.asf.alaska.edu/test/data.zip"


class Source(BaseHTTPRequestHandler):
    requests_seen = []
    mode = "normal"
    delay = .004
    def log_message(self, *args):
        pass
    def do_GET(self):
        cls = type(self)
        cls.requests_seen.append(dict(self.headers))
        attempt = len(cls.requests_seen)
        status = 202 if cls.mode == "pending" and attempt == 1 else 401 if cls.mode == "auth" else 200
        if status != 200:
            self.send_response(status);self.send_header("Content-Length", "0");self.end_headers();return
        start = int(self.headers.get("Range", "bytes=0-")[6:-1]) if cls.mode != "ignored" else 0
        ranged = "Range" in self.headers and cls.mode != "ignored"
        self.send_response(206 if ranged else 200)
        self.send_header("ETag", '"changed"' if cls.mode == "changed" else '"fixture-v1"')
        self.send_header("Content-Type", "text/html" if cls.mode == "html" else "application/octet-stream")
        self.send_header("Content-Length", str(len(DATA) - start))
        if ranged:self.send_header("Content-Range", f"bytes {start}-{len(DATA)-1}/{len(DATA)}")
        self.end_headers()
        try:
            for offset in range(start, len(DATA), 16384):
                self.wfile.write(DATA[offset:offset + 16384]);self.wfile.flush()
                if cls.mode == "broken" and attempt == 1 and offset >= 524288:
                    return
                time.sleep(cls.delay)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class LocalSession:
    guard = None
    def __init__(self, base):
        self.base = base
        self.http = requests.Session();self.http.trust_env = False
    def request(self, method, url, **kwargs):
        return self.http.request(method, self.base + "/data", timeout=3, **kwargs)
    def close(self):self.http.close()


class LocalAuth:
    def __init__(self, base):self.base=base;self.logged_in=True
    def status(self):return {"loggedIn":self.logged_in,"username":"fixture" if self.logged_in else "","burstReady":True}
    def session(self, **kwargs):
        if not self.logged_in:raise AuthError("login required")
        return LocalSession(self.base)


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=ThreadingHTTPServer(("127.0.0.1",0),Source)
        cls.thread=threading.Thread(target=cls.source.serve_forever,daemon=True);cls.thread.start()
        cls.auth=LocalAuth(f"http://127.0.0.1:{cls.source.server_port}")
    @classmethod
    def tearDownClass(cls):
        cls.source.shutdown();cls.source.server_close();cls.thread.join(3)
    def setUp(self):
        Source.mode="normal";Source.requests_seen=[];self.auth.logged_in=True
        self.folder=tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.root=Path(self.folder.name)
        self.manager=DownloadManager(self.auth,self.root / "state",retry_delay=.01,generation_delay=.01)
        self.manager.configure({"output":str(self.root / "output"),"concurrent":2})
    def tearDown(self):
        self.manager.close()
        self.folder.cleanup()
    def add(self, url=URL, filename="scene.zip"):
        self.manager.add([{"url":url,"filename":filename,"title":"fixture image"}])
        return self.manager.snapshot()["tasks"][-1]["id"]

    def test_one_bad_saved_record_does_not_discard_later_valid_jobs(self):
        self.add(URL+'first')
        self.add(URL+'bad')
        self.add(URL+'last')
        self.manager.close()
        path=self.root/'state'/'downloads.json'
        data=json.loads(path.read_text(encoding='utf-8'))
        data['jobs'][1]['url']='invalid-fixture'
        original=json.dumps(data).encode()
        path.write_bytes(original)
        self.manager=DownloadManager(self.auth,self.root/'state')
        self.assertEqual(len(self.manager.snapshot()['tasks']),2)
        self.assertTrue(self.manager.storage_error)
        self.assertEqual(next(path.parent.glob('downloads.json.invalid-*.bak')).read_bytes(),original)
        self.assertEqual(self.manager.settings['output'],str(self.root/'output'))

    def test_close_waits_for_worker_to_finish_saving(self):
        entered, stopping, allow_finish, closed = (threading.Event() for _ in range(4))
        from downloads import Paused
        self.add()
        def transfer(job, event, session):
            entered.set()
            event.wait(3)
            stopping.set()
            allow_finish.wait(3)
            raise Paused()
        def close():
            self.manager.close()
            closed.set()
        with patch.object(self.manager, '_transfer', side_effect=transfer):
            self.manager.action('start')
            self.assertTrue(entered.wait(3))
            closer = threading.Thread(target=close)
            closer.start()
            try:
                self.assertTrue(stopping.wait(3))
                self.assertFalse(closed.is_set())
            finally:
                allow_finish.set()
                closer.join(5)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.manager.running, {})
        self.assertEqual(self.manager.budget.active, 0)
    def wait(self, condition, timeout=8):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            snapshot=self.manager.snapshot()
            if condition(snapshot):return snapshot
            time.sleep(.01)
        self.fail("Download did not reach expected state: "+str(self.manager.snapshot()))
    def done(self):
        return self.wait(lambda result: all(t["state"]=="completed" and not t["busy"] for t in result["tasks"]))["tasks"][0]
    def verify_file(self, job):
        self.assertEqual(hashlib.sha256(Path(job["target"]).read_bytes()).digest(),hashlib.sha256(DATA).digest())
        self.assertEqual(job["completed"],len(DATA))
        self.assertFalse((Path(job['output'])/'.sar-parts'/job['id']).exists())
    def pause(self):
        self.wait(lambda result: result["tasks"][0]["completed"]>0)
        self.manager.action("pause")
        return self.wait(lambda result: not result["tasks"][0]["busy"])["tasks"][0]
    def test_pause_resume_and_process_restart_preserve_exact_bytes(self):
        ident=self.add();self.manager.action("start");job=self.pause()
        self.assertLess(job["completed"],len(DATA));self.assertGreater(job["completed"],0)
        self.manager.close()
        self.manager=DownloadManager(self.auth,self.root / "state",retry_delay=.01)
        self.assertEqual(self.manager.snapshot()["tasks"][0]["state"],"paused")
        self.manager.action("resume",[ident]);self.verify_file(self.done())
        self.assertTrue(any(int(h.get("Range","bytes=0-")[6:-1])>0 for h in Source.requests_seen))
        self.assertTrue(any(h.get("If-Range")=='"fixture-v1"' for h in Source.requests_seen))
    def test_range_ignored_restarts_instead_of_corrupt_append(self):
        self.add();self.manager.action("start");self.pause();Source.mode="ignored"
        self.manager.action("resume");self.verify_file(self.done())
    def test_transient_disconnect_automatically_resumes(self):
        Source.mode="broken";self.add();self.manager.action("start");self.verify_file(self.done())
        self.assertGreater(len(Source.requests_seen),1)
        self.assertIn("Range",Source.requests_seen[1])
    def test_asf_cdn_chain_streams_and_resumes_without_leaking_auth_or_signed_urls(self):
        # Exercise the production session and prepared requests. Only the HTTPS
        # transport is replaced: CDN bodies come from our real local HTTP source.
        seen = []
        base = self.auth.base
        class RedirectTransport(requests.adapters.BaseAdapter):
            def __init__(self):
                self.local = requests.Session();self.local.trust_env = False
            def send(self, request, **kwargs):
                host = urlsplit(request.url).hostname
                seen.append((host, dict(request.headers)))
                if host == "dy4owt9f80bz7.cloudfront.net":
                    result = self.local.get(base + "/data", headers=dict(request.headers), stream=True, timeout=3)
                    result.url = request.url
                    return result
                result = requests.Response()
                result.status_code = 307 if host == "datapool.asf.alaska.edu" else 302
                result.url = request.url;result.request = request;result._content = b""
                result.headers["Location"] = ("https://sentinel1.asf.alaska.edu/SLC/SC/scene.zip"
                    if host == "datapool.asf.alaska.edu" else
                    "https://dy4owt9f80bz7.cloudfront.net/scene.zip?X-Amz-Signature=fixture-signed-secret")
                return result
            def close(self):self.local.close()
        def delegated_session(**kwargs):
            session = ScopedSession(token="fixture-token")
            session.http.trust_env = False
            session.http.cookies.set("asf-urs", "fixture-cookie", domain=".asf.alaska.edu")
            session.http.mount("https://", RedirectTransport())
            return session
        Source.mode = "broken"
        with patch.object(self.auth, "session", side_effect=delegated_session):
            self.add();self.manager.action("start");job = self.done();self.verify_file(job)
        self.assertEqual([host for host, _ in seen],
                         ["datapool.asf.alaska.edu", "sentinel1.asf.alaska.edu", "dy4owt9f80bz7.cloudfront.net"] * 2)
        for host, headers in seen:
            if host.endswith("cloudfront.net"):
                self.assertNotIn("Authorization", headers);self.assertNotIn("Cookie", headers)
            else:
                self.assertEqual(headers["Authorization"], "Bearer fixture-token")
                self.assertIn("fixture-cookie", headers["Cookie"])
        self.assertGreater(int(seen[-1][1]["Range"][6:-1]), 0)
        self.assertEqual(seen[-1][1]["If-Range"], '"fixture-v1"')
        manifest = (self.root / "state" / "downloads.json").read_text(encoding="utf-8")
        self.assertFalse((Path(job["output"]) / ".sar-parts" / job["id"] / "resume.json").exists())
        for stored in (manifest,):
            self.assertNotIn("fixture-token", stored);self.assertNotIn("fixture-cookie", stored)
            self.assertNotIn("fixture-signed-secret", stored);self.assertNotIn("cloudfront.net", stored)
    def test_burst_202_wait_is_not_written_as_an_image(self):
        Source.mode="pending";self.add("https://sentinel1-burst.asf.alaska.edu/scene/IW3/VV/6.tiff", "burst.tiff")
        self.manager.action("start");self.verify_file(self.done());self.assertEqual(len(Source.requests_seen),2)
    def test_changed_validator_on_206_is_rejected(self):
        self.add();self.manager.action("start");job=self.pause();Source.mode="changed"
        self.manager.action("resume")
        job=self.wait(lambda result: result["tasks"][0]["state"]=="error" and not result["tasks"][0]["busy"])["tasks"][0]
        self.assertIn("校验标识",job["error"]);self.assertFalse(Path(job["target"]).exists())
    def test_login_html_and_401_never_become_completed_data(self):
        for mode in ("auth","html"):
            Source.mode=mode
            ident=self.add(URL+mode,mode+".zip");self.manager.action("start",[ident])
            result=self.wait(lambda result: any(t["id"]==ident and t["state"]=="auth_required" and not t["busy"] for t in result["tasks"]))
            job=next(t for t in result["tasks"] if t["id"]==ident)
            self.assertFalse(Path(job["target"]).exists())
    def test_existing_file_is_not_overwritten_and_duplicate_queue_deduplicates(self):
        self.add();self.add();self.assertEqual(len(self.manager.snapshot()["tasks"]),1)
        output=self.root / "output";output.mkdir(exist_ok=True)
        (output / "scene.zip").write_bytes(b"existing-user-data")
        from preflight import PreflightError
        with self.assertRaises(PreflightError):
            self.manager.action("start")
        self.assertFalse(self.manager.snapshot()['tasks'][0]['busy'])
        self.assertEqual(Source.requests_seen,[])
        self.assertEqual((output / "scene.zip").read_bytes(),b"existing-user-data")
    def test_login_and_directory_required_without_starting_network(self):
        self.add();self.auth.logged_in=False
        with self.assertRaises(AuthError):self.manager.action("start")
        self.assertEqual(Source.requests_seen,[])
    def test_public_dem_downloads_without_earthdata_while_sar_waits_for_login(self):
        from test_auxiliary import TILE_URL
        self.auth.logged_in=False
        self.add();self.manager.add([{'url':TILE_URL}])
        with patch('downloads.ScopedSession',side_effect=lambda:LocalSession(self.auth.base)), \
             patch.object(self.auth,'session',side_effect=AssertionError('public source used Earthdata')):
            self.manager.action('start')
            result=self.wait(lambda r:any(t['kind']=='dem' and t['state']=='completed' and not t['busy'] for t in r['tasks']))
        orbit=next(t for t in result['tasks'] if t['kind']=='dem');self.verify_file(orbit)
        self.assertEqual(next(t for t in result['tasks'] if t['kind']=='sar')['state'],'auth_required')
        self.assertTrue(all('Authorization' not in h and 'Cookie' not in h for h in Source.requests_seen))

    def test_dem_pause_and_restart_preserve_job_and_completed_directory(self):
        from downloads import Paused
        self.auth.logged_in=False;started=threading.Event()
        item={'kind':'dem-process','plan':{'bounds':[86,43,86.01,43.01],'height':'egm2008','format':'GTiff'}}
        self.manager.add([item]);ident=self.manager.snapshot()['tasks'][0]['id']
        def pause_worker(manager,job,event):
            manager._update(job,state='processing',progress=.5,phase='fixture');started.set()
            event.wait(5);raise Paused()
        with patch('dem_jobs.run_dem_job',side_effect=pause_worker):
            self.manager.action('start');self.assertTrue(started.wait(3));self.manager.action('pause')
            paused=self.wait(lambda r:not r['tasks'][0]['busy'])['tasks'][0]
        self.assertEqual(paused['state'],'paused');self.assertEqual(paused['progress'],.5)
        self.manager.close();self.manager=DownloadManager(self.auth,self.root/'state')
        def complete_worker(manager,job,event):
            Path(job['target']).mkdir();(Path(job['target'])/'elevation.tif').write_bytes(b'fixture')
        with patch('dem_jobs.run_dem_job',side_effect=complete_worker):
            self.manager.action('resume',[ident]);self.done()
        self.manager.close();self.manager=DownloadManager(self.auth,self.root/'state')
        self.assertEqual(self.manager.snapshot()['tasks'][0]['state'],'completed')
    def test_asf_orbit_reuses_earthdata_authentication(self):
        from test_auxiliary import ORBIT_URL
        self.manager.add([{'url':ORBIT_URL}]);self.auth.logged_in=False
        with self.assertRaises(AuthError):self.manager.action('start')
        self.auth.logged_in=True
        with patch.object(self.auth,'session',wraps=self.auth.session) as session:
            self.manager.action('start');self.verify_file(self.done())
        session.assert_called_once_with(burst=False)
    def test_limit_parallel_transfers_and_collision_names_are_distinct(self):
        self.manager.configure({"output":str(self.root / "output"),"concurrent":1})
        self.add(URL+"one");self.add(URL+"two");self.manager.action("start")
        for _ in range(10):
            self.assertLessEqual(len(self.manager.running),1);time.sleep(.01)
        result=self.wait(lambda r:all(t["state"]=="completed" and not t["busy"] for t in r["tasks"]))
        self.assertEqual(len({t["target"] for t in result["tasks"]}),2)
        for job in result["tasks"]:self.verify_file(job)
    def test_invalid_asset_paths_are_rejected_before_any_disk_write(self):
        for filename in ("../secret.zip","C:\\test.zip","CON.zip","bad.exe"):
            with self.assertRaises(TransferError):self.add(filename=filename)
        for url in ("http://datapool.asf.alaska.edu/a.zip","https://example.org/a.zip","https://datapool.asf.alaska.edu/a.zip?token=secret"):
            with self.assertRaises(TransferError):self.add(url)
        self.assertEqual(Source.requests_seen,[])
    def test_batch_removal_only_deletes_records_preserving_files_and_parts(self):
        one=self.add(URL+'one','one.zip');two=self.add(URL+'two','two.zip')
        output=self.root/'output';output.mkdir(exist_ok=True)
        file=output/'one.zip';file.write_bytes(b'completed fixture')
        part=output/'.sar-parts'/two/'payload.part';part.parent.mkdir(parents=True);part.write_bytes(b'partial fixture')
        self.manager.jobs[one].update(state='completed',output=str(output),target=str(file))
        self.manager.jobs[two].update(state='paused',output=str(output),target=str(output/'two.zip'))
        self.manager.action('remove',[one]);self.assertEqual(len(self.manager.snapshot()['tasks']),1)
        self.manager.action('remove',[two]);self.assertFalse(self.manager.snapshot()['tasks'])
        self.assertEqual(file.read_bytes(),b'completed fixture');self.assertEqual(part.read_bytes(),b'partial fixture')


if __name__=="__main__":unittest.main(verbosity=2)
