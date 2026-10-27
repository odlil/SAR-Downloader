"""Destructive cleanup is exercised only inside disposable fixture directories."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cache_cleanup import Cleanup, SUBSET_RESUME_SUFFIXES
from download_hub import DownloadHub
from earthdata_auth import EarthdataAuth, TransferError


class UnfinishedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.root = Path(self.folder.name)
        self.hub = DownloadHub(EarthdataAuth(), self.root / 'state')
        self.hub.configure({'output': str(self.root / 'output'), 'concurrent': 2})
        self.cleaner = self.hub.unfinished

    def tearDown(self):
        self.hub.close()
        self.folder.cleanup()

    def file(self, path, data=b'fixture'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def ordinary(self, name='scene'):
        self.hub.add([{'url': f'https://datapool.asf.alaska.edu/{name}.zip'}])
        job = list(self.hub.files.jobs.values())[-1]
        job.update(state='paused', output=str(self.root / 'output'),
                   target=str(self.root / 'output' / job['filename']), completed=7, total=100)
        part = self.file(self.root / 'output/.sar-parts' / job['id'] / 'payload.part')
        self.file(part.parent / 'resume.json', b'{}')
        return job, part

    def dem(self, offset=0):
        self.hub.add([{'kind': 'dem-process', 'plan': {'bounds': [86 + offset, 43, 86.01 + offset, 43.01],
                       'height': 'ellipsoid', 'format': 'GTiff'}}])
        job = list(self.hub.files.jobs.values())[-1]
        output = self.root / 'output/dem'
        job.update(state='paused', output=str(output), target=str(output / job['filename']))
        part = self.file(output / '.sar-dem-parts' / job['id'][4:] / 'result/elevation.tif')
        return job, part

    def subset(self):
        scene = {'id': 'NISAR_L1_FIXTURE_RSLC', 'dataset': 'NISAR', 'product': 'RSLC',
                 'url': 'https://datapool.asf.alaska.edu/NISAR_L1_FIXTURE_RSLC.h5'}
        self.hub.add_subset({'scenes': [scene], 'corners': [[86, 44], [87, 44], [87, 43], [86, 43]],
                             'frequencies': ['A'], 'polarizations': ['HH'], 'connections': 2})
        task = next(iter(self.hub.subsets.tasks.values()))
        task.update(status='paused', downloaded=7, network_bytes=90)
        # Older releases used hashed filenames; cleanup must use the registered output.
        task['output'] = str(self.root / 'output/NISAR_FIXTURE_clipped_abcdef.h5')
        paths = [self.file(Path(task['output'] + suffix)) for suffix in SUBSET_RESUME_SUFFIXES]
        return task, paths

    def clear(self, ids, **options):
        preview = self.cleaner.preview({})
        return self.cleaner.clear({'token': preview['token'], 'ids': ids, **options})

    def test_preview_is_read_only_and_cleanup_resets_only_selected_task(self):
        first, part = self.ordinary()
        second, other = self.ordinary('other')
        final = self.file(Path(first['target']), b'formal imagery')
        orbit = self.file(self.root / 'output/orbit/final.EOF')
        orphan = self.file(part.parent.parent / ('a' * 24) / 'payload.part')
        preview = self.cleaner.preview({})
        row = next(row for row in preview['tasks'] if row['id'] == first['id'])
        self.assertEqual((row['bytes'], row['files']), (9, 2))
        self.assertTrue(part.exists())
        self.assertEqual(first['completed'], 7)
        with patch.object(self.hub.files, '_schedule') as schedule:
            result = self.cleaner.clear({'token': preview['token'], 'ids': [first['id']]})
        schedule.assert_not_called()
        self.assertEqual(result['cleanup']['bytesFreed'], 9)
        self.assertFalse(part.parent.exists())
        for path in (other, final, orbit, orphan): self.assertTrue(path.exists(), path)
        self.assertEqual(second['completed'], 7)
        self.assertEqual(first['state'], 'paused')
        self.assertFalse(first['enabled'])
        self.assertEqual(first['completed'], 0)
        self.hub.close()
        self.hub = DownloadHub(EarthdataAuth(), self.root / 'state')
        restored = self.hub.files.jobs[first['id']]
        self.assertEqual(restored['state'], 'paused')
        self.assertEqual(restored['completed'], 0)

    def test_subset_sidecars_removed_but_final_h5_and_other_sidecar_remain(self):
        task, paths = self.subset()
        final = self.file(Path(task['output']), b'formal HDF5')
        other = self.file(final.with_name('other.h5.part'))
        result = self.clear(['subset-' + task['id']])
        self.assertEqual(result['cleanup']['filesRemoved'], len(SUBSET_RESUME_SUFFIXES))
        self.assertTrue(final.exists()); self.assertTrue(other.exists())
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(task['status'], 'paused')
        self.assertEqual(task['downloaded'], 0); self.assertEqual(task['network_bytes'], 0)

    def test_dem_shared_cache_requires_all_unfinished_dem_tasks_selected(self):
        first, first_part = self.dem()
        second, second_part = self.dem(1)
        tile = self.file(self.root / 'state/dem-cache/tiles/tile.tif')
        partial_grid = self.file(self.root / 'state/dem-cache/geoids/.sar-parts' / ('b' * 24) / 'payload.part')
        grid = self.file(self.root / 'state/dem-cache/geoids/egm2008.tif')
        final = self.file(Path(first['target']) / 'elevation.tif')
        result = self.clear([first['id']])
        self.assertTrue(result['cleanup']['sharedPreserved'])
        self.assertFalse(first_part.exists()); self.assertTrue(second_part.exists())
        self.assertTrue(tile.exists()); self.assertTrue(partial_grid.exists())
        result = self.clear([first['id'], second['id']])
        self.assertFalse(result['cleanup']['sharedPreserved'])
        self.assertFalse(second_part.exists()); self.assertFalse(tile.exists()); self.assertFalse(partial_grid.exists())
        self.assertTrue(grid.exists()); self.assertTrue(final.exists())

    def test_bound_old_output_is_used_after_changing_download_directory(self):
        task, part = self.ordinary()
        self.hub.configure({'output': str(self.root / 'new-output'), 'concurrent': 2})
        other = self.file(self.root / 'new-output/.sar-parts' / task['id'] / 'payload.part')
        self.clear([task['id']])
        self.assertFalse(part.exists()); self.assertTrue(other.exists())

    def test_remove_records_only_after_successful_cleanup(self):
        task, part = self.ordinary()
        final = self.file(Path(task['target']))
        result = self.clear([task['id']], removeTasks=True)
        self.assertEqual(result['cleanup']['removedCount'], 1)
        self.assertNotIn(task['id'], self.hub.files.jobs)
        self.assertFalse(part.exists()); self.assertTrue(final.exists())

    def test_busy_even_while_paused_and_subset_queued_workers_block_cleanup(self):
        task, part = self.ordinary()
        preview = self.cleaner.preview({})
        self.hub.files.running[task['id']] = object()
        try:
            with self.assertRaises(TransferError): self.cleaner.preview({})
            with self.assertRaises(TransferError): self.cleaner.clear({'token': preview['token'], 'ids': [task['id']]})
        finally: self.hub.files.running.clear()
        subset, _ = self.subset()
        self.hub.subsets.stops[subset['id']] = threading.Event()
        try:
            with self.assertRaises(TransferError): self.cleaner.preview({})
            with self.assertRaises(TransferError): self.hub.clean_cache()
        finally: self.hub.subsets.stops.clear()
        self.assertTrue(part.exists())

    def test_preview_tokens_are_required_single_use_and_expire(self):
        task, part = self.ordinary()
        for token in (None, 'unknown'):
            with self.assertRaises(TransferError): self.cleaner.clear({'token': token, 'ids': [task['id']]})
        preview = self.cleaner.preview({})
        self.cleaner.previews[preview['token']]['expires'] = 0
        with self.assertRaises(TransferError): self.cleaner.clear({'token': preview['token'], 'ids': [task['id']]})
        preview = self.cleaner.preview({})
        payload = {'token': preview['token'], 'ids': [task['id']]}
        self.cleaner.clear(payload)
        with self.assertRaises(TransferError): self.cleaner.clear(payload)

    def test_invalid_selection_and_path_injection_never_delete_files(self):
        task, part = self.ordinary()
        for options in ({'path': str(part)}, {'ids': []}, {'ids': ['unknown']}, {'ids': [None]}):
            with self.assertRaises(TransferError): self.cleaner.preview(options)
        for options in ({'ids': []}, {'ids': ['unknown']}, {'ids': [task['id']], 'path': str(part)},
                        {'ids': [task['id']], 'removeTasks': 'yes'}):
            preview = self.cleaner.preview({})
            with self.assertRaises(TransferError): self.cleaner.clear({'token': preview['token'], **options})
        task['state'] = 'completed'
        self.assertEqual(self.cleaner.preview({})['tasks'], [])
        with self.assertRaises(TransferError): self.cleaner.preview({'ids': [task['id']]})
        self.assertTrue(part.exists())

    def test_file_or_task_change_after_preview_requires_new_preview(self):
        task, part = self.ordinary()
        for change in (lambda: part.write_bytes(b'changed fixture'), lambda: task.update(total=123)):
            preview = self.cleaner.preview({})
            change()
            with self.assertRaisesRegex(TransferError, '发生变化'):
                self.cleaner.clear({'token': preview['token'], 'ids': [task['id']]})
            self.assertTrue(part.exists())

    def test_new_dem_task_or_shared_file_change_invalidates_shared_cleanup(self):
        task, part = self.dem()
        tile = self.file(self.root / 'state/dem-cache/tiles/tile.tif')
        preview = self.cleaner.preview({})
        tile.write_bytes(b'changed shared cache')
        with self.assertRaisesRegex(TransferError, '共享 DEM'):
            self.cleaner.clear({'token': preview['token'], 'ids': [task['id']]})
        self.assertTrue(part.exists()); self.assertTrue(tile.exists())

    def test_registered_formal_output_inside_cache_blocks_deletion(self):
        task, part = self.ordinary()
        other, _ = self.ordinary('other')
        other['target'] = str(part)
        preview = self.cleaner.preview({})
        row = next(row for row in preview['tasks'] if row['id'] == task['id'])
        self.assertTrue(row['error'])
        with self.assertRaises(TransferError): self.cleaner.clear({'token': preview['token'], 'ids': [task['id']]})
        self.assertTrue(part.exists())

    def test_junction_or_symlink_in_cache_blocks_entire_task(self):
        task, part = self.ordinary()
        outside = self.file(self.root / 'outside/keep.txt')
        link = part.parent / 'linked'
        if sys.platform == 'win32':
            result = subprocess.run([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', 'mklink', '/J',
                                     str(link), str(outside.parent)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else: link.symlink_to(outside.parent, target_is_directory=True)
        try:
            preview = self.cleaner.preview({})
            self.assertTrue(preview['tasks'][0]['error'])
            with self.assertRaises(TransferError): self.cleaner.clear({'token': preview['token'], 'ids': [task['id']]})
            self.assertTrue(part.exists()); self.assertTrue(outside.exists())
        finally:
            if sys.platform == 'win32': link.rmdir()
            else: link.unlink()

    def test_formal_overlap_guard_normalizes_parent_segments(self):
        task, part = self.ordinary()
        other, _ = self.ordinary('other')
        (self.root / 'output/nested').mkdir()
        task['output'] = str(self.root / 'output/nested/..')
        other['target'] = str(part)
        self.assertTrue(next(row for row in self.cleaner.preview({})['tasks'] if row['id'] == task['id'])['error'])
        self.assertTrue(part.exists())

    def test_subset_record_removed_without_touching_final_and_other_tasks(self):
        task, paths = self.subset()
        final = self.file(Path(task['output']))
        other, part = self.ordinary()
        result = self.clear(['subset-' + task['id']], removeTasks=True)
        self.assertEqual(result['cleanup']['removedCount'], 1)
        self.assertNotIn(task['id'], self.hub.subsets.tasks)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(part.exists()); self.assertTrue(final.exists())

    def test_shared_deletion_failure_is_reported_as_preserved(self):
        task, part = self.dem()
        tile = self.file(self.root / 'state/dem-cache/tiles/tile.tif')
        original = Path.unlink
        def fail(path, *args, **kwargs):
            if path == tile: raise PermissionError('fixture shared file in use')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail):
            result = self.clear([task['id']])
        self.assertTrue(result['cleanup']['sharedPreserved'])
        self.assertTrue(result['cleanup']['skipped'])
        self.assertTrue(tile.exists()); self.assertFalse(part.exists())

    def test_save_failure_prevents_any_file_deletion(self):
        task, part = self.ordinary()
        preview = self.cleaner.preview({})
        with patch.object(self.hub.files, '_save', side_effect=OSError('fixture')):
            with self.assertRaisesRegex(TransferError, '未删除文件'):
                self.cleaner.clear({'token': preview['token'], 'ids': [task['id']], 'removeTasks': True})
        self.assertTrue(part.exists()); self.assertIn(task['id'], self.hub.files.jobs)

    def test_file_in_use_keeps_task_record_and_reports_partial_failure(self):
        task, part = self.ordinary()
        original = Path.unlink
        def fail(path, *args, **kwargs):
            if path == part: raise PermissionError('fixture in use')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail):
            result = self.clear([task['id']], removeTasks=True)
        self.assertTrue(part.exists()); self.assertIn(task['id'], self.hub.files.jobs)
        self.assertEqual(result['cleanup']['removedCount'], 0)
        self.assertEqual(result['cleanup']['failedIds'], [task['id']])
        self.assertEqual(task['state'], 'paused'); self.assertEqual(task['completed'], 0)


if __name__ == '__main__': unittest.main()
