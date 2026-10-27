"""Independent RSLC subset engine. Uses public HTTP Range and HDF5 APIs.

No SARGet source or bytecode is imported or executed by this project.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta
import hashlib
import gc
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

import h5py
import numpy as np
import requests
from shapely.geometry import Polygon
from cache_cleanup import SUBSET_RESUME_SUFFIXES

BASE = "science/LSAR/RSLC"
POLS = {"HH", "HV", "VH", "VV", "RH", "RV", "LH", "LV"}
REF_ATTRS = {"DIMENSION_LIST", "REFERENCE_LIST"}
_remote_gc_lock = threading.RLock()
_remote_gc_users = 0
_remote_gc_restore = False


@contextmanager
def remote_hdf5(reader):
    """Avoid cyclic GC on service threads while HDF5 holds its file-object lock.

    h5py documents this service-thread deadlock. Reference-counted suspension
    covers overlapping scenes and restores the caller's previous GC setting.
    Reference-count destruction remains active; only cyclic collection pauses.
    """
    global _remote_gc_users, _remote_gc_restore
    with _remote_gc_lock:
        if _remote_gc_users == 0:
            _remote_gc_restore = gc.isenabled()
            gc.disable()
        _remote_gc_users += 1
    try:
        with h5py.File(reader, "r") as file:
            yield file
    finally:
        with _remote_gc_lock:
            _remote_gc_users -= 1
            if _remote_gc_users == 0 and _remote_gc_restore:
                gc.enable()


class Paused(Exception):
    pass


class ProgressStore:
    """One connection and one SQLite file for the plan and committed block hashes."""
    def __init__(self, path, signature):
        self.path, self.signature = str(path), signature
        self.db = db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS blocks (key TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
            row = db.execute("SELECT value FROM metadata WHERE key='signature'").fetchone()
            if row and row[0] != signature:
                raise ValueError("续传数据库属于不同任务，未写入数据。")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('signature',?)",(signature,))
            db.commit()
        except BaseException:
            db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def manifest(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
        return json.loads(row[0]) if row else None

    def save_manifest(self, manifest):
        value = {key: value for key, value in manifest.items() if key not in ("done", "complete")}
        value["version"] = 3
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('manifest',?)", (json.dumps(value),))
            self.db.executemany("INSERT OR REPLACE INTO blocks VALUES (?,?)", manifest.get("done", {}).items())

    def load(self):
        return dict(self.db.execute("SELECT key,sha256 FROM blocks"))

    def update(self, values=None, remove=()):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO blocks VALUES (?,?)",(values or {}).items())
            self.db.executemany("DELETE FROM blocks WHERE key=?",((key,) for key in remove))


def completion_info(path):
    """Completion metadata lives in the finished HDF5, not an external journal."""
    try:
        with h5py.File(path, "r") as file:
            if not bool(file.attrs.get("subset_download_complete", False)):
                return None
            return {"signature": str(file.attrs["subset_download_signature"]),
                    "identity": json.loads(file.attrs["subset_source_identity_json"]),
                    "total": int(file.attrs["subset_image_bytes"]),
                    "chunks": int(file.attrs["subset_image_chunk_count"])}
    except (OSError, KeyError, ValueError):
        return None


def verify_and_stamp(file, plan, done, signature, identity, config, check):
    """Verify every committed raw chunk before declaring the HDF5 complete."""
    for path, info in plan["datasets"].items():
        if file[path].shape != tuple(info["shape"]):
            raise RuntimeError("成品尺寸校验失败，已保留续传记录。")
    for chunk in plan["chunks"]:
        check()
        expected = done.get(chunk["key"])
        if not expected:
            raise RuntimeError("尚有未提交的数据块，已保留续传记录。")
        _, raw = file[chunk["path"]].id.read_direct_chunk(tuple(chunk["dest"]))
        if len(raw) != chunk["size"] or hashlib.sha256(raw).hexdigest() != expected:
            raise RuntimeError("成品数据块校验失败，已保留续传记录，可继续重取。")
    file.attrs["subset_download_signature"] = signature
    file.attrs["subset_source_identity_json"] = json.dumps(identity, sort_keys=True)
    file.attrs["subset_download_config_json"] = json.dumps(config, sort_keys=True)
    file.attrs["subset_image_bytes"] = plan["total"]
    file.attrs["subset_image_chunk_count"] = len(plan["chunks"])
    file.attrs["subset_download_complete"] = True
    file.flush()


def cleanup_download_cache(output):
    """Delete only this verified download's exact cache filenames."""
    for suffix in SUBSET_RESUME_SUFFIXES:
        try:
            Path(str(output)+suffix).unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError("影像已完成，但续传文件被占用，清理未完成；关闭占用程序后点击继续即可清理。") from exc


def rename_finished(part, output):
    """Publish without overwriting an existing file, even at the rename step."""
    try:
        if os.name == "nt":
            os.rename(part, output)
        else:
            os.link(part, output)
            part.unlink()
    except FileExistsError as exc:
        raise ValueError("同名成品已存在，未覆盖。不同裁剪范围或极化请使用其他保存目录。") from exc


def validate_corners(corners):
    if not isinstance(corners, list) or len(corners) != 4:
        raise ValueError("请输入西北、东北、东南、西南四个角的经纬度。")
    points = []
    for pair in corners:
        if len(pair) != 2:
            raise ValueError("每个角需要经度、纬度两个数值。")
        lon, lat = map(float, pair)
        if not math.isfinite(lon + lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValueError("经纬度超出 WGS84 有效范围。")
        points.append((lon, lat))
    if max(p[0] for p in points) - min(p[0] for p in points) >= 180:
        raise ValueError("本版暂不支持跨越 180° 经线的范围。")
    polygon = Polygon(points)
    if not polygon.is_valid or polygon.area < 1e-12:
        raise ValueError("四角范围无效或发生交叉，请按西北→东北→东南→西南顺序输入。")
    if not polygon.equals(polygon.convex_hull):
        raise ValueError("请使用凸四边形范围。")
    return polygon


class EarthdataSession(requests.Session):
    def __init__(self, credentials=None):
        super().__init__()
        self.credentials = credentials
        self.headers.update({"User-Agent": "NISAR-Subset-Local/0.1", "Accept-Encoding": "identity"})

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        # Basic credentials go only to NASA's authentication endpoint.
        if urlsplit(prepared_request.url).hostname == "urs.earthdata.nasa.gov" and self.credentials:
            prepared_request.prepare_auth(self.credentials)


class WorkbenchRangeSession:
    """Use the application's verified, domain-scoped Earthdata session."""
    def __init__(self, auth, source_url):
        self.session = auth.session()
        self.source_url = source_url

    def get(self, url, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("allow_redirects", None)
        return self.session.request("GET", url, _redirect_from=self.source_url, **kwargs)

    def close(self):
        self.session.close()


class RangeSource:
    def __init__(self, url, credentials=None, stop=None, timeout=40, progress=None, connections=6):
        self.url, self.credentials = url, credentials
        self.stop = stop or threading.Event()
        self.timeout = timeout
        self.local = threading.local()
        self.identity = None
        self.resolved_url = url
        self.received = 0
        self.lock = threading.Lock()
        self.progress = progress or (lambda **kw: None)
        self.requests = self.retries = 0
        self.last_activity = None
        self.reported_at, self.reported_bytes = time.monotonic(), 0
        self.connections = max(1, min(16, int(connections)))
        self.executor, self.sessions = None, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.stop.set()
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
        for session in self.sessions:
            session.close()
        self.sessions.clear()

    def submit(self, function, *args):
        with self.lock:
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=self.connections)
            return self.executor.submit(function, *args)

    def report_network(self, force=False, note=""):
        now=time.monotonic()
        with self.lock:
            elapsed=now-self.reported_at
            if not force and elapsed<.5:
                return
            values={"network_bytes":self.received,"network_requests":self.requests,
                    "network_retries":self.retries,"network_last_activity":self.last_activity,
                    "network_speed":(self.received-self.reported_bytes)/max(.01,elapsed),
                    "network_note":note}
            self.reported_at,self.reported_bytes=now,self.received
        # Never expose URLs, redirect query strings, or credentials in telemetry.
        self.progress(**values)

    def session(self):
        if not hasattr(self.local, "session"):
            self.local.session = WorkbenchRangeSession(self.credentials, self.url) if hasattr(self.credentials, "session") else EarthdataSession(self.credentials)
            with self.lock:
                self.sessions.append(self.local.session)
        return self.local.session

    def check(self):
        if self.stop.is_set():
            raise Paused()

    def read(self, start, end):
        """Inclusive start, exclusive end. Reject full-file 200 responses."""
        if end <= start:
            return b""
        self.check()
        last = None
        for attempt in range(4):
            self.check()
            with self.lock:
                self.requests += 1
            self.report_network(force=True)
            headers = {"Range": f"bytes={start}-{end-1}"}
            if self.identity and self.identity.get("etag"):
                headers["If-Match"] = self.identity["etag"]
            try:
                target_url = self.resolved_url
                with self.session().get(target_url, headers=headers, stream=True,
                                        timeout=(12, self.timeout), allow_redirects=True) as response:
                    if response.status_code in (401, 403):
                        if target_url != self.url:
                            # Signed cloud URLs expire. Revisit the stable Earthdata URL.
                            self.resolved_url = self.url
                            continue
                        raise PermissionError("Earthdata 认证失败或尚未授权 ASF。请更新账号后恢复任务。")
                    if urlsplit(response.url).hostname == "urs.earthdata.nasa.gov":
                        raise PermissionError("Earthdata 登录未完成，请核对账号和 ASF 授权。")
                    if response.status_code == 412:
                        raise ValueError("源文件已变更，不能继续写入旧任务。请创建新任务。")
                    if response.status_code == 429 or response.status_code >= 500:
                        raise requests.ConnectionError(f"源站暂时繁忙（HTTP {response.status_code}）")
                    if response.status_code != 206:
                        raise ValueError(f"源站没有返回局部数据（HTTP {response.status_code}），已停止，避免整景下载。")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match or tuple(map(int, match.groups()[:2])) != (start, end-1):
                        raise ValueError("Content-Range 与请求不符，未写入数据。")
                    identity = {"size": int(match[3]), "etag": response.headers.get("ETag"),
                                "modified": response.headers.get("Last-Modified")}
                    if self.identity and identity != self.identity:
                        raise ValueError("远端文件标识发生变化，无法安全续传。")
                    if response.headers.get("Content-Encoding", "identity") not in ("identity", ""):
                        raise ValueError("源站对字节区间应用了额外编码，无法安全解析。")
                    data = bytearray()
                    for part in response.iter_content(128 * 1024):
                        self.check()
                        data.extend(part)
                        with self.lock:
                            self.received += len(part)
                            self.last_activity=time.time()
                        self.report_network()
                        if len(data) > end-start:
                            raise ValueError("局部响应超出预期长度。")
                    if len(data) != end-start:
                        raise requests.ConnectionError("响应中断或长度不足")
                    self.identity = identity
                    self.resolved_url = response.url
                    self.report_network(force=True)
                    return bytes(data)
            except (requests.RequestException, OSError) as exc:
                if isinstance(exc, PermissionError):
                    raise
                last = exc
                if attempt == 3:
                    break
                with self.lock:
                    self.retries += 1
                self.report_network(force=True,note=f"网络重试 {attempt+1}/3")
                if self.stop.wait(min(2**attempt, 8)):
                    raise Paused()
        raise RuntimeError("网络请求连续失败，已保留进度；恢复任务可继续。") from last

    def probe(self):
        self.read(0, 1)
        return self.identity


class RemoteFile(io.RawIOBase):
    """Seekable HTTP reader with a bounded block cache for HDF5 metadata."""
    def __init__(self, source, block_size=512*1024, cache_blocks=128):
        self.source, self.block_size, self.cache_blocks = source, block_size, cache_blocks
        self.size = source.identity["size"] if source.identity else source.probe()["size"]
        self.pos, self.cache = 0, OrderedDict()

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos

    def seek(self, offset, whence=0):
        position = offset + (0 if whence == 0 else self.pos if whence == 1 else self.size)
        if position < 0:
            raise ValueError("Negative seek")
        self.pos = position
        return position

    def read(self, size=-1):
        end = self.size if size < 0 else min(self.size, self.pos + size)
        result = bytearray()
        while self.pos < end:
            index = self.pos // self.block_size
            if index not in self.cache:
                start = index * self.block_size
                self.cache[index] = self.source.read(start, min(self.size, start+self.block_size))
                if len(self.cache) > self.cache_blocks:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(index)
            offset = self.pos % self.block_size
            piece = self.cache[index][offset:offset + min(end-self.pos, self.block_size-offset)]
            result.extend(piece)
            self.pos += len(piece)
        return bytes(result)

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def prefetch(self, ranges):
        """Concurrent page-aligned reads; all h5py calls remain on the caller thread."""
        pages = set()
        for start, end in ranges:
            pages.update(range(start//self.block_size, (end-1)//self.block_size+1))
        missing = sorted(p for p in pages if p not in self.cache)
        # Bound working sets to the LRU capacity. Excess pages remain lazy reads.
        missing = missing[:self.cache_blocks]
        batches = []
        for page in missing:
            if batches and page == batches[-1][-1]+1 and len(batches[-1])*self.block_size < 4*1024*1024:
                batches[-1].append(page)
            else:
                batches.append([page])
        def fetch(batch):
            start = batch[0]*self.block_size
            end = min(self.size,(batch[-1]+1)*self.block_size)
            return batch, self.source.read(start,end)
        futures = [self.source.submit(fetch, batch) for batch in batches]
        for future in futures:
            batch, data = future.result()
            for page in batch:
                offset=(page-batch[0])*self.block_size
                self.cache[page]=data[offset:offset+self.block_size]
                self.cache.move_to_end(page)
                if len(self.cache)>self.cache_blocks:
                    self.cache.popitem(last=False)


def prefetch_dataset(source, dataset, selection=()):
    """Prefetch compressed chunks for a dataset selection via its HTTP reader."""
    reader = getattr(source,"_subset_remote_reader",None)
    if reader is None or dataset.size == 0:
        return
    if isinstance(selection,slice):
        selection=(selection,)
    ranges=[]
    if dataset.chunks:
        selection = selection if selection else tuple(slice(None) for _ in dataset.shape)
        starts=[]
        for size,block,part in zip(dataset.shape,dataset.chunks,selection):
            begin,end,_=part.indices(size)
            starts.append(range(begin//block*block,end,block))
        from itertools import product
        for coordinate in product(*starts):
            info=dataset.id.get_chunk_info_by_coord(coordinate)
            if info.byte_offset is not None and info.size:
                ranges.append((int(info.byte_offset),int(info.byte_offset+info.size)))
    elif selection == () or all(part == slice(None) for part in selection):
        offset=dataset.id.get_offset()
        if offset is not None:
            ranges=[(int(offset),int(offset+dataset.id.get_storage_size()))]
    if ranges:
        reader.prefetch(ranges)


def inverse_cell(points, longitude, latitude):
    """Invert a bilinear lon/lat cell. u=range, v=azimuth."""
    p00, p10, p11, p01 = np.asarray(points, dtype=float)
    u = v = .5
    target = np.array([longitude, latitude])
    for _ in range(16):
        point = (1-u)*(1-v)*p00 + u*(1-v)*p10 + u*v*p11 + (1-u)*v*p01
        du = (1-v)*(p10-p00) + v*(p11-p01)
        dv = (1-u)*(p01-p00) + u*(p11-p10)
        jacobian = np.column_stack([du, dv])
        if abs(np.linalg.det(jacobian)) < 1e-15:
            raise ValueError("退化定位网格")
        delta = np.linalg.solve(jacobian, point-target)
        u, v = u-delta[0], v-delta[1]
        if np.max(np.abs(delta)) < 1e-8:
            break
    return float(np.clip(u, 0, 1)), float(np.clip(v, 0, 1))


def geolocation_arrays(source, min_height=None, max_height=None):
    report = getattr(source, "_subset_progress", lambda **kw: None)
    grid = source[f"{BASE}/metadata/geolocationGrid"]
    height_key = "height" if "height" in grid else "heightAboveEllipsoid"
    heights = np.asarray(grid[height_key][()]).reshape(-1)
    lower,upper=0,len(heights)
    if min_height is not None:
        lower=max(0,int(np.searchsorted(heights,float(min_height)))-1)
    if max_height is not None:
        upper=min(len(heights),int(np.searchsorted(heights,float(max_height),side="right"))+1)
    if lower>=upper:
        raise ValueError("指定高程不在定位网格范围内。")
    selection=(slice(lower,upper),slice(None),slice(None))
    for name in ("coordinateX","coordinateY"):
        report(phase="读取定位网格", detail=f"{'经度' if name == 'coordinateX' else '纬度'} · {upper-lower} 个高度层")
        prefetch_dataset(source,grid[name],selection)
        if name == "coordinateX":
            longitude = grid[name][selection]
        else:
            latitude = grid[name][selection]
    heights=heights[lower:upper]
    times, ranges = grid["zeroDopplerTime"][()], grid["slantRange"][()]
    epsg = int(grid["epsg"][()]) if "epsg" in grid else 4326
    if epsg != 4326:
        from pyproj import Transformer
        longitude, latitude = Transformer.from_crs(epsg, 4326, always_xy=True).transform(longitude, latitude)
    if longitude.shape != (len(heights), len(times), len(ranges)) or latitude.shape != longitude.shape:
        raise ValueError("定位网格维度与坐标轴不一致，无法可靠裁剪。")
    if np.any(np.diff(times) <= 0) or np.any(np.diff(ranges) <= 0) or np.any(np.diff(heights) <= 0):
        raise ValueError("定位网格坐标轴必须单调递增。")
    return longitude, latitude, heights, times, ranges


def radar_window(source, polygon, min_height=None, max_height=None, padding=32, arrays=None):
    longitude, latitude, heights, times, ranges = arrays if arrays is not None else geolocation_arrays(source,min_height,max_height)
    report = getattr(source, "_subset_progress", lambda **kw: None)
    check = getattr(source, "_subset_check", lambda: None)
    levels = np.arange(len(heights))
    if min_height is not None or max_height is not None:
        low = float(min_height if min_height is not None else heights.min())
        high = float(max_height if max_height is not None else heights.max())
        if low > high:
            raise ValueError("最低高程不能大于最高高程。")
        # Include both bracketing levels, rather than assuming a constant terrain.
        levels = np.unique(np.clip([np.searchsorted(heights, low)-1, np.searchsorted(heights, high)] +
                                  list(np.where((heights >= low) & (heights <= high))[0]), 0, len(heights)-1))
    west, south, east, north = polygon.bounds
    limits = []
    vertices = np.asarray(polygon.exterior.coords)[:-1,:2]
    orientation = 1 if np.sum(vertices[:,0]*np.roll(vertices[:,1],-1)-np.roll(vertices[:,0],-1)*vertices[:,1]) > 0 else -1
    for index,level in enumerate(levels):
        check()
        report(phase="计算雷达窗口", detail=f"高度层 {index+1}/{len(levels)} · {heights[level]:g} m",
               prep_current=index, prep_total=len(levels))
        lon, lat = longitude[level], latitude[level]
        xs = np.stack([lon[:-1,:-1], lon[:-1,1:], lon[1:,1:], lon[1:,:-1]])
        ys = np.stack([lat[:-1,:-1], lat[:-1,1:], lat[1:,1:], lat[1:,:-1]])
        candidates = (np.isfinite(xs).all(0) & np.isfinite(ys).all(0) &
                      (xs.min(0) <= east) & (xs.max(0) >= west) &
                      (ys.min(0) <= north) & (ys.max(0) >= south))
        # A convex cell entirely inside the requested convex polygon contributes
        # its native axis bounds directly. Only boundary cells need inverse solves.
        inside = candidates.copy()
        for a,b in zip(vertices,np.roll(vertices,-1,axis=0)):
            inside &= (orientation*((b[0]-a[0])*(ys-a[1])-(b[1]-a[1])*(xs-a[0])) >= 0).all(0)
        dx,dy=np.roll(xs,-1,axis=0)-xs,np.roll(ys,-1,axis=0)-ys
        turns=dx*np.roll(dy,-1,axis=0)-dy*np.roll(dx,-1,axis=0)
        inside &= (turns>0).all(0) | (turns<0).all(0)
        full_rows,full_cols=np.where(inside)
        if len(full_rows):
            limits.extend([(times[full_rows.min()],ranges[full_cols.min()]),
                           (times[full_rows.max()+1],ranges[full_cols.max()+1])])
        for cell_index,(row, col) in enumerate(np.argwhere(candidates & ~inside)):
            if cell_index % 64 == 0:
                check()
            points = np.column_stack([xs[:,row,col], ys[:,row,col]])
            cell = Polygon(points)
            if not cell.is_valid or cell.area <= 0 or not cell.intersects(polygon):
                continue
            intersection = cell.intersection(polygon)
            geometries = list(intersection.geoms) if hasattr(intersection, "geoms") else [intersection]
            for shape in geometries:
                coords = list(shape.exterior.coords) if hasattr(shape, "exterior") else list(shape.coords)
                for a, b in zip(coords, coords[1:] or coords):
                    for alpha in np.linspace(0, 1, 7):
                        point = np.asarray(a[:2])*(1-alpha) + np.asarray(b[:2])*alpha
                        try:
                            u, v = inverse_cell(points, *point)
                        except ValueError:
                            continue
                        limits.append((np.interp(row+v, np.arange(len(times)), times),
                                       np.interp(col+u, np.arange(len(ranges)), ranges)))
    if not limits:
        raise ValueError("四角范围与该影像定位网格不相交。")
    limits = np.asarray(limits)
    az = source[f"{BASE}/swaths/zeroDopplerTime"][()]
    az0 = max(0, int(np.searchsorted(az, limits[:,0].min()))-padding)
    az1 = min(len(az), int(np.searchsorted(az, limits[:,0].max(), side="right"))+padding)
    report(prep_current=len(levels),prep_total=len(levels))
    return az0, az1, float(limits[:,1].min()), float(limits[:,1].max())


def align_window(window, shape, chunks):
    a0, a1, r0, r1 = window
    return (max(0, a0//chunks[0]*chunks[0]), min(shape[0], math.ceil(a1/chunks[0])*chunks[0]),
            max(0, r0//chunks[1]*chunks[1]), min(shape[1], math.ceil(r1/chunks[1])*chunks[1]))


def make_plan(source, polygon, frequencies, polarizations, min_height=None, max_height=None):
    if BASE not in source:
        raise ValueError("该文件不是受支持的 NISAR L-band RSLC。")
    arrays = geolocation_arrays(source,min_height,max_height)
    az0, az1, near, far = radar_window(source, polygon, min_height, max_height, arrays=arrays)
    report = getattr(source, "_subset_progress", lambda **kw: None)
    check = getattr(source, "_subset_check", lambda: None)
    report(phase="索引影像数据块", detail="读取块位置与压缩大小",prep_current=0,prep_total=0)
    datasets, chunks, windows = {}, [], {}
    for freq in frequencies:
        path = f"{BASE}/swaths/frequency{freq}"
        if path not in source:
            raise ValueError(f"该影像没有频率 {freq}，请修改下载设置。")
        rg = source[path + "/slantRange"][()]
        r0, r1 = max(0, int(np.searchsorted(rg, near))-32), min(len(rg), int(np.searchsorted(rg, far, side="right"))+32)
        shapes = []
        for pol in polarizations:
            dataset_path = f"{path}/{pol}"
            if dataset_path not in source:
                raise ValueError(f"该影像频率 {freq} 没有 {pol} 极化。")
            ds = source[dataset_path]
            if not ds.chunks or len(ds.shape) != 2:
                raise ValueError("SLC 不是二维分块布局，无法采用原始块续传。")
            shapes.append((ds.shape, ds.chunks))
        if len(set(shapes)) != 1:
            raise ValueError("同频率极化的网格或分块不一致，暂不支持。")
        shape, block = shapes[0]
        window = align_window((az0, az1, r0, r1), shape, block)
        if window[0] >= window[1] or window[2] >= window[3]:
            raise ValueError("所选区域没有可下载的像元。")
        windows[freq] = list(window)
        for pol in polarizations:
            key = f"{path}/{pol}"
            ds = source[key]
            datasets[key] = {"window": list(window), "shape": [window[1]-window[0], window[3]-window[2]]}
            for a in range(window[0], window[1], block[0]):
                check()
                for r in range(window[2], window[3], block[1]):
                    info = ds.id.get_chunk_info_by_coord((a, r))
                    if info.byte_offset is None or info.size <= 0:
                        continue
                    chunks.append({"key": f"{freq}/{pol}/{a}/{r}", "path": key,
                                   "offset": int(info.byte_offset), "size": int(info.size),
                                   "dest": [a-window[0], r-window[2]], "filter_mask": int(info.filter_mask)})
    # Keep time axes common to A and B. Different azimuth chunks require a shared alignment.
    az_windows = {(w[0], w[1]) for w in windows.values()}
    if len(az_windows) != 1:
        raise ValueError("A/B 方位分块不能保持共同时间轴，请分开下载两个频率。")
    if not chunks:
        raise ValueError("所选窗口内没有已分配的影像数据块。")
    plan = {"datasets": datasets, "windows": windows, "chunks": chunks,
            "total": sum(c["size"] for c in chunks)}
    plan["footprint"], plan["footprint_height"] = native_footprint(source, plan, arrays)
    plan["height_bounds"]=[float(arrays[2].min()),float(arrays[2].max())]
    return plan


def copy_attrs(src, dst):
    for name, value in src.attrs.items():
        if name in REF_ATTRS:
            continue
        if h5py.check_dtype(ref=np.asarray(value).dtype):
            continue
        dst.attrs[name] = value


def native_footprint(source, plan, arrays):
    longitude, latitude, heights, times, ranges = arrays
    level = int(np.argmin(np.abs(heights)))
    freq, window = next(iter(plan["windows"].items()))
    a0, a1, r0, r1 = window
    az = source[f"{BASE}/swaths/zeroDopplerTime"]
    rg = source[f"{BASE}/swaths/frequency{freq}/slantRange"]
    vertices = [(az[a0], rg[r0]), (az[a0], rg[r1-1]), (az[a1-1], rg[r1-1]), (az[a1-1], rg[r0])]
    points = []
    for start, end in zip(vertices, vertices[1:] + vertices[:1]):
        for fraction in np.linspace(0, 1, 17, endpoint=False):
            t = start[0]*(1-fraction) + end[0]*fraction
            r = start[1]*(1-fraction) + end[1]*fraction
            ai = float(np.interp(t, times, np.arange(len(times))))
            ri = float(np.interp(r, ranges, np.arange(len(ranges))))
            a, col = min(int(ai), len(times)-2), min(int(ri), len(ranges)-2)
            v, u = ai-a, ri-col
            def interpolate(array):
                return ((1-u)*(1-v)*array[level,a,col]+u*(1-v)*array[level,a,col+1]+
                        u*v*array[level,a+1,col+1]+(1-u)*v*array[level,a+1,col])
            points.append((interpolate(longitude),interpolate(latitude)))
    footprint = Polygon(points)
    if not footprint.is_valid:
        footprint = footprint.buffer(0)
    if footprint.is_empty:
        raise ValueError("无法生成裁剪后的有效覆盖范围。")
    return footprint.wkt, float(heights[level])


def create_subset(source, output, plan, frequencies, polarizations, polygon, check):
    """Subset calibration/geolocation LUTs with bracketing coordinate samples."""
    windows = plan["windows"]
    report = getattr(source, "_subset_progress", lambda **kw: None)
    first = next(iter(windows.values()))
    a0, a1 = first[:2]
    excluded_freqs = {"A", "B"} - set(frequencies)
    scales = []
    footprint, footprint_height = plan["footprint"], plan["footprint_height"]
    az_axis = source[f"{BASE}/swaths/zeroDopplerTime"]
    time_bounds = (float(az_axis[a0]), float(az_axis[a1-1]))
    range_bounds = []
    for freq, window in windows.items():
        axis = source[f"{BASE}/swaths/frequency{freq}/slantRange"]
        range_bounds.extend([float(axis[window[2]]), float(axis[window[3]-1])])
    physical = {"zeroDopplerTime": time_bounds, "slantRange": (min(range_bounds), max(range_bounds)),
                "height":plan["height_bounds"],"heightAboveEllipsoid":plan["height_bounds"]}
    axis_slices, group_axes = {}, {}
    def find_axes(name, item):
        if not name.startswith(BASE+"/metadata/") or not isinstance(item, h5py.Dataset) or item.ndim != 1:
            return
        axis_name = name.rsplit("/", 1)[-1]
        if axis_name not in physical or len(item) < 2:
            return
        values = item[()]
        if np.any(np.diff(values) <= 0):
            return
        lo, hi = physical[axis_name]
        padding=0 if axis_name in ("height","heightAboveEllipsoid") else 1
        start = max(0, int(np.searchsorted(values, lo))-padding)
        end = min(len(values), int(np.searchsorted(values, hi, side="right"))+padding)
        if end-start < 2:
            start, end = max(0, min(start,len(values)-2)), min(len(values), max(end,2))
        axis_slices[item.name] = slice(start, end)
        group_axes.setdefault(item.parent.name,{})[axis_name] = item.name
    source.visititems(find_axes)
    def metadata_selection(item):
        if item.name in axis_slices:
            return axis_slices[item.name]
        selection = [slice(None)]*item.ndim
        used = set()
        for dimension, dim in enumerate(item.dims):
            for _, scale in dim.items():
                if scale.name in axis_slices:
                    selection[dimension] = axis_slices[scale.name]
                    used.add(dimension)
        # Some products omit attached scales. Match known sibling axes only
        # for unambiguous trailing range/time dimensions of LUTs.
        parent = item.parent
        while parent.name.startswith("/"+BASE+"/metadata"):
            axes = group_axes.get(parent.name,{})
            for axis_name, axis_path in axes.items():
                expected_dimension = item.ndim-1 if axis_name=="slantRange" else item.ndim-2 if axis_name=="zeroDopplerTime" else 0
                if expected_dimension >= 0 and expected_dimension not in used and item.shape[expected_dimension] == len(source[axis_path]):
                    selection[expected_dimension] = axis_slices[axis_path]
                    used.add(expected_dimension)
            if axes:
                break
            parent=parent.parent
        return tuple(selection)
    with h5py.File(output, "w") as target:
        copy_attrs(source, target)
        def clone(group, parent):
            for name in group:
                check()
                item = group[name]
                path = item.name.lstrip("/")
                if path == BASE+"/metadata/RFI":
                    continue
                if any(f"/frequency{f}" in item.name for f in excluded_freqs):
                    continue
                if name in POLS and name not in polarizations:
                    continue
                if isinstance(item, h5py.Group):
                    child = parent.create_group(name)
                    copy_attrs(item, child)
                    clone(item, child)
                    continue
                if path in plan["datasets"]:
                    info = plan["datasets"][path]
                    shape = tuple(info["shape"])
                    maximum = tuple(max(a, b) for a, b in zip(shape, item.chunks))
                    space = h5py.h5s.create_simple(shape, maximum)
                    # Low-level type copy preserves NISAR's complex float16 layout.
                    identifier = h5py.h5d.create(parent.id, name.encode(), item.id.get_type(), space,
                                               dcpl=item.id.get_create_plist())
                    child = h5py.Dataset(identifier)
                else:
                    report(detail=f"辅助数据：{path}")
                    selection = ()
                    if path.startswith(BASE+"/metadata/") and item.ndim:
                        selection = metadata_selection(item)
                    for freq, window in windows.items():
                        prefix = f"{BASE}/swaths/frequency{freq}/"
                        if path.startswith(prefix) and item.ndim == 2 and not name.startswith("validSamplesSubSwath"):
                            full_shape = (len(source[f"{BASE}/swaths/zeroDopplerTime"]),
                                          len(source[prefix+"slantRange"]))
                            if item.shape == full_shape:
                                selection = (slice(window[0],window[1]),slice(window[2],window[3]))
                    if item.size * item.dtype.itemsize > 64*1024*1024 and selection == ():
                        raise ValueError(f"辅助数据 {path} 过大且未识别坐标轴，已停止，避免下载整景数组。")
                    try:
                        prefetch_dataset(source,item,selection)
                        value = item[selection]
                    except Paused:
                        raise
                    except Exception as exc:
                        raise RuntimeError(f"读取辅助数据 {path} 失败：{exc}") from exc
                    if path == f"{BASE}/swaths/zeroDopplerTime":
                        value = value[a0:a1]
                    for freq, window in windows.items():
                        prefix = f"{BASE}/swaths/frequency{freq}/"
                        r0, r1 = window[2:]
                        if path == prefix + "slantRange":
                            value = value[r0:r1]
                        if path.startswith(prefix) and name.startswith("validSamplesSubSwath"):
                            if np.asarray(value).shape[-1] != 2:
                                raise ValueError("无法识别有效样本索引布局。")
                            original_dtype=value.dtype
                            value = np.array(value[a0:a1], dtype=np.int64,copy=True)
                            original_width=len(source[prefix+"slantRange"])
                            valid = (value[:,0] >= 0) & (value[:,1] > value[:,0]) & (value[:,0]<original_width) & (value[:,1]<=original_width)
                            value[:,0] = np.maximum(0, value[:,0]-r0)
                            value[:,1] = np.minimum(r1-r0, value[:,1]-r0)
                            valid &= value[:,1] > value[:,0]
                            value[~valid] = -1
                            if np.issubdtype(original_dtype,np.unsignedinteger):
                                value[~valid]=np.iinfo(original_dtype).max
                            value=value.astype(original_dtype)
                    if name == "listOfPolarizations":
                        value = np.asarray(polarizations, dtype="S2")
                    if name == "listOfFrequencies":
                        value = np.asarray(frequencies, dtype="S1")
                    if path.endswith("/identification/boundingPolygon"):
                        value = np.bytes_(footprint)
                    if path.endswith(("/identification/zeroDopplerStartTime", "/identification/zeroDopplerEndTime")):
                        axis = source[f"{BASE}/swaths/zeroDopplerTime"]
                        units = axis.attrs.get("units", "")
                        units = units.decode() if isinstance(units, bytes) else str(units)
                        if "since " in units:
                            epoch = datetime.fromisoformat(units.split("since ", 1)[1].strip().replace("Z", "+00:00"))
                            when = epoch + timedelta(seconds=float(axis[a0 if name.endswith("StartTime") else a1-1]))
                            value = np.bytes_(when.isoformat().replace("+00:00", "Z"))
                    child = parent.create_dataset(name, data=value)
                copy_attrs(item, child)
                try:
                    for axis, dim in enumerate(item.dims):
                        for _, scale in dim.items():
                            scales.append((child.name, axis, scale.name))
                except (KeyError, RuntimeError):
                    pass
        clone(source, target)
        for path, axis, scale in scales:
            if scale in target and path in target and path != scale:
                target[path].dims[axis].attach_scale(target[scale])
        target.attrs["subset_requested_polygon_wkt"] = polygon.wkt
        target.attrs["subset_radar_windows_json"] = json.dumps(windows)
        target.attrs["subset_tool"] = "NISAR Subset Local 0.1"
        target.attrs["subset_omitted_paths"] = BASE+"/metadata/RFI (whole-scene processing diagnostics)"
        target.attrs["subset_footprint_height_meters"] = footprint_height
        target.flush()


def group_ranges(chunks, max_size=8*1024*1024, max_gap=65536):
    groups = []
    for chunk in sorted(chunks, key=lambda c: c["offset"]):
        start, end = chunk["offset"], chunk["offset"]+chunk["size"]
        if groups and start-groups[-1]["end"] <= max_gap and end-groups[-1]["start"] <= max_size:
            groups[-1]["end"] = max(end, groups[-1]["end"])
            groups[-1]["chunks"].append(chunk)
        else:
            groups.append({"start": start, "end": end, "chunks": [chunk]})
    return groups


def validate_resume(manifest, signature, identity):
    if manifest["signature"] != signature or manifest["identity"] != identity:
        raise ValueError("续传配置或远端文件标识变化，请创建新任务。")
    if not identity.get("etag") and not identity.get("modified"):
        raise ValueError("远端未提供 ETag 或修改时间，无法安全跨会话续传。")


def prepare_plan(source, polygon, output, config, store, manifest, progress):
    if manifest and manifest.get("plan"):
        return manifest["plan"]
    part = Path(str(output) + ".part")
    if output.exists():
        raise ValueError("目标文件已存在且没有可核验的完成记录，未覆盖。")
    if part.exists():
        if manifest is None:
            raise ValueError("存在未识别的临时文件，未覆盖。请改用新任务。")
        # A matching preparation marker proves this is our uncommitted skeleton.
        part.unlink()
    manifest = {"signature": store.signature, "identity": source.identity, "config": config, "plan": None}
    store.save_manifest(manifest)
    remote = RemoteFile(source)
    with remote_hdf5(remote) as h5:
        h5._subset_remote_reader = remote
        h5._subset_progress = progress
        h5._subset_check = source.check
        plan = make_plan(h5, polygon, config["frequencies"], config["polarizations"],
                         config["min_height"], config["max_height"])
        progress(phase="创建子集文件", downloaded=0, total=plan["total"], speed=0,
                 windows=plan["windows"], chunks=len(plan["chunks"]))
        try:
            create_subset(h5, part, plan, config["frequencies"], config["polarizations"], polygon, source.check)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    store.save_manifest({**manifest, "plan": plan})
    return plan


def transfer_chunks(source, part, plan, store, done, config, progress):
    progress(phase="检查续传数据块", total=plan["total"], downloaded=0, speed=0)
    with h5py.File(part, "r+") as h5:
        invalid = []
        for chunk in plan["chunks"]:
            source.check()
            key = chunk["key"]
            if key not in done:
                continue
            try:
                _, raw = h5[chunk["path"]].id.read_direct_chunk(tuple(chunk["dest"]))
                valid = hashlib.sha256(raw).hexdigest() == done[key]
            except (RuntimeError, OSError):
                valid = False
            if not valid:
                del done[key]
                invalid.append(key)
        store.update(remove=invalid)
        completed = sum(c["size"] for c in plan["chunks"] if c["key"] in done)
        initial, started = completed, time.monotonic()
        ranges = iter(group_ranges([c for c in plan["chunks"] if c["key"] not in done]))
        progress(phase="并发下载", downloaded=completed, total=plan["total"], speed=0,
                 windows=plan["windows"], chunks=len(plan["chunks"]))
        futures = {}
        def submit():
            while len(futures) < source.connections * 2:
                group = next(ranges, None)
                if group is None:
                    break
                futures[source.submit(source.read, group["start"], group["end"])] = group
        try:
            submit()
            while futures:
                source.check()
                ready, _ = wait(futures, timeout=.25, return_when=FIRST_COMPLETED)
                for future in ready:
                    group = futures.pop(future)
                    data, committed = future.result(), {}
                    for chunk in group["chunks"]:
                        offset = chunk["offset"] - group["start"]
                        raw = data[offset:offset + chunk["size"]]
                        h5[chunk["path"]].id.write_direct_chunk(tuple(chunk["dest"]), raw, chunk["filter_mask"])
                        committed[chunk["key"]] = hashlib.sha256(raw).hexdigest()
                        completed += chunk["size"]
                    h5.flush()
                    store.update(committed)
                    done.update(committed)
                    progress(phase="并发下载", downloaded=completed, total=plan["total"],
                             speed=(completed-initial)/max(.01, time.monotonic()-started))
                submit()
        finally:
            for future in futures:
                future.cancel()
            h5.flush()
        source.check()
        progress(phase="校验成品", downloaded=completed, total=plan["total"], speed=0)
        verify_and_stamp(h5, plan, done, store.signature, source.identity, config, source.check)


def download_subset(url, corners, output, credentials=None, frequencies=("A",), polarizations=("HH",),
                    connections=6, stop=None, progress=lambda **kw: None, min_height=None, max_height=None):
    polygon = validate_corners(corners)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    config = {"url": url.split("?", 1)[0], "corners": corners, "frequencies": list(frequencies),
              "polarizations": list(polarizations), "min_height": min_height, "max_height": max_height}
    signature = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    with RangeSource(url, credentials, stop, progress=progress, connections=connections) as source:
        progress(phase="连接与读取元数据", downloaded=0, total=0, speed=0)
        identity = source.probe()
        legacy_path = Path(str(output) + ".progress.json")
        legacy = json.loads(legacy_path.read_text(encoding="utf-8")) if legacy_path.exists() else None
        if legacy:
            validate_resume(legacy, signature, identity)
        finished = completion_info(output) if output.exists() else None
        if finished:
            if finished["signature"] != signature:
                raise ValueError("同名成品属于其他裁剪范围或极化，未覆盖。请使用其他保存目录。")
            if finished["identity"] != identity:
                raise ValueError("续传配置或远端文件标识变化，请创建新任务。")
            total = finished["total"]
        else:
            with ProgressStore(str(output) + ".progress.sqlite", signature) as store:
                manifest = store.manifest()
                if manifest is None and legacy:
                    store.save_manifest(legacy)
                    manifest = legacy
                if manifest:
                    validate_resume(manifest, signature, identity)
                if legacy:
                    legacy_path.unlink()  # SQLite commits plan and old hashes before removing JSON.
                plan = prepare_plan(source, polygon, output, config, store, manifest, progress)
                done = store.load()
                part = Path(str(output) + ".part")
                if output.exists():
                    # A legacy published file must match every saved hash before it is adopted.
                    progress(phase="校验成品", total=plan["total"], speed=0)
                    with h5py.File(output, "r+") as h5:
                        verify_and_stamp(h5, plan, done, signature, identity, config, source.check)
                else:
                    if not part.exists():
                        raise ValueError("缺少临时影像文件，不能续传。")
                    transfer_chunks(source, part, plan, store, done, config, progress)
                    source.check()
                    rename_finished(part, output)
                total = plan["total"]
        progress(phase="清理下载记录", downloaded=total, total=total, speed=0)
        cleanup_download_cache(output)
        progress(phase="已完成", downloaded=total, total=total, speed=0)
        return str(output)
