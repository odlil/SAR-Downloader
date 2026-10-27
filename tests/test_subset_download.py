from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import h5py
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nisar_subset_engine import (BASE, Paused, RangeSource, download_subset, validate_corners,
                  radar_window, completion_info)

TEST_ROOT=Path(__file__).resolve().parent/"_output"
assert TEST_ROOT.resolve().is_relative_to(Path(__file__).resolve().parents[1])
TEST_ROOT.mkdir(exist_ok=True)

def saved_manifest(output):
    db=sqlite3.connect(str(output)+".progress.sqlite")
    try:
        manifest=json.loads(db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
        manifest["done"]=dict(db.execute("SELECT key,sha256 FROM blocks"))
    finally:db.close()
    return manifest


def fixture(path):
    rng = np.random.default_rng(42)
    with h5py.File(path, "w") as f:
        ident = f.create_group("science/LSAR/identification")
        ident["granuleId"] = np.bytes_("NISAR_L1_TEST_RSLC")
        ident["boundingPolygon"] = np.bytes_("POLYGON((118 35,119 35,119 36,118 36,118 35))")
        ident["zeroDopplerStartTime"] = np.bytes_("2026-01-01T00:00:00Z")
        ident["zeroDopplerEndTime"] = np.bytes_("2026-01-01T00:17:03Z")
        ident["listOfFrequencies"] = np.array([b"A", b"B"])
        metadata = f.create_group(BASE+"/metadata")
        metadata["orbit/time"] = np.arange(10, dtype=float)
        metadata["orbit/position"] = np.ones((10,3))
        metadata["RFI/frequencyA/HH/raw/hitCount"] = np.zeros((4,3,2),dtype="f4")
        grid = metadata.create_group("geolocationGrid")
        grid["height"] = [-500., 1000., 9000.]
        grid["zeroDopplerTime"] = np.linspace(0, 1023, 9)
        grid["slantRange"] = np.linspace(800000, 810230, 9)
        x, y = np.meshgrid(118+np.linspace(0,.1023,9), 35+np.linspace(0,.1023,9))
        grid["coordinateX"] = np.stack([x+.0001*h for h in [0,1,2]])
        grid["coordinateY"] = np.stack([y,y,y])
        swath = f.create_group(BASE+"/swaths")
        az=swath.create_dataset("zeroDopplerTime", data=np.arange(1024,dtype=float))
        az.attrs["units"] = np.bytes_("seconds since 2026-01-01T00:00:00Z")
        az.make_scale("zeroDopplerTime")
        for freq, width in (("A",1024),("B",256)):
            group=swath.create_group("frequency"+freq)
            rg=group.create_dataset("slantRange", data=np.linspace(800000,810230,width))
            rg.make_scale("slantRange")
            group["listOfPolarizations"]=np.array([b"HH",b"HV"])
            valid=np.tile([0,width],(1024,1)).astype("i4")
            valid[400:402]=[-1,-1]
            if freq=="B":valid=valid.astype("u4")
            group["validSamplesSubSwath1"]=valid
            group.create_dataset("inputDataExceptionMask",data=rng.integers(0,8,size=(1024,width),dtype="u1"),chunks=(16,16),compression="gzip")
            for pol in ("HH","HV"):
                array=(rng.normal(size=(1024,width))+1j*rng.normal(size=(1024,width))).astype("complex64")
                ds=group.create_dataset(pol,data=array,chunks=(16,16),compression="gzip",shuffle=True)
                ds.dims[0].attach_scale(az)
                ds.dims[1].attach_scale(rg)
        # Native complex-float16 compound type, retained through low-level raw-block copying.
        group=swath["frequencyA"]
        native=np.zeros((1024,1024),dtype=[("r","f2"),("i","f2")])
        native["r"]=rng.normal(size=native.shape)
        native["i"]=rng.normal(size=native.shape)
        group.create_dataset("VV",data=native,chunks=(16,16),compression="gzip",shuffle=True)


class RangeHandler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        if self.path.endswith("/expired"):
            self.send_response(403);self.end_headers();return
        match=re.fullmatch(r"bytes=(\d+)-(\d+)",self.headers.get("Range",""))
        data=self.server.data
        if not match:
            self.send_response(400);self.end_headers();return
        start,end=map(int,match.groups())
        self.server.requests.append((start,end))
        if self.server.reject:
            self.send_response(200);self.send_header("Content-Length","0");self.end_headers();return
        content=data[start:end+1]
        self.send_response(206)
        self.send_header("Content-Range",f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length",str(len(content)))
        self.send_header("ETag",self.server.etag)
        self.end_headers()
        try:self.wfile.write(content)
        except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError):pass


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(dir=TEST_ROOT)
        cls.root=Path(cls.temp.name)
        cls.source=cls.root/"source.h5"
        fixture(cls.source)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),RangeHandler)
        cls.server.data=cls.source.read_bytes()
        cls.server.etag='"fixture-v1"'
        cls.server.reject=False
        cls.server.requests=[]
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url=f"http://127.0.0.1:{cls.server.server_port}/source.h5"
        cls.corners=[[118.039,35.041],[118.044,35.042],[118.045,35.037],[118.040,35.036]]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()

    def test_range_rejects_200_without_writing(self):
        self.server.reject=True
        try:
            with self.assertRaisesRegex(ValueError,"整景"):
                RangeSource(self.url).probe()
        finally:self.server.reject=False

    def test_expired_signed_url_refreshes_from_stable_url(self):
        source=RangeSource(self.url)
        source.probe()
        source.resolved_url=self.url+"/expired"
        self.assertEqual(source.read(20,40),self.server.data[20:40])
        self.assertEqual(source.resolved_url,self.url)

    def test_finished_download_keeps_only_hdf5_and_leaves_other_files(self):
        output=self.root/'clean_clipped.h5'
        unrelated=self.root/'unrelated.h5.progress.json'
        unrelated.write_text('keep',encoding='utf-8')
        download_subset(self.url,self.corners,output,connections=2)
        self.assertEqual(list(self.root.glob(output.name+'*')),[output])
        self.assertTrue(completion_info(output))
        self.assertEqual(unrelated.read_text(encoding='utf-8'),'keep')
        self.assert_output(output)

    def test_failed_final_verification_preserves_resume_records(self):
        output=self.root/'verify-failure_clipped.h5'
        with patch('nisar_subset_engine.verify_and_stamp',side_effect=RuntimeError('校验失败')):
            with self.assertRaisesRegex(RuntimeError,'校验失败'):
                download_subset(self.url,self.corners,output,connections=2)
        self.assertFalse(output.exists())
        for suffix in ('.part','.progress.sqlite'):
            self.assertTrue(Path(str(output)+suffix).exists())
        self.assertFalse(Path(str(output)+'.progress.json').exists())
        before=len(self.server.requests)
        download_subset(self.url,self.corners,output,connections=2)
        self.assertEqual(len(self.server.requests)-before,1)
        self.assertEqual(list(self.root.glob(output.name+'*')),[output])

    def test_failed_cleanup_can_retry_without_redownloading(self):
        output=self.root/'cleanup-retry_clipped.h5'
        with patch('nisar_subset_engine.cleanup_download_cache',side_effect=RuntimeError('清理未完成')):
            with self.assertRaisesRegex(RuntimeError,'清理未完成'):
                download_subset(self.url,self.corners,output,connections=2)
        self.assertTrue(completion_info(output))
        self.assertTrue(Path(str(output)+'.progress.sqlite').exists())
        before=len(self.server.requests)
        download_subset(self.url,self.corners,output,connections=2)
        self.assertEqual(len(self.server.requests)-before,1)
        self.assertEqual(list(self.root.glob(output.name+'*')),[output])

    def test_same_filename_different_bounds_does_not_overwrite(self):
        output=self.root/'collision_clipped.h5'
        download_subset(self.url,self.corners,output,connections=2)
        previous=hashlib.sha256(output.read_bytes()).hexdigest()
        shifted=[[x+.0001,y] for x,y in self.corners]
        with self.assertRaisesRegex(ValueError,'其他裁剪范围'):
            download_subset(self.url,shifted,output,connections=2)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(),previous)

    def test_crash_after_rename_then_resume_cleans_records(self):
        output=self.root/'rename-crash_clipped.h5'
        child='''import json,os,sys,nisar_subset_engine
def progress(**value):
    if value.get("phase")=="清理下载记录":os._exit(26)
nisar_subset_engine.download_subset(sys.argv[1],json.loads(sys.argv[2]),sys.argv[3],connections=2,progress=progress)
'''
        result=subprocess.run([sys.executable,'-B','-c',child,self.url,json.dumps(self.corners),str(output)],
                              cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=45)
        self.assertEqual(result.returncode,26,result.stderr.decode('utf-8',errors='replace'))
        self.assertTrue(completion_info(output))
        before=len(self.server.requests)
        download_subset(self.url,self.corners,output)
        self.assertEqual(len(self.server.requests)-before,1)
        self.assertEqual(list(self.root.glob(output.name+'*')),[output])

    def test_metadata_and_image_reads_share_one_executor(self):
        from concurrent.futures import ThreadPoolExecutor
        with patch('nisar_subset_engine.ThreadPoolExecutor',wraps=ThreadPoolExecutor) as executor:
            download_subset(self.url,self.corners,self.root/'shared-pool.h5',connections=2)
        self.assertEqual(executor.call_count,1)

    def test_legacy_progress_versions_migrate_without_repeating_chunks(self):
        import nisar_subset_engine
        original=nisar_subset_engine.group_ranges
        for version in (1,2):
            with self.subTest(version=version):
                output=self.root/f'legacy-v{version}.h5'
                stop=threading.Event()
                def pause(**value):
                    if value.get('phase')=='并发下载' and value.get('downloaded',0)>0:stop.set()
                split=lambda chunks:original(chunks,max_size=4096,max_gap=0)
                with patch('nisar_subset_engine.group_ranges',side_effect=split):
                    with self.assertRaises(Paused):
                        download_subset(self.url,self.corners,output,connections=1,stop=stop,progress=pause)
                legacy=saved_manifest(output)
                done=legacy['done'].copy()
                db_path=Path(str(output)+'.progress.sqlite')
                if version==1:
                    db_path.unlink()
                else:
                    db=sqlite3.connect(db_path)
                    with db:db.execute("DELETE FROM metadata WHERE key='manifest'")
                    db.close()
                    legacy['done']={}
                legacy.update(version=version,complete=False)
                legacy_path=Path(str(output)+'.progress.json')
                legacy_path.write_text(json.dumps(legacy),encoding='utf-8')
                self.server.requests.clear()
                def check_migrated(**value):
                    if value.get('phase')=='并发下载':
                        self.assertFalse(legacy_path.exists())
                        self.assertEqual(saved_manifest(output)['version'],3)
                with patch('nisar_subset_engine.group_ranges',side_effect=split):
                    download_subset(self.url,self.corners,output,connections=2,progress=check_migrated)
                for chunk in legacy['plan']['chunks']:
                    if chunk['key'] in done:
                        self.assertFalse(any(start<=chunk['offset'] and end>=chunk['offset']+chunk['size']-1
                                             for start,end in self.server.requests if end>start))
                self.assert_output(output)
                self.assertEqual(list(self.root.glob(output.name+'*')),[output])

    def test_crash_during_metadata_rebuilds_only_owned_skeleton(self):
        output=self.root/'metadata-crash.h5'
        child='''import json,os,sys,pathlib,nisar_subset_engine
def crash(source,output,*args):
    pathlib.Path(output).write_bytes(b'incomplete metadata skeleton')
    os._exit(27)
nisar_subset_engine.create_subset=crash
nisar_subset_engine.download_subset(sys.argv[1],json.loads(sys.argv[2]),sys.argv[3],connections=2)
'''
        result=subprocess.run([sys.executable,'-B','-c',child,self.url,json.dumps(self.corners),str(output)],
                              cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=45)
        self.assertEqual(result.returncode,27,result.stderr.decode('utf-8',errors='replace'))
        self.assertIsNone(saved_manifest(output)['plan'])
        download_subset(self.url,self.corners,output,connections=2)
        self.assert_output(output)
        self.assertEqual(list(self.root.glob(output.name+'*')),[output])

    def test_unowned_partial_is_never_deleted(self):
        output=self.root/'unowned.h5'
        part=Path(str(output)+'.part')
        part.write_bytes(b'unrelated data')
        with self.assertRaisesRegex(ValueError,'未识别的临时文件'):
            download_subset(self.url,self.corners,output)
        self.assertEqual(part.read_bytes(),b'unrelated data')

    def assert_output(self, output, pol="HH"):
        with h5py.File(self.source) as source,h5py.File(output) as target:
            windows=json.loads(target.attrs["subset_radar_windows_json"])
            for freq,window in windows.items():
                a0,a1,r0,r1=window
                path=f"{BASE}/swaths/frequency{freq}/{pol}"
                if pol=="VV":
                    original=source[path].id
                    copied=target[path].id
                    for a in range(a0,a1,16):
                        for r in range(r0,r1,16):
                            self.assertEqual(original.read_direct_chunk((a,r)),copied.read_direct_chunk((a-a0,r-r0)))
                else:
                    np.testing.assert_array_equal(target[path][()],source[path][a0:a1,r0:r1])
                self.assertEqual(target[path].shape,(a1-a0,r1-r0))
                self.assertLess(target[path].size,source[path].size//3)
                if pol=="HH":self.assertEqual(len(target[path].dims[0]),1)
                valid=target[f"{BASE}/swaths/frequency{freq}/validSamplesSubSwath1"][()]
                self.assertTrue(np.all((valid==-1)|(valid>=0)))
                invalid=np.iinfo(valid.dtype).max if np.issubdtype(valid.dtype,np.unsignedinteger) else -1
                self.assertTrue(np.any(valid[:,0]==invalid))
                mask=f"{BASE}/swaths/frequency{freq}/inputDataExceptionMask"
                np.testing.assert_array_equal(target[mask][()],source[mask][a0:a1,r0:r1])
                self.assertEqual(target[f"{BASE}/swaths/frequency{freq}/listOfPolarizations"][0],pol.encode())
            np.testing.assert_array_equal(target[f"{BASE}/metadata/orbit/position"],source[f"{BASE}/metadata/orbit/position"])
            self.assertNotIn(BASE+"/metadata/RFI",target)

    def test_subset_both_bands_preserves_values_and_metadata(self):
        output=self.root/"both-telemetry.h5"
        events=[]
        download_subset(self.url,self.corners,output,frequencies=["A","B"],connections=4,progress=lambda **v:events.append(v))
        self.assertTrue(any(event.get("network_bytes",0)>0 for event in events))
        self.assertTrue(any(event.get("phase")=="计算雷达窗口" for event in events))
        self.assertGreater(max(event.get("network_requests",0) for event in events),0)
        self.assert_output(output)
        before=len(self.server.requests)
        download_subset(self.url,self.corners,output,frequencies=["A","B"])
        self.assertEqual(len(self.server.requests)-before,1) # identity probe only

    def test_pause_restart_skips_committed_chunks(self):
        output=self.root/"resume.h5";stop=threading.Event()
        def progress(**value):
            if value.get("phase")=="并发下载" and value.get("downloaded",0)>0:stop.set()
        # Force several independently checkpointed byte ranges.
        import nisar_subset_engine
        original=nisar_subset_engine.group_ranges
        with patch("nisar_subset_engine.group_ranges",side_effect=lambda chunks:original(chunks,max_size=4096,max_gap=0)):
            with self.assertRaises(Paused):
                download_subset(self.url,self.corners,output,stop=stop,progress=progress,connections=1)
        manifest=saved_manifest(output)
        self.assertGreater(len(manifest["done"]),0)
        self.assertLess(len(manifest["done"]),len(manifest["plan"]["chunks"]))
        done=[c for c in manifest["plan"]["chunks"] if c["key"] in manifest["done"]]
        self.server.requests.clear()
        # No shared downloader object survives; this is a fresh run from persisted files.
        with patch("nisar_subset_engine.group_ranges",side_effect=lambda chunks:original(chunks,max_size=4096,max_gap=0)):
            download_subset(self.url,self.corners,output,connections=2)
        for chunk in done:
            self.assertFalse(any(start<=chunk["offset"] and end>=chunk["offset"]+chunk["size"]-1
                                 for start,end in self.server.requests if end>start))
        self.assert_output(output)

    def test_source_identity_change_is_rejected(self):
        output=self.root/"both.h5"
        if not output.exists():download_subset(self.url,self.corners,output,frequencies=["A","B"])
        self.server.etag='"fixture-v2"'
        try:
            with self.assertRaisesRegex(ValueError,"标识变化"):
                download_subset(self.url,self.corners,output,frequencies=["A","B"])
        finally:self.server.etag='"fixture-v1"'

    def test_native_complex_float16_is_copied_without_numpy_conversion(self):
        output=self.root/"native.h5"
        download_subset(self.url,self.corners,output,polarizations=["VV"],connections=2)
        self.assert_output(output,pol="VV")

    def test_corrupt_committed_chunk_is_repaired_on_resume(self):
        output=self.root/"repair.h5"
        stop=threading.Event()
        def progress(**value):
            if value.get("phase")=="并发下载" and value.get("downloaded",0)>0:stop.set()
        import nisar_subset_engine
        original=nisar_subset_engine.group_ranges
        with patch("nisar_subset_engine.group_ranges",side_effect=lambda chunks:original(chunks,max_size=4096,max_gap=0)):
            with self.assertRaises(Paused):
                download_subset(self.url,self.corners,output,stop=stop,progress=progress,connections=1)
        manifest=saved_manifest(output)
        chunk=next(c for c in manifest["plan"]["chunks"] if c["key"] in manifest["done"])
        with h5py.File(str(output)+".part","r+") as h5:
            h5[chunk["path"]].id.write_direct_chunk(tuple(chunk["dest"]),b"corrupted compressed chunk",0)
        download_subset(self.url,self.corners,output,connections=2)
        self.assert_output(output)

    def test_real_process_crash_then_restart(self):
        output=self.root/"process-crash.h5"
        child='''import json,os,sys
import nisar_subset_engine
original=nisar_subset_engine.group_ranges
nisar_subset_engine.group_ranges=lambda chunks:original(chunks,max_size=4096,max_gap=0)
def progress(**value):
    if value.get("phase")=="并发下载" and value.get("downloaded",0)>0:
        os._exit(23)
nisar_subset_engine.download_subset(sys.argv[1],json.loads(sys.argv[2]),sys.argv[3],connections=1,progress=progress)
'''
        result=subprocess.run([sys.executable,"-B","-c",child,self.url,json.dumps(self.corners),str(output)],
                              cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=45)
        self.assertEqual(result.returncode,23,result.stderr.decode("utf-8",errors="replace"))
        manifest=saved_manifest(output)
        self.assertGreater(len(manifest["done"]),0)
        download_subset(self.url,self.corners,output,connections=2)
        self.assert_output(output)

    def test_geolocation_axes_and_dimensions_are_consistently_subsetted(self):
        output=self.root/"metadata.h5"
        download_subset(self.url,self.corners,output,connections=2)
        with h5py.File(self.source) as source,h5py.File(output) as target:
            path=BASE+"/metadata/geolocationGrid"
            grid=target[path]
            self.assertEqual(grid["coordinateX"].shape,(3,len(grid["zeroDopplerTime"]),len(grid["slantRange"])))
            self.assertLess(grid["coordinateX"].size,source[path+"/coordinateX"].size)
            self.assertNotEqual(target["science/LSAR/identification/boundingPolygon"][()],source["science/LSAR/identification/boundingPolygon"][()])

    def test_invalid_and_nonintersecting_corners(self):
        with self.assertRaises(ValueError):validate_corners([[0,0],[1,1],[0,1],[1,0]])
        with h5py.File(self.source) as source:
            with self.assertRaisesRegex(ValueError,"不相交"):
                radar_window(source,validate_corners([[1,2],[2,2],[2,1],[1,1]]))


if __name__=="__main__":unittest.main(verbosity=2)
