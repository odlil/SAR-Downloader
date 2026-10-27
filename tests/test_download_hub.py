"""Unified queue contracts, without Earthdata credentials or live downloads."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from auxiliary_api import AuxiliaryError, OrbitLinks, match_orbits
from download_hub import DownloadHub
from earthdata_auth import AuthError, EarthdataAuth, TransferError

SCENE = {'id': 'burst-1', 'name': 'burst-1', 'dataset': 'SLC-BURST', 'platform': 'S1A',
         'startTime': '2024-01-30T12:10:07Z', 'stopTime': '2024-01-30T12:10:34Z',
         'bounds': [86.6, 43, 87, 43.25], 'url': 'https://sentinel1-burst.asf.alaska.edu/source/IW2/VV/1.tiff',
         'xmlUrls': ['https://sentinel1-burst.asf.alaska.edu/source/IW2/VV/1.xml']}
EOF = 'S1A_OPER_AUX_POEORB_OPOD_20240219T070659_V20240129T225942_20240131T005942.EOF'
NISAR = {'id': 'NISAR_L1_TEST_RSLC', 'dataset': 'NISAR', 'product': 'RSLC',
         'url': 'https://datapool.asf.alaska.edu/TEST/NISAR_L1_TEST_RSLC.h5'}


def wait_for(predicate):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('后台任务未及时停止')


class HubTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.auth = EarthdataAuth()
        self.hub = DownloadHub(self.auth, Path(self.folder.name) / 'state')
        self.hub.configure({'concurrent': 2, 'output': str(Path(self.folder.name) / 'output')})

    def tearDown(self):
        self.hub.close()
        self.folder.cleanup()

    def payload(self):
        return {'scenes': [NISAR], 'corners': [[86.6, 43.25], [87, 43.25], [87, 43], [86.6, 43]],
                'frequencies': ['A'], 'polarizations': ['HH'], 'connections': 2}

    def test_start_checks_each_destination_once_without_skipping_authentication(self):
        from preflight import writable_directory
        self.hub.add_scenes({'scenes': [NISAR]})
        with patch.object(self.hub.files, '_schedule') as schedule:
            with self.assertRaises(AuthError):
                self.hub.action('start')
            schedule.assert_not_called()
            self.auth.token = 'fixture-token'
            with patch('preflight.writable_directory', wraps=writable_directory) as probe:
                result = self.hub.action('start')
            probe.assert_called_once()
            self.assertTrue(result['tasks'][0]['enabled'])

    def test_files_route_to_imagery_orbit_and_dem_directories(self):
        from test_auxiliary import ORBIT_URL, TILE_URL
        self.auth.token = 'fixture-token'
        self.hub.add([{'url': NISAR['url']}, {'url': ORBIT_URL}, {'url': TILE_URL},
                      {'kind': 'dem-process', 'plan': {'bounds': [86, 43, 86.01, 43.01],
                                                     'height': 'egm2008', 'format': 'GTiff'}}])
        root = Path(self.folder.name) / 'output'
        with patch('preflight.dem_environment', return_value=True):
            report = self.hub.preflight({'action': 'start'})
        self.assertEqual({row['path'] for row in report['destinations']},
                         {str(root), str(root / 'orbit'), str(root / 'dem')})
        self.assertFalse(root.exists())

        def transfer(job, event, session):
            Path(job['target']).write_bytes(b'fixture data')
        def process(manager, job, event):
            Path(job['target']).mkdir()
            (Path(job['target']) / 'elevation.tif').write_bytes(b'fixture DEM')
        with patch('preflight.dem_environment', return_value=True), \
             patch.object(self.hub.files, '_transfer', side_effect=transfer), \
             patch('dem_jobs.run_dem_job', side_effect=process):
            self.hub.action('start')
            wait_for(lambda: not any(task['busy'] for task in self.hub.snapshot()['tasks']))
        tasks = self.hub.snapshot()['tasks']
        self.assertTrue(all(task['state'] == 'completed' for task in tasks), tasks)
        for task in tasks:
            parent = root / {'sar': '', 'orbit': 'orbit', 'dem': 'dem', 'dem-process': 'dem'}[task['kind']]
            self.assertEqual(Path(task['output']), parent)
            self.assertEqual(Path(task['target']).parent, parent)
            self.assertTrue(Path(task['target']).exists())
        self.hub.close()
        self.hub = DownloadHub(self.auth, Path(self.folder.name) / 'state')
        self.assertEqual({task['id']: task['target'] for task in self.hub.snapshot()['tasks']},
                         {task['id']: task['target'] for task in tasks})

    def test_existing_orbit_resume_keeps_original_directory(self):
        from test_auxiliary import ORBIT_URL
        from downloads import Paused
        self.auth.token = 'fixture-token'
        task = self.hub.add([{'url': ORBIT_URL}])['tasks'][0]
        root = Path(self.folder.name) / 'output'
        job = self.hub.files.jobs[task['id']]
        job.update(output=str(root), target=str(root / task['filename']), state='paused')
        part = root / '.sar-parts' / task['id'] / 'payload.part'
        part.parent.mkdir(parents=True)
        part.write_bytes(b'original partial bytes')
        self.hub.files._save()
        self.hub.close()
        self.hub = DownloadHub(self.auth, Path(self.folder.name) / 'state')
        self.hub.configure({'concurrent': 2, 'output': str(root / 'new')})
        report = self.hub.preflight()
        self.assertEqual(report['destinations'][0]['path'], str(root))
        with patch.object(self.hub.files, '_transfer', side_effect=Paused()):
            self.hub.action('resume')
            wait_for(lambda: not self.hub.snapshot()['tasks'][0]['busy'])
        self.assertEqual(self.hub.snapshot()['tasks'][0]['target'], str(root / task['filename']))
        self.assertEqual(part.read_bytes(), b'original partial bytes')

    def test_nisar_full_scene_size_hint_survives_restart(self):
        task = self.hub.add_scenes({'scenes': [{**NISAR, 'size': 1000}]})['tasks'][0]
        self.assertEqual(task['expectedBytes'], 1000)
        self.hub.close()
        self.hub = DownloadHub(self.auth, Path(self.folder.name) / 'state')
        self.assertEqual(self.hub.snapshot()['tasks'][0]['expectedBytes'], 1000)

    def test_mixed_queue_respects_global_limit_and_releases_slot(self):
        from downloads import Paused
        from nisar_subset_engine import Paused as SubsetPaused
        self.auth.token = 'fixture-token'
        self.hub.configure({'concurrent': 1, 'output': str(Path(self.folder.name) / 'output')})
        file_task = self.hub.add([{'url': 'https://datapool.asf.alaska.edu/test/scene.zip'}])['tasks'][0]
        self.hub.add_subset(self.payload())
        entered, release, subset_entered = threading.Event(), threading.Event(), threading.Event()
        def file_transfer(*args):
            entered.set()
            release.wait(5)
            raise Paused()
        def subset_transfer(*args, **kwargs):
            subset_entered.set()
            raise SubsetPaused()
        with patch.object(self.hub.files, '_transfer', side_effect=file_transfer), patch('nisar_subset_jobs.download_subset', side_effect=subset_transfer):
            self.hub.action('start', [file_task['id']])
            self.assertTrue(entered.wait(3))
            self.hub.action('start')
            try:
                self.assertFalse(subset_entered.wait(.2))
                self.assertEqual(self.hub.files.budget.active, 1)
            finally:
                release.set()
            self.assertTrue(subset_entered.wait(3))
            wait_for(lambda: not any(task['busy'] for task in self.hub.snapshot()['tasks']))

    def test_corrupt_subset_queue_does_not_prevent_startup_and_is_backed_up(self):
        self.hub.close()
        path = Path(self.folder.name) / 'state' / 'nisar-subsets' / 'tasks.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken json', encoding='utf-8')
        self.hub = DownloadHub(self.auth, Path(self.folder.name) / 'state')
        self.assertEqual(self.hub.snapshot()['tasks'], [])
        self.assertIn('备份', self.hub.snapshot()['warning'])
        self.assertEqual(next(path.parent.glob('*.bak')).read_text(encoding='utf-8'), '{broken json')
        self.assertEqual(self.hub.add_subset(self.payload())['added'], 1)

    def test_bad_subset_record_preserves_other_tasks(self):
        self.hub.add_subset(self.payload())
        path = self.hub.subsets.path
        self.hub.close()
        saved = json.loads(path.read_text(encoding='utf-8'))
        valid = saved['tasks'][0] if isinstance(saved, dict) else saved[0]
        path.write_text(json.dumps([{'id': 'invalid'}, valid]), encoding='utf-8')
        self.hub = DownloadHub(self.auth, Path(self.folder.name) / 'state')
        self.assertEqual(len(self.hub.snapshot()['tasks']), 1)
        self.assertIn('备份', self.hub.snapshot()['warning'])

    def test_subset_waiting_for_global_slot_can_pause_without_starting(self):
        from downloads import Paused
        self.auth.token = 'fixture-token'
        self.hub.configure({'concurrent': 1, 'output': str(Path(self.folder.name) / 'output')})
        ident = self.hub.add([{'url': 'https://datapool.asf.alaska.edu/test/scene.zip'}])['tasks'][0]['id']
        subset = self.hub.add_subset(self.payload())['ids'][0]
        entered, release = threading.Event(), threading.Event()
        def file_transfer(*args):
            entered.set()
            release.wait(5)
            raise Paused()
        with patch.object(self.hub.files, '_transfer', side_effect=file_transfer), patch('nisar_subset_jobs.download_subset') as download:
            self.hub.action('start', [ident])
            self.assertTrue(entered.wait(3))
            try:
                self.hub.action('start', [subset])
                self.hub.action('pause', [subset])
                wait_for(lambda: not self.hub.snapshot()['tasks'][-1]['busy'])
                self.assertEqual(self.hub.snapshot()['tasks'][-1]['state'], 'paused')
                download.assert_not_called()
            finally:
                release.set()
                self.hub.close()

    def test_burst_orbits_match_full_utc_interval_without_standard_zip_name(self):
        parser = OrbitLinks('POEORB')
        parser.feed(f'<a href="{EOF}">orbit</a>')
        with patch('auxiliary_api.orbit_catalog', return_value=list(parser.records.values())):
            result = self.hub.scene_bundle({'scenes': [SCENE, SCENE], 'includeXML': True,
                                           'auxiliary': {'orbit': True}})
        self.assertEqual(result['added'], 3)
        self.assertEqual({task['kind'] for task in result['tasks']}, {'sar', 'orbit'})
        self.assertTrue(all(task['state'] == 'queued' and not task['busy'] for task in result['tasks']))
        self.assertEqual(self.hub.scene_bundle({'scenes': [SCENE], 'includeXML': True})['added'], 0)

    def test_companion_failure_does_not_leave_half_a_batch(self):
        with patch('download_hub.match_orbits', side_effect=AuxiliaryError('fixture catalog failure')):
            with self.assertRaises(AuxiliaryError):
                self.hub.scene_bundle({'scenes': [SCENE], 'auxiliary': {'orbit': True}})
        self.assertEqual(self.hub.snapshot()['tasks'], [])

    def test_legacy_orbit_flags_use_one_precise_first_match_and_report_missing(self):
        result = {'files': 0, 'assets': [], 'missing': 1}
        with patch('download_hub.match_orbits', return_value=result) as matcher:
            snapshot = self.hub.scene_bundle({'scenes': [SCENE], 'auxiliary': {'precise': True, 'restituted': True}})
        self.assertEqual([call.args[0]['strategy'] for call in matcher.call_args_list], ['auto'])
        self.assertEqual(snapshot['added'], 1)
        self.assertEqual(len(snapshot['warnings']), 1)

    def test_precise_exists_so_no_restituted_file_or_catalog_is_used(self):
        parser = OrbitLinks('POEORB')
        parser.feed(f'<a href="{EOF}">orbit</a>')
        with patch('auxiliary_api.orbit_catalog', return_value=list(parser.records.values())) as catalog:
            result = self.hub.scene_bundle({'scenes': [SCENE], 'auxiliary': {'orbit': True}})
        catalog.assert_called_once_with('POEORB')
        orbits = [task for task in result['tasks'] if task['kind'] == 'orbit']
        self.assertEqual(len(orbits), 1)
        self.assertIn('POEORB', orbits[0]['filename'])

    def test_restituted_is_only_matched_for_scenes_missing_precise_orbits(self):
        parser = OrbitLinks('POEORB')
        parser.feed(f'<a href="{EOF}">orbit</a>')
        precise = list(parser.records.values())[0]
        restored_name = EOF.replace('POEORB', 'RESORB').replace('S1A_', 'S1B_')
        restored = {**precise, 'platform': 'S1B', 'type': 'RESORB', 'filename': restored_name,
                    'url': 'https://s1qc.asf.alaska.edu/aux_resorb/' + restored_name}
        second = {**SCENE, 'id': 'burst-2', 'name': 'burst-2', 'platform': 'S1B'}
        with patch('auxiliary_api.orbit_catalog', side_effect=[[precise], [restored]]):
            result = self.hub.scene_bundle({'scenes': [SCENE, second], 'auxiliary': {'orbit': True}})
        orbits = [task for task in result['tasks'] if task['kind'] == 'orbit']
        self.assertEqual(len(orbits), 2)
        self.assertEqual(sum('POEORB' in task['filename'] for task in orbits), 1)
        self.assertEqual(sum('RESORB' in task['filename'] for task in orbits), 1)

    def test_missing_burst_time_is_rejected_instead_of_guessing_orbit(self):
        with self.assertRaises(AuxiliaryError):
            match_orbits({'scenes': [{'id': 'burst', 'platform': 'S1A', 'start': SCENE['startTime'], 'end': None}]})

    def test_dem_covers_selection_union_and_commits_with_image(self):
        from auxiliary_api import dem_plan
        def preview(payload):
            self.assertEqual(payload['bounds'], [86.6, 43, 87.5, 43.5])
            return {**dem_plan(payload), 'available': 1, 'warnings': []}
        with patch('download_hub.preview_dem', side_effect=preview):
            result = self.hub.scene_bundle({'scenes': [SCENE, {**SCENE, 'bounds': [87, 43.2, 87.5, 43.5]}],
                                          'auxiliary': {'dem': True}})
        self.assertEqual(result['added'], 2)
        self.assertEqual({task['kind'] for task in result['tasks']}, {'sar', 'dem-process'})

    def test_subset_deduplicates_stays_queued_and_restores_paused(self):
        result = self.hub.add_subset(self.payload())
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['tasks'][0]['kind'], 'nisar-subset')
        self.assertEqual(result['tasks'][0]['state'], 'queued')
        self.assertFalse(result['tasks'][0]['busy'])
        self.assertEqual(self.hub.add_subset(self.payload())['added'], 0)
        with self.assertRaises(AuthError):
            self.hub.action('start')
        self.hub.action('pause')
        self.assertEqual(self.hub.snapshot()['tasks'][0]['state'], 'paused')
        self.hub.close()
        self.hub = DownloadHub(self.auth, Path(self.folder.name) / 'state')
        self.assertEqual(self.hub.snapshot()['tasks'][0]['state'], 'paused')
        serialized = (self.hub.runtime / 'nisar-subsets/tasks.json').read_text(encoding='utf-8')
        self.assertNotIn('password', serialized)
        self.assertNotIn('token', serialized)

    def test_simple_subset_names_reject_conflicts_without_partially_adding_scenes(self):
        with self.assertRaises(TransferError):
            self.hub.add_subset({**self.payload(), 'scenes': [{**NISAR, 'product': 'GCOV'}]})
        self.assertEqual(self.hub.snapshot()['tasks'], [])
        self.hub.add_subset(self.payload())
        changed = {**self.payload(), 'polarizations': ['HV'], 'scenes': [{**NISAR, 'id': 'NISAR_L1_OTHER_RSLC'}, NISAR]}
        with self.assertRaisesRegex(TransferError, '_clipped.h5'):
            self.hub.add_subset(changed)
        self.assertEqual(len(self.hub.snapshot()['tasks']), 1)
        self.hub.configure({'concurrent': 2, 'output': str(Path(self.folder.name) / 'other-output')})
        self.assertEqual(self.hub.add_subset(changed)['added'], 2)
        tasks = self.hub.snapshot()['tasks']
        self.assertEqual(len(tasks), 3)
        self.assertEqual(len({task['target'] for task in tasks}), 3)
        self.assertTrue(all(task['filename'].endswith('_clipped.h5') for task in tasks))
        self.assertEqual(self.hub.add_subset(changed)['added'], 0)

    def test_nisar_full_companion_dem_uses_scene_union_and_forces_ellipsoid(self):
        from auxiliary_api import dem_plan
        scenes = [{**NISAR, 'bounds': [86.6, 43, 87, 43.25]},
                  {**NISAR, 'id': 'NISAR_OTHER_RSLC', 'url': NISAR['url'].replace('TEST_RSLC', 'OTHER_RSLC'),
                   'bounds': [87, 43.2, 87.5, 43.5]}]
        def preview(payload):
            self.assertEqual(payload, {'bounds': [86.6, 43, 87.5, 43.5], 'buffer': 5,
                                       'height': 'ellipsoid', 'format': 'GTiff', 'source': 'NISAR'})
            return {**dem_plan(payload), 'available': 1, 'warnings': []}
        with patch('download_hub.preview_dem', side_effect=preview):
            result = self.hub.add_scenes({'scenes': scenes, 'includeDEM': True,
                                          'demOptions': {'height': 'egm2008', 'format': 'tiles'}})
        self.assertEqual(result['added'], 3)
        dem = next(task for task in result['tasks'] if task['kind'] == 'dem-process')
        self.assertEqual(dem['plan']['height'], 'ellipsoid')
        self.assertEqual(dem['plan']['source'], 'NISAR')
        self.assertTrue(all(task['state'] == 'queued' and not task['busy'] for task in result['tasks']))

    def test_alos_family_shares_queue_and_dem_without_sentinel_orbits(self):
        from auxiliary_api import dem_plan
        for dataset in ('ALOS', 'ALOS-2'):
            scene = {'id': dataset, 'name': dataset, 'dataset': dataset, 'product': 'L1.1',
                     'url': f'https://cumulus.asf.earthdatacloud.nasa.gov/L1.1/ALOS2/{dataset}.zip',
                     'filename': dataset + '.zip', 'size': 61239643845, 'bounds': [86.6,43,87,43.25]}
            with patch('download_hub.match_orbits') as orbits, patch('download_hub.preview_dem', side_effect=lambda p: {**dem_plan(p), 'available':1, 'warnings':[]}):
                result = self.hub.scene_bundle({'scenes': [scene], 'auxiliary': {'dem': True}})
            self.assertEqual(result['bundle'], {'imagery':1, 'orbit':0, 'dem':1})
            orbits.assert_not_called()
            self.assertTrue(any(task['kind'] == 'sar' and task['filename'] == dataset + '.zip' for task in result['tasks']))
            with self.assertRaisesRegex(TransferError, '仅适用于 Sentinel-1'):
                self.hub.scene_bundle({'scenes': [scene], 'auxiliary': {'orbit': True}})

    def test_nisar_subset_companion_dem_uses_crop_and_deduplicates_across_scenes(self):
        from auxiliary_api import dem_plan
        def preview(payload):
            self.assertEqual(payload['bounds'], [86.6, 43, 87, 43.25])
            return {**dem_plan(payload), 'available': 1, 'warnings': []}
        with patch('download_hub.preview_dem', side_effect=preview):
            first = self.hub.add_subset({**self.payload(), 'includeDEM': True})
            second = self.hub.add_subset({**self.payload(), 'scenes': [{**NISAR, 'id': 'NISAR_OTHER_RSLC'}], 'includeDEM': True})
        self.assertEqual(first['added'], 2)
        self.assertEqual(second['added'], 1)
        self.assertEqual(sum(task['kind'] == 'dem-process' for task in second['tasks']), 1)
        subset = next(task for task in first['tasks'] if task['kind'] == 'nisar-subset')
        self.assertEqual(subset['filename'], NISAR['id'] + '_clipped.h5')
        dem = next(task for task in first['tasks'] if task['kind'] == 'dem-process')
        self.assertEqual(set(first['ids']), {subset['id'], dem['id']})
        # A shared DEM must also be returned on a later selection so it starts with that selection.
        self.assertIn(dem['id'], second['ids'])

    def test_nisar_dem_failure_leaves_no_subset_or_file_tasks(self):
        with patch('download_hub.preview_dem', side_effect=AuxiliaryError('fixture failure')):
            with self.assertRaises(AuxiliaryError):
                self.hub.add_subset({**self.payload(), 'includeDEM': True})
        self.assertEqual(self.hub.snapshot()['tasks'], [])
        with patch('download_hub.preview_dem') as preview:
            self.hub.add_subset({**self.payload(), 'includeDEM': False})
        preview.assert_not_called()

    def test_legacy_hashed_subset_paths_are_kept_for_resume(self):
        self.hub.add_subset(self.payload())
        job = next(iter(self.hub.subsets.tasks.values()))
        original = str(Path(job['output']).with_name(NISAR['id'] + '_clipped_fixturehash.h5'))
        job['output'] = original
        self.hub.subsets.save()
        self.hub.close()
        self.hub = DownloadHub(self.auth, Path(self.folder.name) / 'state')
        result = self.hub.add_subset(self.payload())
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['tasks'][0]['target'], original)

    def test_anonymous_dem_can_start_while_subset_waits_for_login(self):
        self.hub.add_subset(self.payload())
        tile = 'Copernicus_DSM_COG_10_N43_00_E086_00_DEM'
        self.hub.add([{'url': f'https://copernicus-dem-30m.s3.amazonaws.com/{tile}/{tile}.tif'}])
        with patch.object(self.hub.files, '_schedule'):
            result = self.hub.action('start')
        by_kind = {task['kind']: task for task in result['tasks']}
        self.assertEqual(by_kind['nisar-subset']['state'], 'auth_required')
        self.assertEqual(by_kind['dem']['state'], 'queued')
        self.assertTrue(by_kind['dem']['enabled'])

    def test_subset_pause_resume_never_schedules_duplicate_workers_and_remove_preserves_files(self):
        from nisar_subset_engine import Paused
        self.auth.token, self.auth.username = 'fixture-token', 'fixture'
        ident = self.hub.add_subset(self.payload())['ids'][0]
        entered = threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            kwargs['stop'].wait(5)
            raise Paused()
        with patch('nisar_subset_jobs.download_subset', side_effect=blocked) as download:
            self.hub.action('start', [ident])
            self.assertTrue(entered.wait(3))
            self.hub.action('resume', [ident])
            self.assertEqual(download.call_count, 1)
            self.hub.action('pause', [ident])
            wait_for(lambda: not self.hub.snapshot()['tasks'][0]['busy'])
        target = Path(self.hub.snapshot()['tasks'][0]['target'])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'finished fixture')
        sidecar = Path(str(target) + '.part')
        sidecar.write_bytes(b'partial fixture')
        self.hub.action('remove', [ident])
        self.assertEqual(self.hub.snapshot()['tasks'], [])
        self.assertTrue(target.exists())
        self.assertTrue(sidecar.exists())


if __name__ == '__main__':
    unittest.main()
