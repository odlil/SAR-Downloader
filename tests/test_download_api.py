from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from downloads import DownloadManager
from download_hub import DownloadHub
from earthdata_auth import EarthdataAuth
from local_secrets import LocalSecrets, BasemapSettings
from server import Handler


class DownloadEndpointTests(unittest.TestCase):
    def test_update_endpoints_require_local_origin_and_valid_json(self):
        for route in ('/api/updates','/api/updates/check'):
            response=(self.http.get(self.base+route,headers={'Origin':'https://evil.example'})
                      if route=='/api/updates' else self.http.post(self.base+route,json={},headers={'Origin':'https://evil.example'}))
            self.assertEqual(response.status_code,403)
        self.assertEqual(self.http.post(self.base+'/api/updates/check',data='{}').status_code,415)
        self.assertEqual(self.http.post(self.base+'/api/updates/check',json=[]).status_code,400)
        self.assertEqual(self.http.get(self.base+'/api/updates').status_code,200)
        with patch('updates.UpdateService._latest',side_effect=AssertionError('unexpected network')):
            response=self.http.post(self.base+'/api/updates/check',json={'repository':'https://evil.example/x/y'})
            self.assertEqual(response.status_code,400)

    def test_update_check_returns_platform_download_without_starting_tasks(self):
        from test_updates import REPO,release
        from updates import UpdateService
        self.server.updates=UpdateService(self.folder.name)
        with patch('updates.UpdateService._latest',return_value=release()),patch('updates.platform_asset',return_value=('windows-x64.zip','Windows x64')):
            response=self.http.post(self.base+'/api/updates/check',json={'repository':REPO})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['updateAvailable'])
        self.assertEqual(response.json()['package']['kind'],'windows-x64.zip')
        self.assertEqual(self.server.downloads.snapshot()['tasks'],[])

    def test_health_reports_precise_first_orbit_support(self):
        result = self.http.get(self.base + '/api/health').json()
        self.assertTrue(result['capabilities']['orbitAuto'])
        self.assertTrue(result['capabilities']['auxiliaryFolders'])
        self.assertTrue(result['capabilities']['unfinishedCleanup'])

    def test_unfinished_cleanup_routes_require_local_origin_and_preview_token(self):
        hub = self.server.downloads
        hub.configure({'output': str(Path(self.folder.name) / 'output'), 'concurrent': 1})
        hub.add([{'url': 'https://datapool.asf.alaska.edu/fixture.zip'}])
        task = next(iter(hub.files.jobs.values()))
        part = Path(self.folder.name) / 'output/.sar-parts' / task['id'] / 'payload.part'
        part.parent.mkdir(parents=True); part.write_bytes(b'fixture')
        for path in ('/api/cache/unfinished/preview', '/api/cache/unfinished/clear'):
            self.assertEqual(self.http.post(self.base + path, json={}, headers={'Origin': 'https://other.example'}).status_code, 403)
            self.assertEqual(self.http.post(self.base + path, json={'path': str(part)}).status_code, 400)
        self.assertTrue(part.exists())
        response = self.http.post(self.base + '/api/cache/unfinished/preview', json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(part.exists())
        result = self.http.post(self.base + '/api/cache/unfinished/clear', json={
            'token': response.json()['token'], 'ids': [task['id']], 'removeTasks': False})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['cleanup']['bytesFreed'], 7)
        self.assertFalse(part.exists())

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.server=ThreadingHTTPServer(("127.0.0.1",0),partial(Handler,directory=str(ROOT / "web")))
        self.server.auth=EarthdataAuth()
        self.server.basemap=BasemapSettings(LocalSecrets(self.folder.name))
        self.server.downloads=DownloadHub(self.server.auth,Path(self.folder.name))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f"http://127.0.0.1:{self.server.server_port}"
        self.http=requests.Session();self.http.trust_env=False
    def tearDown(self):
        self.http.close();self.server.downloads.close();self.server.shutdown();self.server.server_close();self.thread.join(3);self.folder.cleanup()
    def test_foreign_origin_host_and_form_posts_cannot_use_credentials_or_queue(self):
        for headers, code in (({"Origin":"https://other.example"},403),({"Host":"rebind.example"},403),({"Content-Type":"text/plain"},415)):
            with patch.object(self.server.auth,"login") as login:
                response=self.http.post(self.base+"/api/account/login",data=b'{}',headers={"Content-Type":"application/json",**headers})
                self.assertEqual(response.status_code,code);login.assert_not_called()
        self.assertEqual(self.http.get(self.base+"/api/account",headers={"Origin":"https://other.example"}).status_code,403)

    def test_post_routes_share_json_validation_before_running_handlers(self):
        with patch('server.search_asf') as search, patch('server.choose_aoi') as picker:
            for path in ('/api/search', '/api/aoi/open', '/api/downloads/add'):
                for body in ('null', '[]', '1', '"text"', '{invalid'):
                    with self.subTest(path=path, body=body):
                        response = self.http.post(self.base+path, data=body,
                                                  headers={'Content-Type': 'application/json'})
                        self.assertEqual(response.status_code, 400)
                response = self.http.post(self.base+path, json={}, headers={'Origin': 'https://other.example'})
                self.assertEqual(response.status_code, 403)
            search.assert_not_called()
            picker.assert_not_called()

    def test_preflight_and_scene_routes_reject_foreign_origins(self):
        added=self.http.post(self.base+'/api/downloads/add',json={'tasks':[{'url':'https://datapool.asf.alaska.edu/test/fixture.zip'}]}).json()
        self.assertEqual(added['added'],1)
        self.assertNotIn('batches',added)
        checked=self.http.post(self.base+'/api/downloads/preflight',json={'action':'start'}).json()
        self.assertFalse(checked['canStart']);self.assertEqual(checked['taskCount'],1)
        for path in ('/api/downloads/preflight','/api/downloads/scenes'):
            self.assertEqual(self.http.post(self.base+path,json={},headers={'Origin':'https://other.example'}).status_code,403)
        self.assertEqual(self.http.get(self.base+'/api/batches/removed').status_code,404)

    def test_conflict_preflight_is_enforced_even_when_client_skips_check(self):
        output=Path(self.folder.name)/'data';output.mkdir()
        self.http.post(self.base+'/api/downloads/settings',json={'concurrent':1,'output':str(output)})
        tile='Copernicus_DSM_COG_10_N43_00_E086_00_DEM'
        self.http.post(self.base+'/api/downloads/add',json={'tasks':[{'url':f'https://copernicus-dem-30m.s3.amazonaws.com/{tile}/{tile}.tif'}]})
        target=output/'dem'/(tile+'.tif');target.parent.mkdir();target.write_bytes(b'keep fixture')
        response=self.http.post(self.base+'/api/downloads/action',json={'action':'start'})
        self.assertEqual(response.status_code,409)
        self.assertIn('preflight',response.json())
        self.assertEqual(target.read_bytes(),b'keep fixture')

    def test_unreadable_private_settings_reject_save_without_destroying_original(self):
        original=b'fixture unreadable encrypted settings'
        path=self.server.basemap.secrets.path
        path.write_bytes(original)
        self.assertIn('warning',self.http.get(self.base+'/api/basemap').json())
        response=self.http.post(self.base+'/api/basemap',json={'provider':'esri','kind':'satellite','key':'fixture-key'})
        self.assertEqual(response.status_code,409)
        self.assertEqual(path.read_bytes(),original)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows current-user DPAPI')
    def test_basemap_remembered_and_only_available_to_local_ui(self):
        payload = {'provider': 'tianditu', 'kind': 'satellite', 'key': 'fixture-private-key'}
        self.assertEqual(self.http.post(self.base+'/api/basemap', json=payload).status_code, 200)
        self.server.basemap=BasemapSettings(LocalSecrets(self.folder.name))
        self.assertEqual(self.http.get(self.base+'/api/basemap').json()['keys'], {'tianditu': 'fixture-private-key'})
        self.assertEqual(self.http.get(self.base+'/api/basemap', headers={'Origin':'https://other.example'}).status_code, 403)
        self.assertEqual(self.http.post(self.base+'/api/basemap', json=payload, headers={'Origin':'https://other.example'}).status_code, 403)
        self.assertNotIn('fixture-private-key', self.http.get(self.base+'/api/downloads').text)
        self.assertEqual(self.http.post(self.base+'/api/basemap', json={**payload,'key':''}).status_code, 400)
    def test_queue_is_persistent_but_never_starts_without_login(self):
        response=self.http.post(self.base+"/api/downloads/add",json={"tasks":[{"url":"https://datapool.asf.alaska.edu/test/scene.zip","filename":"scene.zip"}]})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()["added"],1)
        self.assertEqual(self.http.post(self.base+"/api/downloads/action",json={"action":"start"}).status_code,401)
        result=self.http.get(self.base+"/api/downloads").json()
        self.assertEqual(result["tasks"][0]["state"],"queued")
        self.assertEqual(result["auth"],{"loggedIn":False,"username":"","burstReady":False})
        self.assertTrue((Path(self.folder.name) / "downloads.json").is_file())

    def test_cache_cleanup_is_local_only_and_never_accepts_paths(self):
        path=Path(self.folder.name)/'dem-cache/tiles/cache.tif'
        path.parent.mkdir(parents=True);path.write_bytes(b'fixture')
        self.assertEqual(self.http.post(self.base+'/api/cache/clear',json={},headers={'Origin':'https://other.example'}).status_code,403)
        self.assertEqual(self.http.post(self.base+'/api/cache/clear',json={'path':str(path)}).status_code,400)
        self.assertTrue(path.exists())
        result=self.http.post(self.base+'/api/cache/clear',json={})
        self.assertEqual(result.status_code,200);self.assertEqual(result.json()['cleanup']['bytesFreed'],7)
        self.assertFalse(path.exists())
    def test_login_errors_never_echo_submitted_secret(self):
        response=self.http.post(self.base+"/api/account/login",json={"username":"","password":"fixture-only-secret"})
        self.assertEqual(response.status_code,400);self.assertNotIn("fixture-only-secret",response.text)
        self.assertEqual(list(Path(self.folder.name).iterdir()),[])
    def test_last_search_is_durable_and_foreign_pages_cannot_read_it(self):
        area={'type':'Polygon','coordinates':[[[86,43],[87,43],[87,44],[86,44],[86,43]]]}
        with patch('server.search_asf',return_value={'scenes':[{'bounds':[85,42,88,45]}]}):
            self.assertEqual(self.http.post(self.base+'/api/search',json={'aoi':area}).status_code,200)
        record=self.http.get(self.base+'/api/last-search').json()['location']
        self.assertEqual(record['bounds'],[85,42,88,45])
        self.assertEqual(self.http.get(self.base+'/api/last-search',headers={'Origin':'https://other.example'}).status_code,403)
        with patch('server.search_asf',side_effect=__import__('asf_api').SearchError('fixture timeout',504)):
            self.assertEqual(self.http.post(self.base+'/api/search',json={'aoi':area}).status_code,504)
        self.assertEqual(self.http.get(self.base+'/api/last-search').json()['location'],record)
    def test_auxiliary_routes_validate_before_network_and_reject_foreign_origins(self):
        for path in ('/api/orbits/match','/api/orbits/folder','/api/dem/preview','/api/dem/add'):
            response=self.http.post(self.base+path,json={},headers={'Origin':'https://other.example'})
            self.assertEqual(response.status_code,403)
        with patch('auxiliary_api.copdem_catalog',side_effect=AssertionError('invalid input reached network')):
            self.assertEqual(self.http.post(self.base+'/api/dem/preview',json={'bounds':[0,0,0,1]}).status_code,400)
        self.assertEqual(self.http.post(self.base+'/api/orbits/folder',json={'folder':'C:/private'}).status_code,400)
        result=self.http.post(self.base+'/api/dem/add',json={'bounds':[86,43,86.01,43.01],'height':'egm2008','format':'ISCE'})
        self.assertEqual(result.status_code,200);self.assertEqual(result.json()['tasks'][0]['kind'],'dem-process')
    def test_logout_invalidates_memory_credentials_and_pauses_queue(self):
        self.server.auth.token="fixture-only-token";self.server.auth.username="fixture-user"
        self.server.downloads.add([{"url":"https://datapool.asf.alaska.edu/a.zip"}])
        response=self.http.post(self.base+"/api/account/logout",json={})
        self.assertEqual(response.status_code,200);self.assertFalse(response.json()["loggedIn"])
        result=self.http.get(self.base+"/api/downloads").json()
        self.assertEqual(result["tasks"][0]["state"],"paused")
        self.assertNotIn("fixture-only-token",json.dumps(result))
        self.assertNotIn("fixture-only-token",(Path(self.folder.name) / "downloads.json").read_text(encoding="utf-8"))


if __name__=="__main__":unittest.main(verbosity=2)
