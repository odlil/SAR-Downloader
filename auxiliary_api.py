"""Public Sentinel-1 orbit catalogs and Copernicus DEM coverage (no credentials)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import unquote, urlsplit

import requests
from earthdata_auth import ScopedSession, TransferError
from dem_sources import SOURCES, srtm_tiles

ORBIT_BASE = 'https://s1qc.asf.alaska.edu/'
COP_BASE = 'https://copernicus-dem-30m.s3.amazonaws.com/'
SCENE = re.compile(r'^(S1[A-D])_(?:IW|EW|SM|WV)_[A-Z0-9_]+_(\d{8}T\d{6})_(\d{8}T\d{6})_\d{6}_[A-Z0-9]+_[A-Z0-9]+$', re.I)
ORBIT = re.compile(r'^(S1[A-D])_OPER_AUX_(POEORB|RESORB)_OPOD_(\d{8}T\d{6})_V(\d{8}T\d{6})_(\d{8}T\d{6})\.EOF$')
COP_TILE = re.compile(r'^Copernicus_DSM_COG_10_([NS])(\d{2})_00_([EW])(\d{3})_00_DEM$')
_cache, _lock = {}, threading.RLock()


class AuxiliaryError(TransferError):
    status = 400


def _stamp(text):
    return datetime.strptime(text, '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc)


def _public_text(url, limit=16 * 1024 * 1024):
    session = ScopedSession()
    try:
        with session.request('GET', url, stream=True) as response:
            if response.status_code != 200:
                raise AuxiliaryError(f'公共数据源暂不可用（HTTP {response.status_code}），请稍后重试。')
            data, count = [], 0
            for chunk in response.iter_content(65536):
                count += len(chunk)
                if count > limit:
                    raise AuxiliaryError('公共数据目录超过允许大小，请稍后重试。')
                data.append(chunk)
            return b''.join(data).decode('utf-8')
    except (requests.RequestException, UnicodeError):
        raise AuxiliaryError('无法读取公共数据目录，请检查网络后重试。') from None
    finally:
        session.close()


def _cached(key, loader, ttl=600):
    # Only one thread fetches a given public index at a time.
    with _lock:
        if key not in _cache or time.monotonic() - _cache[key][0] > ttl:
            _cache[key] = (time.monotonic(), loader())
        return _cache[key][1]


def parse_scene(value):
    if not isinstance(value, str) or len(value) > 240:
        raise AuxiliaryError('影像名称无效。')
    name = re.sub(r'\.(zip|SAFE)$', '', value, flags=re.I)
    match = SCENE.fullmatch(name)
    if not match:
        raise AuxiliaryError('需要标准命名的 Sentinel-1 ZIP / SAFE 影像。')
    try:
        start, end = _stamp(match[2]), _stamp(match[3])
    except ValueError:
        raise AuxiliaryError('影像名称中的获取日期无效。') from None
    if start > end or (end - start).total_seconds() > 86400:
        raise AuxiliaryError('影像获取时间区间无效。')
    return {'id': name, 'platform': match[1].upper(), 'start': match[2], 'end': match[3]}


def parse_acquisition(value):
    if isinstance(value, str):
        return parse_scene(value)
    if not isinstance(value, dict):
        raise AuxiliaryError('轨道匹配需要有效的影像获取时间。')
    platform, identifier = value.get('platform'), value.get('id')
    if not isinstance(platform, str) or not re.fullmatch(r'S1[A-D]', platform):
        raise AuxiliaryError('轨道匹配需要 Sentinel-1 卫星标识。')
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 240:
        raise AuxiliaryError('影像标识无效。')
    try:
        start = datetime.fromisoformat(value['start'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(value['end'].replace('Z', '+00:00'))
        if start.tzinfo is None or end.tzinfo is None or not 0 <= (end-start).total_seconds() <= 86400:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AuxiliaryError('缺少有效的影像开始/结束 UTC，不能按日期猜测轨道。') from None
    if end.microsecond:
        end = end.replace(microsecond=0) + timedelta(seconds=1)
    return {'id': identifier, 'platform': platform,
            'start': start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S'),
            'end': end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S')}


class OrbitLinks(HTMLParser):
    def __init__(self, kind):
        super().__init__();self.kind = kind;self.records = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'a':return
        href = dict(attrs).get('href', '')
        if urlsplit(href).scheme or '/' in href:return
        name = unquote(href)
        match = ORBIT.fullmatch(name)
        if not match or match[2] != self.kind:return
        try:
            if _stamp(match[4]) >= _stamp(match[5]):return
            _stamp(match[3])
        except ValueError:return
        self.records[name] = {'filename': name, 'platform': match[1], 'type': match[2],
                              'created': match[3], 'start': match[4], 'end': match[5],
                              'url': ORBIT_BASE + ('aux_poeorb/' if match[2] == 'POEORB' else 'aux_resorb/') + name}


def orbit_catalog(kind):
    def load():
        parser = OrbitLinks(kind)
        parser.feed(_public_text(ORBIT_BASE + ('aux_poeorb/' if kind == 'POEORB' else 'aux_resorb/')))
        if not parser.records:
            raise AuxiliaryError('轨道目录未返回可识别的 EOF 文件，请稍后重试。')
        return list(parser.records.values())
    return _cached('orbit:' + kind, load)


def match_orbits(payload):
    if not isinstance(payload, dict) or payload.get('strategy', 'auto') not in ('auto', 'precise', 'restituted'):
        raise AuxiliaryError('轨道匹配策略无效。')
    names = payload.get('scenes')
    if not isinstance(names, list) or not 1 <= len(names) <= 1000:
        raise AuxiliaryError('一次可匹配 1–1000 景 Sentinel-1 影像。')
    scenes = list({item['id']: item for item in map(parse_acquisition, names)}.values())
    strategy = payload.get('strategy', 'auto')
    matches = {}
    for kind in (('POEORB', 'RESORB') if strategy == 'auto' else ('POEORB',) if strategy == 'precise' else ('RESORB',)):
        remaining = [scene for scene in scenes if scene['id'] not in matches]
        if not remaining:break
        records = orbit_catalog(kind)
        for scene in remaining:
            choices = [record for record in records if record['platform'] == scene['platform']
                       and record['start'] <= scene['start'] and record['end'] >= scene['end']]
            if choices:
                matches[scene['id']] = max(choices, key=lambda item: (item['created'], item['filename']))
    results, assets = [], {}
    for scene in scenes:
        orbit = matches.get(scene['id'])
        results.append({**scene, 'state': 'matched' if orbit else 'missing', 'orbit': orbit,
                        'message': '' if orbit else '源站未找到覆盖完整获取时间的轨道；新影像可能尚未发布，恢复轨道历史覆盖有限。'})
        if orbit:
            assets[orbit['url']] = {'kind': 'orbit', 'url': orbit['url'], 'filename': orbit['filename'],
                                     'title': f"{orbit['platform']} · {orbit['type']} · {orbit['start'][:8]}"}
    return {'scenes': results, 'assets': list(assets.values()), 'matched': len(matches),
            'missing': len(results) - len(matches), 'files': len(assets), 'provider': 'ASF'}


def scan_scene_folder(folder):
    root = Path(folder)
    if root.name.lower().endswith('.safe'):
        try:return {'scenes': [parse_scene(root.name)], 'label': root.name, 'warnings': []}
        except AuxiliaryError:pass
    found, stack, visited, truncated = {}, [(root, 0)], 0, False
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 100000 or len(found) >= 1000:
                        truncated = True;stack.clear();break
                    if entry.is_symlink() or getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0) & 1024:continue
                    if entry.name.lower().endswith(('.zip', '.safe')):
                        try:
                            scene = parse_scene(entry.name);found[scene['id']] = scene
                            continue
                        except AuxiliaryError:pass
                    if entry.is_dir(follow_symlinks=False) and depth < 6:stack.append((Path(entry.path), depth+1))
        except (OSError, PermissionError):continue
    return {'scenes': list(found.values()), 'label': root.name,
            'warnings': ['扫描达到数量或目录深度限制，请按批次选择影像目录。'] if truncated else []}


def dem_plan(payload):
    if not isinstance(payload, dict):raise AuxiliaryError('DEM 参数无效。')
    source = payload.get('source', 'COP')
    if not isinstance(source, str) or source not in SOURCES:raise AuxiliaryError('DEM 数据源无效。')
    bounds = payload.get('bounds')
    if not isinstance(bounds, list) or len(bounds) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in bounds):
        raise AuxiliaryError('DEM 范围需要 WGS84 经纬度西、南、东、北四个值。')
    w, s, e, n = bounds
    if not -180 <= w < e <= 180 or not -85 <= s < n <= 85:
        raise AuxiliaryError('DEM 经纬度范围无效；跨日期变更线请拆分。')
    buffer = payload.get('buffer', 0)
    if isinstance(buffer, bool) or not isinstance(buffer, (int, float)) or not math.isfinite(buffer) or not 0 <= buffer <= 100:
        raise AuxiliaryError('缓冲距离需要为 0–100 km。')
    height, output_format = payload.get('height', 'ellipsoid'), payload.get('format', 'GTiff')
    if height not in ('ellipsoid', SOURCES[source]['height']) or output_format not in ('GTiff', 'ISCE', 'tiles'):
        raise AuxiliaryError('DEM 高程或输出格式无效。')
    if source != 'COP' and output_format == 'tiles':
        raise AuxiliaryError('NISAR DEM 和 SRTM 请使用 GeoTIFF 或 ISCE 输出。')
    if output_format == 'tiles':height = 'egm2008'
    lat_pad = buffer / 111.32
    lon_pad = lat_pad / max(.1, math.cos((s + n) * math.pi / 360))
    padded = [max(-180, w - lon_pad), max(-85, s - lat_pad), min(180, e + lon_pad), min(85, n + lat_pad)]
    if source == 'SRTM' and (padded[1] < -56 or padded[3] > 60):
        raise AuxiliaryError('SRTM 含缓冲范围须位于南纬 56°至北纬 60°；其他区域请选择 CopDEM 或 NISAR DEM。')
    count = (math.ceil(padded[2]) - math.floor(padded[0])) * (math.ceil(padded[3]) - math.floor(padded[1]))
    if count > 500:raise AuxiliaryError('DEM 超过 500 个瓦片，请缩小范围。')
    pixels = math.ceil((padded[2] - padded[0]) * 3600) * math.ceil((padded[3] - padded[1]) * 3600)
    if output_format != 'tiles' and pixels > 200_000_000:
        raise AuxiliaryError('DEM 输出超过 2 亿像元，请缩小范围或仅下载原始瓦片。')
    return {'bounds': list(bounds), 'paddedBounds': padded, 'buffer': buffer, 'height': height,
            'format': output_format, 'pixels': pixels, 'source': source}


def copdem_catalog():
    def load():
        records = {}
        for line in _public_text(COP_BASE + 'tileList.txt', 8 * 1024 * 1024).splitlines():
            name = line.strip().rstrip('/')
            match = COP_TILE.fullmatch(name)
            if not match:continue
            y = int(match[2]) * (1 if match[1] == 'N' else -1)
            x = int(match[4]) * (1 if match[3] == 'E' else -1)
            records[(x, y)] = name
        if not records:raise AuxiliaryError('CopDEM 目录未返回可识别瓦片。')
        return records
    return _cached('copdem:30m', load, 3600)


def preview_dem(payload):
    plan = dem_plan(payload)
    if plan['source'] != 'COP':
        tiles = srtm_tiles(plan['paddedBounds'])
        if plan['source'] == 'NISAR':
            tiles = [{key: tile[key] for key in ('id', 'bounds', 'available')} for tile in tiles]
        return {**plan, 'tiles': tiles, 'available': len(tiles), 'missing': [],
                'sourceLabel': SOURCES[plan['source']]['label'],
                'sourceHeight': SOURCES[plan['source']]['height'].upper(), 'coverageVerified': False,
                'warnings': ['范围已按数据源覆盖范围规划；需要登录 Earthdata，启动时核验源站实际数据。'
                             + (' SRTM 海域或缺失瓦片会明确报错，不填为零高程。' if plan['source'] == 'SRTM' else '')]}
    catalog = copdem_catalog()
    w, s, e, n = plan['paddedBounds']
    tiles, missing = [], []
    for y in range(math.floor(s), math.ceil(n)):
        for x in range(math.floor(w), math.ceil(e)):
            name = catalog.get((x, y))
            tile = {'id': f"{'N' if y >= 0 else 'S'}{abs(y):02d}_{'E' if x >= 0 else 'W'}{abs(x):03d}",
                    'bounds': [x, y, x + 1, y + 1], 'available': bool(name)}
            if name:tile.update(filename=name + '.tif', url=COP_BASE + name + '/' + name + '.tif')
            else:missing.append(tile['id'])
            tiles.append(tile)
    warnings = ['公共目录缺失的瓦片可能是海域或尚未公开区域；处理结果中缺失覆盖保留为 NoData，不推断为零高程。'] if missing else []
    return {**plan, 'tiles': tiles, 'available': sum(tile['available'] for tile in tiles), 'missing': missing,
            'warnings': warnings, 'sourceLabel': 'Copernicus GLO-30 Public', 'sourceHeight': 'EGM2008', 'coverageVerified': True}
