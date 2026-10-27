"""Public release checks: versions, scoped downloads, persistence and failures."""
import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from updates import UpdateError, UpdateService, normalize_repository, parse_version, platform_asset
from updates import UpdateNetworkError

REPO='publisher/SAR-Downloader'


def release(tag='v1.1.0'):
    return {'tag_name':tag,'draft':False,'prerelease':False,'body':'A new release',
            'published_at':'2026-10-23T00:00:00Z',
            'assets':[{'name':f'SAR-{tag}-{suffix}','state':'uploaded','size':1024,
                       'browser_download_url':f'https://github.com/{REPO}/releases/download/{tag}/SAR-{tag}-{suffix}'}
                      for suffix in ('windows-x64.zip','linux-x86_64.tar.gz','source.zip')]}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root=Path(self.temp.name)
        self.config=self.root/'update_config.json'
        self.config.write_text(json.dumps({'repository':REPO}),encoding='utf-8')
        self.service=UpdateService(self.root/'state',self.config)

    def tearDown(self):self.temp.cleanup()

    def test_versions_compare_numerically_and_ignore_zero_patch(self):
        self.assertEqual(parse_version('v1.0'),parse_version('1.0.0'))
        self.assertGreater(parse_version('1.10'),parse_version('1.9.9'))
        for value in ('main','v1.1-rc1','1.0.1.1',None,{},'https://example.test'):
            with self.assertRaises(UpdateError):parse_version(value)

    def test_repository_input_is_only_an_owner_and_repo_not_arbitrary_network(self):
        for value in (REPO,'https://github.com/'+REPO+'/', 'https://github.com/'+REPO+'.git'):
            self.assertEqual(normalize_repository(value),REPO)
        for value in ('http://github.com/'+REPO,'https://127.0.0.1/x/y','https://github.com.evil.test/a/b',
                      'https://user:password@github.com/a/b','https://github.com/a/b?token=secret',
                      'https://github.com/a/b/releases/latest','../x','',None):
            with self.assertRaises(UpdateError):normalize_repository(value)

    def test_settings_are_local_only_and_saved_choice_overrides_bundled_default(self):
        with patch('updates.urlopen',side_effect=AssertionError('unexpected network')):
            self.assertEqual(self.service.settings()['repository'],REPO)
        with patch.object(self.service,'_latest',return_value=release()):
            self.service.check({'repository':REPO})
        self.assertEqual(UpdateService(self.root/'state',self.config).settings()['repository'],REPO)
        self.assertEqual(json.loads(self.service.path.read_text()),{'repository':REPO})

    def test_blank_or_unpublished_repository_reports_actionable_error(self):
        self.config.write_text('{"repository":""}')
        with patch.object(self.service,'_latest') as fetch:
            with self.assertRaisesRegex(UpdateError,'尚未配置'):self.service.check({})
            fetch.assert_not_called()

    def test_platform_detection_avoids_wrong_architecture_binaries(self):
        for system,machine,libc,expected in [('Windows','AMD64','','windows-x64.zip'),
                  ('Linux','x86_64','glibc','linux-x86_64.tar.gz'),('Linux','aarch64','glibc','source.zip'),
                  ('Linux','x86_64','musl','source.zip'),('Darwin','arm64','','source.zip')]:
            with patch('updates.platform.system',return_value=system),patch('updates.platform.machine',return_value=machine),patch('updates.platform.libc_ver',return_value=(libc,'')):
                self.assertEqual(platform_asset()[0],expected)

    def test_new_equal_and_older_releases_have_distinct_states(self):
        with patch('updates.platform_asset',return_value=('windows-x64.zip','Windows x64')):
            new=self.service._result(REPO,release())
            self.assertTrue(new['updateAvailable']);self.assertEqual(new['package']['kind'],'windows-x64.zip')
            self.assertEqual(new['sourcePackage']['kind'],'source.zip')
            same=self.service._result(REPO,release('v1.0.0'))
            self.assertFalse(same['updateAvailable']);self.assertFalse(same['localNewer'])
            self.assertTrue(self.service._result(REPO,release('v0.9'))['localNewer'])

    def test_draft_prerelease_and_invalid_upstream_metadata_are_rejected(self):
        for changes in ({'draft':True},{'prerelease':True},{'assets':{}},{'tag_name':'v1.1-rc1'}):
            with self.assertRaises(UpdateError):self.service._result(REPO,{**release(),**changes})

    def test_download_assets_must_belong_to_same_repo_tag_and_version(self):
        for url in ('javascript:alert(1)','https://evil.test/file.zip',
                    'https://github.com/other/repo/releases/download/v1.1.0/SAR-v1.1.0-windows-x64.zip',
                    f'https://github.com/{REPO}/releases/download/v0.9/SAR-v1.1.0-windows-x64.zip'):
            data=release();data['assets'][0]['browser_download_url']=url
            with patch('updates.platform_asset',return_value=('windows-x64.zip','Windows x64')):
                result=self.service._result(REPO,data)
            self.assertIsNone(result['package'])
            self.assertTrue(result['updateAvailable'])
        data=release();data['assets'][0]['name']='SAR-v9.0-windows-x64.zip'
        with patch('updates.platform_asset',return_value=('windows-x64.zip','Windows x64')):
            self.assertIsNone(self.service._result(REPO,data)['package'])

    def test_repository_case_differences_do_not_hide_valid_download(self):
        data=release()
        with patch('updates.platform_asset',return_value=('windows-x64.zip','Windows x64')):
            self.assertIsNotNone(self.service._result(REPO.lower(),data)['package'])

    def test_git_network_errors_never_expose_request_internals(self):
        for status,message in ((404,'尚未发布'),(403,'限制访问'),(429,'限制访问'),(301,'已变更'),(500,'暂不可用')):
            with patch('updates.urlopen') as fetch:
                fetch.side_effect=HTTPError('https://api.github.com',status,'fixture',{},io.BytesIO())
                with self.assertRaisesRegex(UpdateError,message):self.service._latest(REPO)
                request=fetch.call_args.args[0]
                self.assertNotIn('Authorization',request.headers)
        with patch('updates.urlopen') as fetch,patch('updates.time.sleep'):
            fetch.side_effect=URLError('secret request details')
            with self.assertRaises(UpdateError) as caught:self.service._latest(REPO)
            self.assertNotIn('secret',str(caught.exception))

    def test_metadata_size_and_json_are_bounded(self):
        reply=Mock(status=200);reply.__enter__=Mock(return_value=reply);reply.__exit__=Mock(return_value=False)
        reply.geturl.return_value=f'https://api.github.com/repos/{REPO}/releases/latest'
        for raw in (b'not-json',b'x'*(2*1024*1024+1)):
            reply.read.return_value=raw
            with patch('updates.urlopen',return_value=reply):
                with self.assertRaises(UpdateError):self.service._latest(REPO)

    def test_verified_https_metadata_request_succeeds_without_credentials(self):
        reply=Mock(status=200);reply.__enter__=Mock(return_value=reply);reply.__exit__=Mock(return_value=False)
        reply.geturl.return_value=f'https://api.github.com/repos/{REPO}/releases/latest'
        reply.read.return_value=json.dumps(release()).encode()
        with patch('updates.urlopen',return_value=reply) as fetch:
            self.assertEqual(self.service._latest(REPO)['tag_name'],'v1.1.0')
            request=fetch.call_args.args[0]
            self.assertEqual(request.full_url,f'https://api.github.com/repos/{REPO}/releases/latest')
            self.assertNotIn('Authorization',request.headers)
            self.assertTrue(fetch.call_args.kwargs['context'].check_hostname)

    def test_redirected_metadata_is_not_accepted_as_an_update(self):
        reply=Mock(status=200);reply.__enter__=Mock(return_value=reply);reply.__exit__=Mock(return_value=False)
        reply.geturl.return_value='https://other.example/release'
        with patch('updates.urlopen',return_value=reply):
            with self.assertRaisesRegex(UpdateError,'已变更'):self.service._latest(REPO)

    def test_transient_network_failure_retries_once_without_retrying_bad_releases(self):
        with patch('updates.UpdateService._fetch_latest',side_effect=[UpdateNetworkError('network'),release()]) as fetch,patch('updates.time.sleep'):
            self.assertEqual(self.service._latest(REPO)['tag_name'],'v1.1.0')
            self.assertEqual(fetch.call_count,2)
        with patch('updates.UpdateService._fetch_latest',side_effect=UpdateError('not found')) as fetch:
            with self.assertRaises(UpdateError):self.service._latest(REPO)
            self.assertEqual(fetch.call_count,1)

    def test_checks_never_touch_the_image_queue(self):
        queue=self.service.path.parent/'downloads.json';queue.parent.mkdir(parents=True)
        queue.write_bytes(b'original queue bytes')
        with patch.object(self.service,'_latest',return_value=release()):self.service.check({'repository':REPO})
        self.assertEqual(queue.read_bytes(),b'original queue bytes')


if __name__=='__main__':unittest.main()
