"""DEM source metadata and authenticated, bounded NISAR VRT access.

NISAR's large COGs are read by HTTP Range through short-lived signed URLs.
The rewritten VRT tree exists only in GDAL memory, never in task manifests.
"""
from __future__ import annotations

import math
import re
from urllib.parse import urljoin, urlsplit
import uuid
import xml.etree.ElementTree as ET

SOURCES = {
    'COP': {'label': 'Copernicus GLO-30 Public', 'short': 'CopDEM', 'height': 'egm2008'},
    'NISAR': {'label': 'NISAR DEM v1.2', 'short': 'NISAR_DEM', 'height': 'ellipsoid'},
    'SRTM': {'label': 'SRTMGL1 V003', 'short': 'SRTM', 'height': 'egm96'},
}
NISAR_BASE = 'https://nisar.asf.earthdatacloud.nasa.gov/NISAR/DEM/v1.2/EPSG4326/'
SRTM_BASE = 'https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/SRTMGL1.003/'
GEOID_URLS = {'egm2008': 'https://cdn.proj.org/us_nga_egm08_25.tif',
              'egm96': 'https://cdn.proj.org/us_nga_egm96_15.tif'}


def protected_dem_url(url):
    if not isinstance(url, str):return False
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return False
    try:
        if parsed.port not in (None, 443):return False
    except ValueError:return False
    if parsed.hostname == 'data.lpdaac.earthdatacloud.nasa.gov':
        return bool(re.fullmatch(r'/lp-prod-protected/SRTMGL1\.003/([NS]\d{2}[EW]\d{3}\.SRTMGL1\.hgt)/\1\.zip', parsed.path))
    if parsed.hostname == 'nisar.asf.earthdatacloud.nasa.gov':
        path = parsed.path.removeprefix('/NISAR/DEM/v1.2/EPSG4326/')
        return (path != parsed.path and all(re.fullmatch(r'[A-Za-z0-9_-]+(?:\.(?:vrt|tif))?', part)
                                            for part in path.split('/')) and path.endswith(('.vrt', '.tif')))
    return False


def needs_auth(job):
    return (job.get('kind') in ('sar', 'orbit', 'nisar-subset') or
            job.get('kind') == 'dem-process' and job.get('plan', {}).get('source', 'COP') != 'COP')


def srtm_tiles(bounds):
    w, s, e, n = bounds
    result = []
    for y in range(math.floor(s), math.ceil(n)):
        for x in range(math.floor(w), math.ceil(e)):
            name = f"{'N' if y >= 0 else 'S'}{abs(y):02d}{'E' if x >= 0 else 'W'}{abs(x):03d}"
            stem = name + '.SRTMGL1.hgt'
            result.append({'id': name, 'bounds': [x, y, x+1, y+1], 'available': True,
                           'filename': stem + '.zip', 'url': SRTM_BASE + stem + '/' + stem + '.zip'})
    return result


def nisar_vrt(gdal, session, bounds, progress=lambda text: None):
    """Prune official VRTs to the AOI and replace COG paths with scoped signed URLs.

    Return (in-memory root VRT, all in-memory paths, original asset URLs).
    No Earthdata headers are passed to GDAL or to the CDN.
    """
    from earthdata_auth import AuthError, TransferError, validate_url
    memory, assets, seen = [], [], {}
    prefix = '/vsimem/sar-nisar-' + uuid.uuid4().hex + '-'

    def visit(url, depth=0):
        if url in seen:return seen[url]
        if depth > 8 or len(seen) > 512 or not protected_dem_url(url):
            raise TransferError('NISAR DEM 索引路径或层级无效。')
        progress('解析 NISAR DEM 分层索引')
        with session.request('GET', url, stream=True) as response:
            if response.status_code in (401, 403):raise AuthError('NISAR DEM 认证失效，请重新登录 Earthdata。')
            if response.status_code != 200:raise TransferError(f'NISAR DEM 索引不可用（HTTP {response.status_code}）。')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 8*1024*1024:raise TransferError('NISAR DEM 索引超过允许大小。')
                chunks.append(chunk)
        try:
            document = ET.fromstring(b''.join(chunks))
            gt = [float(x) for x in document.findtext('GeoTransform').split(',')]
            if len(gt) != 6 or gt[1] <= 0 or gt[5] >= 0 or gt[2] or gt[4]:raise ValueError()
        except (ET.ParseError, TypeError, ValueError, AttributeError):
            raise TransferError('NISAR DEM 返回了无效索引或登录页面。') from None
        path = prefix + str(len(seen)) + '.vrt'
        seen[url] = path
        for band in document.findall('VRTRasterBand'):
            for source in list(band):
                filename = source.find('SourceFilename')
                if filename is None:continue
                rect = source.find('DstRect')
                if rect is None:raise TransferError('NISAR DEM 索引缺少瓦片范围。')
                x, y, width, height = (float(rect.get(k)) for k in ('xOff','yOff','xSize','ySize'))
                box = [gt[0]+x*gt[1], gt[3]+(y+height)*gt[5], gt[0]+(x+width)*gt[1], gt[3]+y*gt[5]]
                # Keep a one-pixel margin for edge resampling.
                if (box[2] < bounds[0]-gt[1] or box[0] > bounds[2]+gt[1] or
                    box[3] < bounds[1]+gt[5] or box[1] > bounds[3]-gt[5]):
                    band.remove(source);continue
                remote = urljoin(url, (filename.text or '').removeprefix('/vsicurl/'))
                if not protected_dem_url(remote):raise TransferError('NISAR DEM 索引包含非官方数据地址。')
                if remote.endswith('.vrt'):
                    filename.text = visit(remote, depth+1)
                else:
                    progress('验证 NISAR DEM 高程瓦片')
                    with session.request('GET', remote, stream=True) as response:
                        if response.status_code in (401,403):raise AuthError('NISAR DEM 瓦片需要重新登录 Earthdata。')
                        if response.status_code != 200:raise TransferError(f'NISAR DEM 瓦片不可用（HTTP {response.status_code}）。')
                        validate_url(response.url, redirect_from=remote)
                        filename.text = '/vsicurl/' + response.url
                    assets.append(remote)
                filename.set('relativeToVRT', '0')
        gdal.FileFromMemBuffer(path, ET.tostring(document, encoding='utf-8'))
        memory.append(path)
        return path
    try:
        root = visit(NISAR_BASE + 'EPSG4326.vrt')
        if not assets:raise TransferError('当前范围没有 NISAR DEM 高程数据。')
        return root, memory, list(dict.fromkeys(assets))
    except Exception:
        for path in memory:gdal.Unlink(path)
        raise
