from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earthdata_auth import AuthError
from preflight import inspect, ensure_ready, PreflightError


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.folder.name)
        self.settings = {'output': str(self.root / 'data'), 'concurrent': 2}
        self.task = {'id': 'fixture', 'kind': 'dem', 'state': 'queued', 'busy': False,
                     'title': 'fixture DEM', 'filename': 'tile.tif', 'target': '', 'output': '', 'expectedBytes': 1000}

    def tearDown(self):
        self.folder.cleanup()

    def test_public_download_checks_disk_without_network_or_creating_output(self):
        with patch('requests.Session.request', side_effect=AssertionError('unexpected network')):
            report = inspect([self.task], self.settings, {'loggedIn': False})
        self.assertTrue(report['canStart'])
        self.assertEqual(report['knownBytes'], 1000)
        self.assertFalse(Path(self.settings['output']).exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_estimates_aggregate_same_disk_and_include_partial_files(self):
        output = Path(self.settings['output']) / 'dem'
        part = output / '.sar-parts' / 'fixture' / 'payload.part'
        part.parent.mkdir(parents=True)
        part.write_bytes(b'x'*400)
        other = {**self.task, 'id': 'other', 'output': str(self.root/'other')}
        with patch('preflight.writable_directory', return_value=(self.root, 1200, (1, 'disk'))):
            report = inspect([self.task, other], self.settings, {'loggedIn': False})
        self.assertEqual(report['knownBytes'], 1600)
        self.assertFalse(report['canStart'])
        self.assertIn('SPACE_INSUFFICIENT', [check['code'] for check in report['checks']])

    def test_existing_target_and_permissions_block_before_any_download(self):
        output=Path(self.settings['output'])/'dem';output.mkdir(parents=True)
        target=output/'tile.tif';target.write_bytes(b'keep fixture')
        report=inspect([self.task],self.settings,{'loggedIn':False})
        with self.assertRaises(PreflightError):ensure_ready(report)
        self.assertEqual(target.read_bytes(),b'keep fixture')
        target.unlink()
        with patch('preflight.writable_directory',side_effect=PermissionError('fixture')):
            report=inspect([self.task],self.settings,{'loggedIn':False})
        self.assertFalse(report['canStart'])
        self.assertIn('OUTPUT_UNWRITABLE',[check['code'] for check in report['checks']])

    def test_public_tasks_remain_ready_when_protected_tasks_need_login(self):
        protected={**self.task,'id':'protected','kind':'sar','filename':'scene.zip'}
        report=inspect([self.task,protected],self.settings,{'loggedIn':False})
        self.assertTrue(report['canStart'])
        self.assertEqual(report['readyCount'],1)
        self.assertEqual(report['blockedCount'],1)
        ensure_ready(report)
        with self.assertRaises(AuthError):
            ensure_ready(inspect([protected],self.settings,{'loggedIn':False}))

    def test_same_name_tasks_check_their_distinct_execution_targets(self):
        output = Path(self.settings['output']) / 'dem'
        output.mkdir(parents=True)
        conflict = output / 'tile--other.tif'
        conflict.write_bytes(b'preserve existing output')
        other = {**self.task, 'id': 'other'}
        report = inspect([self.task, other], self.settings, {'loggedIn': False})
        self.assertFalse(report['canStart'])
        self.assertTrue(report['tasks'][0]['ready'])
        self.assertIn('TARGET_EXISTS', report['tasks'][1]['issues'])
        self.assertEqual(conflict.read_bytes(), b'preserve existing output')

    def test_dem_environment_failure_and_unknown_size_are_explicit(self):
        task={**self.task,'kind':'dem-process'}
        with patch('preflight.dem_environment',return_value=False) as check:
            report=inspect([task,{**task,'id':'second'}],self.settings,{'loggedIn':False})
        check.assert_called_once()
        self.assertEqual(report['unknownCount'],2)
        self.assertEqual(report['knownBytes'],0)
        self.assertFalse(report['canStart'])

    def test_scope_excludes_busy_completed_and_paused_on_start(self):
        tasks=[self.task,{**self.task,'id':'busy','busy':True},
               {**self.task,'id':'completed','state':'completed'},
               {**self.task,'id':'paused','state':'paused'}]
        report=inspect(tasks,self.settings,{'loggedIn':False},action='start')
        self.assertEqual([row['id'] for row in report['tasks']],['fixture'])
        report=inspect(tasks,self.settings,{'loggedIn':False},ids=['paused'])
        self.assertEqual([row['id'] for row in report['tasks']],['paused'])
