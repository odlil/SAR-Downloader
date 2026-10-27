"""Fault injection around queue ownership, persistence and worker finalization."""
from copy import deepcopy
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from download_hub import DownloadHub
from earthdata_auth import AuthError, EarthdataAuth, TransferError
from nisar_subset_engine import Paused
from dem_jobs import run_dem_job
from downloads import Paused as FilePaused
from preflight import selected_tasks
from unfinished_cleanup import UnfinishedCleanup

URL = 'https://datapool.asf.alaska.edu/test/fixture.zip'
SCENE = {'id': 'NISAR_L1_FIXTURE_RSLC', 'dataset': 'NISAR', 'product': 'RSLC',
         'url': 'https://datapool.asf.alaska.edu/test/NISAR_L1_FIXTURE_RSLC.h5'}


class QueueResilienceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.root = Path(self.folder.name)
        self.auth = EarthdataAuth()
        self.auth.token = 'fixture-only'
        self.hub = DownloadHub(self.auth, self.root / 'state')
        self.files = self.hub.files
        self.hub.configure({'output': str(self.root / 'output'), 'concurrent': 2})

    def tearDown(self):
        self.hub.close()
        self.folder.cleanup()

    def file(self):
        return self.hub.add([{'url': URL}])['tasks'][0]['id']

    def payload(self):
        return {'scenes': [dict(SCENE)], 'corners': [[86, 44], [87, 44], [87, 43], [86, 43]],
                'frequencies': ['A'], 'polarizations': ['HH']}

    def subset(self):
        identifier = self.hub.add_subset(self.payload())['ids'][0].removeprefix('subset-')
        return self.hub.subsets, identifier

    def test_failed_file_add_does_not_leave_unpersisted_tasks(self):
        with patch.object(self.files, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.file()
        self.assertEqual(self.hub.snapshot()['tasks'], [])

    def test_duplicate_file_retains_first_title(self):
        result = self.hub.add([{'url': URL, 'title': 'first'}, {'url': URL, 'title': 'second'}])
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['tasks'][0]['title'], 'first')

    def test_failed_settings_save_preserves_settings_and_budget(self):
        before = self.files.snapshot()['settings']
        with patch.object(self.files, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.hub.configure({'output': str(self.root / 'other'), 'concurrent': 1})
        self.assertEqual(self.files.settings, before)
        self.assertEqual(self.files.budget.limit, before['concurrent'])

    def test_failed_start_cannot_be_enabled_by_later_configuration(self):
        identifier = self.file()
        with patch.object(self.files, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.hub.action('start')
        self.assertFalse(self.files.jobs[identifier].get('enabled'))
        with patch.object(self.files, '_transfer') as transfer:
            self.hub.configure(dict(self.files.settings))
            self.assertEqual(self.files.running, {})
            transfer.assert_not_called()

    def test_failed_remove_preserves_record_in_memory_and_on_disk(self):
        identifier = self.file()
        with patch.object(self.files, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.hub.action('remove', [identifier])
        self.assertIn(identifier, self.files.jobs)
        self.assertEqual(json.loads((self.files.runtime / 'downloads.json').read_text())['jobs'][0]['id'], identifier)

    def test_pause_remains_effective_even_when_its_save_fails(self):
        identifier = self.file()
        stop = self.files.cancels[identifier] = threading.Event()
        with patch.object(self.files, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.hub.action('pause')
        self.assertTrue(stop.is_set())
        self.assertEqual(self.files.jobs[identifier]['state'], 'paused')

    def test_thread_creation_failure_does_not_leave_busy_file(self):
        identifier = self.file()
        with patch('downloads.threading.Thread.start', side_effect=RuntimeError('thread limit')):
            with self.assertRaises(RuntimeError):
                self.hub.action('start')
        self.assertEqual(self.files.running, {})
        self.assertEqual(self.files.cancels, {})
        self.assertEqual(self.files.jobs[identifier]['state'], 'error')

    def test_session_close_failure_still_releases_slot_and_worker(self):
        identifier = self.file()
        job = self.files.jobs[identifier]
        job.update(output=str(self.root), target=str(self.root / 'fixture.zip'))
        self.files.running[identifier] = threading.current_thread()
        stop = self.files.cancels[identifier] = threading.Event()
        session = Mock()
        session.close.side_effect = OSError('fixture close failure')
        with patch.object(self.auth, 'session', return_value=session), patch.object(self.files, '_transfer'):
            with self.assertRaises(OSError):
                self.files._run(job, stop)
        self.assertEqual(self.files.running, {})
        self.assertEqual(self.files.cancels, {})
        self.assertEqual(self.files.budget.active, 0)

    def test_failed_subset_create_does_not_leave_unpersisted_tasks(self):
        jobs = self.hub._subsets()
        with patch.object(jobs, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.hub.add_subset(self.payload())
        self.assertEqual(jobs.tasks, {})

    def test_subset_owns_its_configuration(self):
        payload = self.payload()
        identifier = self.hub.add_subset(payload)['ids'][0].removeprefix('subset-')
        expected = deepcopy(self.hub.subsets.tasks[identifier]['config'])
        payload['corners'][0][0] = 1
        payload['polarizations'].append('VV')
        self.assertEqual(self.hub.subsets.tasks[identifier]['config'], expected)

    def test_failed_subset_start_restores_state_and_clears_busy_markers(self):
        jobs, identifier = self.subset()
        before = deepcopy(jobs.tasks[identifier])
        with patch.object(jobs, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.hub.action('start')
        self.assertEqual(jobs.stops, {})
        self.assertEqual(jobs.tasks[identifier], before)

    def test_subset_initial_worker_save_failure_runs_finalizer(self):
        jobs, identifier = self.subset()
        stop = jobs.stops[identifier] = threading.Event()
        with patch.object(jobs, 'save', side_effect=OSError('disk full')), patch('nisar_subset_jobs.download_subset') as download:
            jobs.run(identifier, stop)
        download.assert_not_called()
        self.assertEqual(jobs.stops, {})
        self.assertEqual(jobs.budget.active, 0)
        self.assertEqual(jobs.tasks[identifier]['status'], 'error')
        self.assertIn('保存失败', self.hub.snapshot()['warning'])

    def test_subset_submit_failure_clears_unsubmitted_busy_markers(self):
        jobs, identifier = self.subset()
        with patch.object(jobs.pool, 'submit', side_effect=RuntimeError('executor unavailable')):
            with self.assertRaises(RuntimeError):
                self.hub.action('start')
        self.assertEqual(jobs.stops, {})
        self.assertEqual(jobs.tasks[identifier]['status'], 'error')

    def test_subset_auth_failure_uses_auth_required_state(self):
        jobs, identifier = self.subset()
        stop = jobs.stops[identifier] = threading.Event()
        with patch('nisar_subset_jobs.download_subset', side_effect=AuthError('session expired')):
            jobs.run(identifier, stop)
        self.assertEqual(jobs.tasks[identifier]['status'], 'auth_required')
        self.assertEqual(jobs.stops, {})

    def test_late_subset_progress_cannot_overwrite_pause_or_speed(self):
        jobs, identifier = self.subset()
        stop = jobs.stops[identifier] = threading.Event()
        def download(*args, **kwargs):
            jobs.pause(identifier)
            kwargs['progress'](status='running', phase='late packet', network_speed=123, speed=123)
            self.assertEqual(jobs.tasks[identifier]['status'], 'pausing')
            self.assertEqual(jobs.tasks[identifier]['phase'], '正在保存进度')
            self.assertEqual(jobs.tasks[identifier]['network_speed'], 0)
            raise Paused()
        with patch('nisar_subset_jobs.download_subset', side_effect=download):
            jobs.run(identifier, stop)
        self.assertEqual(jobs.tasks[identifier]['status'], 'paused')

    def test_terminal_subset_has_zero_network_speed(self):
        jobs, identifier = self.subset()
        stop = jobs.stops[identifier] = threading.Event()
        def download(*args, **kwargs):
            kwargs['progress'](network_speed=123)
            raise RuntimeError('fixture source failure')
        with patch('nisar_subset_jobs.download_subset', side_effect=download):
            jobs.run(identifier, stop)
        self.assertEqual(self.hub.snapshot()['tasks'][0]['speed'], 0)

    def test_invalid_settings_types_raise_domain_error(self):
        for settings in (None, [], 'invalid'):
            with self.subTest(settings=settings), self.assertRaises(TransferError):
                self.hub.configure(settings)

    def test_invalid_action_type_raises_domain_error(self):
        for action in ([], {}, 1):
            with self.subTest(action=action), self.assertRaises(TransferError):
                self.hub.action(action)

    def test_malformed_subset_config_does_not_add_tasks(self):
        for change in ({'min_height': True}, {'min_height': 500, 'max_height': 100},
                       {'frequencies': 'A'}, {'polarizations': 'HH'}):
            with self.subTest(change=change), self.assertRaises(TransferError):
                self.hub.add_subset({**self.payload(), **change})
        self.assertEqual(self.hub.snapshot()['tasks'], [])

    def test_corrupt_saved_subset_is_backed_up_without_losing_valid_record(self):
        jobs, identifier = self.subset()
        valid = deepcopy(jobs.tasks[identifier])
        self.hub.close()
        for change in ('url', 'height', 'progress'):
            with self.subTest(change=change):
                bad = deepcopy(valid)
                bad['id'] = 'a' * 16
                if change == 'url':
                    bad['scene']['url'] = 'https://untrusted.example/data.h5'
                elif change == 'height':
                    bad['config'].update(min_height=500, max_height=100)
                else:
                    bad['network_speed'] = -1
                jobs.path.write_text(json.dumps([bad, valid]), encoding='utf-8')
                restored = DownloadHub(self.auth, self.root / 'state')
                try:
                    self.assertEqual([task['id'] for task in restored.snapshot()['tasks']], ['subset-' + identifier])
                    self.assertIn('备份', restored.snapshot()['warning'])
                finally:
                    restored.close()

    def dem_job(self):
        self.hub.add([{'kind': 'dem-process', 'plan': {'bounds': [86, 43, 86.01, 43.01],
                                                     'height': 'egm2008', 'format': 'GTiff'}}])
        job = next(iter(self.files.jobs.values()))
        job.update(output=str(self.root), target=str(self.root / 'dem-result'))
        return job

    def check_dem_stops_child(self, stop, *, start_error=False, source='import time; time.sleep(30)'):
        processes = []
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process
        with patch('dem_jobs.engine_command', return_value=[sys.executable, '-u', '-B', '-c', source]), \
             patch('dem_jobs.subprocess.Popen', side_effect=launch):
            if start_error:
                with patch('dem_jobs.threading.Thread.start', side_effect=RuntimeError('thread limit')):
                    with self.assertRaises(RuntimeError):
                        run_dem_job(self.files, self.dem_job(), stop)
            else:
                with self.assertRaises(FilePaused if stop.is_set() else TransferError):
                    run_dem_job(self.files, self.dem_job(), stop)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)

    def test_dem_reader_start_failure_still_terminates_child(self):
        self.check_dem_stops_child(threading.Event(), start_error=True)

    def test_dem_pause_terminates_child_and_closes_pipe(self):
        stop = threading.Event()
        stop.set()
        self.check_dem_stops_child(stop)

    def test_dem_abnormal_exit_closes_child_resources(self):
        self.check_dem_stops_child(threading.Event(), source='import sys; sys.exit(7)')

    def test_full_mixed_queue_selection_uses_one_capacity_for_actions_and_cleanup(self):
        ids = [str(index) for index in range(2000)]
        rows = {key: {'id': key, 'error': ''} for key in ids}
        tasks = [{'id': key, 'state': 'queued'} for key in ids]
        self.assertEqual(len(selected_tasks(tasks, 'start', ids)), 2000)
        self.assertEqual(UnfinishedCleanup.select(ids, rows), ids)
        with self.assertRaises(TransferError):
            selected_tasks(tasks, 'start', [*ids, 'overflow'])

    def test_restart_never_restores_a_live_subset_speed(self):
        jobs, identifier = self.subset()
        jobs.tasks[identifier].update(status='paused', network_speed=123)
        jobs.save()
        self.hub.close()
        self.hub = DownloadHub(self.auth, self.root / 'state')
        self.files = self.hub.files
        self.assertEqual(self.hub.snapshot()['tasks'][0]['speed'], 0)


if __name__ == '__main__':
    unittest.main()
