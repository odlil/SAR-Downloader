"""Cleanup frees disposable data without damaging results or resumable tasks."""
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
from cache_cleanup import Cleanup
from downloads import DownloadManager, Paused
from earthdata_auth import TransferError


class Auth:
    def status(self):
        return {'loggedIn': False, 'username': '', 'burstReady': False}


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=ROOT/'tests')
        self.root = Path(self.folder.name)
        self.manager = DownloadManager(Auth(), self.root/'state')
        self.manager.configure({'output': str(self.root/'output'), 'concurrent': 1})

    def tearDown(self):
        self.manager.close()
        self.folder.cleanup()

    def file(self, path, data=b'cached bytes'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def dem(self, bounds=None):
        self.manager.add([{'kind':'dem-process','plan':{'bounds':bounds or [86,43,86.01,43.01],
                          'height':'egm2008','format':'GTiff'}}])
        job=list(self.manager.jobs.values())[-1]
        job.update(output=str(self.root/'output'), target=str(self.root/'output'/job['filename']))
        return job

    def test_successful_dem_cleans_work_and_tiles_but_keeps_result_grid_and_queue(self):
        job=self.dem()
        grid=self.file(self.root/'state/dem-cache/geoids/grid.tif')
        tile=self.file(self.root/'state/dem-cache/tiles/tile.tif')
        work=self.root/'output/.sar-dem-parts'/job['id'].split(':')[1]
        self.file(work/'request.json');self.file(work/'source.vrt')
        def finish(manager, record, event):
            self.file(Path(record['target'])/'elevation.tif', b'final raster')
        with patch('dem_jobs.run_dem_job',side_effect=finish):
            self.manager._run(job, threading.Event())
        self.assertEqual(job['state'],'completed')
        self.assertFalse(work.exists());self.assertFalse(tile.exists())
        self.assertTrue(grid.exists());self.assertEqual((Path(job['target'])/'elevation.tif').read_bytes(),b'final raster')
        self.assertTrue((self.root/'state/downloads.json').is_file())

    def test_paused_failed_and_shared_dem_caches_are_protected_until_last_completion(self):
        first=self.dem();second=self.dem([87,43,87.01,43.01])
        tile=self.file(self.root/'state/dem-cache/tiles/tile.tif')
        work=self.root/'output/.sar-dem-parts'/second['id'].split(':')[1]
        self.file(work/'result/elevation.tif')
        for failure in (Paused(), TransferError('fixture failure')):
            with patch('dem_jobs.run_dem_job',side_effect=failure):
                self.manager._run(second,threading.Event())
            self.assertTrue(tile.exists());self.assertTrue(work.exists())
            self.assertTrue(self.manager.clean_cache()['cleanup']['demCachePreserved'])
            self.assertTrue(work.exists())
        first['state']='completed';self.manager._clean_completed_dem(first)
        self.assertTrue(tile.exists())
        second['state']='completed';self.manager._clean_completed_dem(second)
        self.assertFalse(work.exists());self.assertFalse(tile.exists())

    def test_manual_cleanup_preserves_unfinished_downloads_and_all_formal_results(self):
        self.manager.add([{'url':'https://datapool.asf.alaska.edu/scene.zip'}])
        job=next(iter(self.manager.jobs.values()))
        job.update(state='paused', output=str(self.root/'output'))
        partial=self.file(self.root/'output/.sar-parts'/job['id']/'payload.part')
        completed=self.file(self.root/'output/.sar-parts'/('a'*24)/'resume.json', b'123')
        orphan=self.file(self.root/'output/.sar-parts'/('b'*24)/'payload.part', b'4567')
        formal=self.file(self.root/'output/final.zip')
        tile=self.file(self.root/'state/dem-cache/tiles/tile.tif', b'89')
        grid=self.file(self.root/'state/dem-cache/geoids/grid.tif')
        grid_resume=self.file(self.root/'state/dem-cache/geoids/.sar-parts'/('c'*24)/'payload.part')
        grid_meta=self.file(self.root/'state/dem-cache/geoids/.sar-parts'/('d'*24)/'resume.json', b'0')
        report=self.manager.clean_cache()['cleanup']
        self.assertEqual(report['bytesFreed'],10);self.assertEqual(report['filesRemoved'],4)
        for path in (partial,formal,grid,grid_resume):self.assertTrue(path.is_file(),path)
        for path in (completed,orphan,tile,grid_meta):self.assertFalse(path.exists(),path)
        self.assertEqual(len(self.manager.jobs),1)
        self.manager.running['fixture']=object()
        with self.assertRaises(TransferError):self.manager.clean_cache()
        self.manager.running.clear()

    def test_deletion_refuses_escape_and_junction_without_touching_outside(self):
        cache=self.root/'cache';cache.mkdir()
        outside=self.file(self.root/'outside/keep.txt')
        cleanup=Cleanup();cleanup.remove(outside,cache)
        self.assertTrue(outside.exists());self.assertEqual(cleanup.skipped,1)
        link=cache/'linked'
        if sys.platform=='win32':
            result=subprocess.run([os.environ.get('COMSPEC','cmd.exe'),'/d','/c','mklink','/J',str(link),str(outside.parent)],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
        else:
            link.symlink_to(outside.parent, target_is_directory=True)
        try:
            inside=self.file(cache/'ordinary.txt')
            cleanup.remove(cache,self.root)
            self.assertTrue(inside.exists())  # Preflight refuses the entire tree.
            self.assertTrue(outside.exists())
            cleanup.tiles(link)
            self.assertTrue(outside.exists())
        finally:
            if sys.platform=='win32':link.rmdir()
            else:link.unlink()


if __name__=='__main__':unittest.main()
