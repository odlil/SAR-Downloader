"""Isolated sardem/GDAL engine. Stdout is a JSON-lines progress protocol."""
from __future__ import annotations

import argparse
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile

from dem_sources import SOURCES, GEOID_URLS, nisar_vrt

ROOT = Path(__file__).resolve().parent
if not getattr(sys, 'frozen', False) and (ROOT / '.runtime' / 'dem-python').is_dir():
    sys.path.insert(0, str(ROOT / '.runtime' / 'dem-python'))


def emit(**values):
    print(json.dumps(values, ensure_ascii=True, allow_nan=False), flush=True)


def engine():
    try:
        from osgeo import gdal, osr
        from sardem import cop_dem
        version = metadata.version('sardem')
        if tuple(map(int, version.split('.')[:2])) < (0, 14):
            raise RuntimeError('需要 sardem 0.14 或更新版本，请运行配置DEM环境.cmd。')
        gdal.UseExceptions()
        if getattr(sys, 'frozen', False):
            gdal_data, proj_data = ROOT / 'gdal_data', ROOT / 'proj_data'
        else:
            share = Path(sys.prefix) / ('Library/share' if sys.platform == 'win32' else 'share')
            gdal_data, proj_data = share / 'gdal', share / 'proj'
        if gdal_data.is_dir():
            gdal.SetConfigOption('GDAL_DATA', str(gdal_data))
        if (proj_data / 'proj.db').is_file():
            osr.SetPROJSearchPaths([str(proj_data)])
        gdal.SetCacheMax(128 * 1024 * 1024)
        gdal.SetConfigOption('GDAL_NUM_THREADS', '2')
        gdal.SetConfigOption('PROJ_NETWORK', 'OFF')
        gdal.SetConfigOption('GDAL_HTTP_CONNECTTIMEOUT', '15')
        gdal.SetConfigOption('GDAL_HTTP_TIMEOUT', '60')
        gdal.SetConfigOption('GDAL_HTTP_MAX_RETRY', '3')
        gdal.SetConfigOption('GDAL_HTTP_RETRY_DELAY', '2')
        for name in ('GTiff', 'ISCE', 'VRT'):
            if not gdal.GetDriverByName(name):raise RuntimeError('GDAL 缺少 ' + name + ' 驱动。')
        return gdal, osr, cop_dem, version
    except ImportError:
        raise RuntimeError('DEM 需要 sardem 和 GDAL，请先运行配置DEM环境.cmd，或使用完整的新版免安装包。') from None


def download_asset(url, cache, progress, auth=None):
    """Cache original tiles with the same validated HTTP Range engine as SAR."""
    from downloads import DownloadManager
    from earthdata_auth import ScopedSession, TransferError
    import requests
    class Anonymous:
        def status(self):return {'loggedIn': False, 'username': '', 'burstReady': False}
    cache.mkdir(parents=True, exist_ok=True)
    manager = DownloadManager(Anonymous(), cache / 'state')
    job = manager._new_job({'url': url})
    target = cache / job['filename']
    if target.is_file():
        manager.close()
        return target
    job.update(output=str(cache), target=str(target))
    def update(record, **values):
        record.update(values)
        ratio = record.get('completed', 0) / record['total'] if record.get('total') else 0
        progress(min(1, ratio))
    manager._update = update
    session = auth.session() if auth is not None else ScopedSession()
    try:
        for attempt in range(4):
            try:
                manager._transfer(job, threading.Event(), session)
                progress(1)
                return target
            except (requests.RequestException, ConnectionError, TimeoutError):
                if attempt == 3:raise TransferError('公共数据下载中断；断点已保留，请重试。') from None
                time.sleep(2 ** attempt)
    finally:
        session.close();manager.close()


def validate_raster(gdal, path, *, full=True):
    dataset = gdal.Open(str(path))
    if not dataset or dataset.RasterCount != 1 or dataset.RasterXSize <= 0 or dataset.RasterYSize <= 0:
        raise RuntimeError('栅格文件不完整：' + Path(path).name)
    if full:dataset.GetRasterBand(1).Checksum()
    dataset = None


def strict_transform(osr, grid, longitude, latitude, height='egm2008'):
    paths = [str(grid.parent), *osr.GetPROJSearchPaths()]
    osr.SetPROJSearchPaths(list(dict.fromkeys(paths)))
    source, target = osr.SpatialReference(), osr.SpatialReference()
    source.SetFromUserInput('EPSG:4326+' + ('5773' if height == 'egm96' else '3855'));target.ImportFromEPSG(4979)
    for value in (source, target):value.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    options = osr.CoordinateTransformationOptions();options.SetBallparkAllowed(False)
    transformation = osr.CreateCoordinateTransformation(source, target, options)
    result = transformation.TransformPoint(longitude, latitude, 0)
    if not all(math.isfinite(value) for value in result) or abs(result[2]) > 150:
        raise RuntimeError(height.upper() + ' 高程转换不可用，未生成伪装为 WGS84 的结果。')
    return transformation


def verify_heights(gdal, transformation, source, output):
    """Check the result against a strict geoid operation, catching silent fallback."""
    input_ds, output_ds = gdal.Open(str(source)), gdal.Open(str(output))
    inverse = gdal.InvGeoTransform(input_ds.GetGeoTransform())
    transform = output_ds.GetGeoTransform()
    checked = 0
    for fx, fy in ((.5,.5),(.2,.2),(.8,.8),(.2,.8),(.8,.2)):
        x, y = min(output_ds.RasterXSize-1, int(output_ds.RasterXSize*fx)), min(output_ds.RasterYSize-1, int(output_ds.RasterYSize*fy))
        lon, lat = gdal.ApplyGeoTransform(transform, x+.5, y+.5)
        sx, sy = gdal.ApplyGeoTransform(inverse, lon, lat)
        sx, sy = int(math.floor(sx)), int(math.floor(sy))
        if not (0 <= sx < input_ds.RasterXSize and 0 <= sy < input_ds.RasterYSize):continue
        original = struct.unpack('f', input_ds.GetRasterBand(1).ReadRaster(sx,sy,1,1,buf_type=gdal.GDT_Float32))[0]
        actual = struct.unpack('f', output_ds.GetRasterBand(1).ReadRaster(x,y,1,1,buf_type=gdal.GDT_Float32))[0]
        if not math.isfinite(original) or original == input_ds.GetRasterBand(1).GetNoDataValue():continue
        expected = transformation.TransformPoint(lon,lat,original)[2]
        if not math.isfinite(actual) or abs(actual-expected) > 1:
            raise RuntimeError('WGS84 高程抽样校验失败；结果未发布，原始瓦片缓存保留。')
        checked += 1
    if not checked:raise RuntimeError('输出区域没有可校验的有效高程像元，请检查数据覆盖。')
    input_ds = output_ds = None


def create_product(plan, files, stage, grid=None):
    gdal, osr, cop_dem, version = engine()
    from sardem import utils
    bounds = plan['paddedBounds']
    stage.mkdir(parents=True, exist_ok=True)
    source_key = plan.get('source', 'COP')
    source_height = SOURCES[source_key]['height']
    source_srs = {'egm96': 'EPSG:4326+5773', 'egm2008': 'EPSG:4326+3855', 'ellipsoid': 'EPSG:4979'}[source_height]
    # Signed NISAR URLs must stay inside /vsimem, never in a disk VRT.
    vrt = files if isinstance(files, str) else stage.parent / 'source.vrt'
    if not isinstance(files, str):
        source = gdal.BuildVRT(str(vrt), list(map(str, files)), options=gdal.BuildVRTOptions(
            resolution='highest', outputBounds=utils.align_bounds_to_pixel_grid(bounds),
            outputSRS=source_srs, VRTNodata=-32767))
        if not source:raise RuntimeError('原始瓦片无法拼接。')
        source.FlushCache();source = None
    transformation = None
    if plan['height'] == 'ellipsoid' and source_height != 'ellipsoid':
        if grid is None:raise RuntimeError('缺少 ' + source_height.upper() + ' 网格，不能进行椭球高转换。')
        transformation = strict_transform(osr, grid, (bounds[0]+bounds[2])/2, (bounds[1]+bounds[3])/2, source_height)
    output = stage / ('elevation.tif' if plan['format'] == 'GTiff' else 'elevation.dem')
    original_options, original_progress = gdal.WarpOptions, gdal.TermProgress
    last = [0.0]
    def progress(fraction, message='', data=None):
        now = time.monotonic()
        if now-last[0] > .4 or fraction >= 1:
            emit(progress=.7+.25*fraction, phase='拼接、裁剪和高程处理')
            last[0] = now
        return 1
    def bounded_options(*args, **kwargs):
        kwargs.update(warpMemoryLimit=128, warpOptions=['NUM_THREADS=2'], dstNodata=-32767)
        if kwargs.get('format') == 'GTiff':
            kwargs['creationOptions'] = ['TILED=YES','COMPRESS=DEFLATE','PREDICTOR=3','BIGTIFF=IF_SAFER']
        return original_options(*args, **kwargs)
    gdal.WarpOptions, gdal.TermProgress = bounded_options, progress
    try:
        if source_key == 'COP':
            cop_dem.download_and_stitch(str(output), bounds, keep_egm=plan['height']=='egm2008',
                                       vrt_filename=str(vrt), output_format='GTiff' if plan['format']=='GTiff' else 'ISCE', output_type='float32')
        else:
            result = gdal.Warp(str(output), str(vrt), options=gdal.WarpOptions(
                format=plan['format'], outputBounds=utils.align_bounds_to_pixel_grid(bounds),
                srcSRS=source_srs, dstSRS='EPSG:4979' if plan['height']=='ellipsoid' else source_srs,
                xRes=1/3600, yRes=1/3600, outputType=gdal.GDT_Float32, resampleAlg='near', callback=progress))
            if result is None:raise RuntimeError('DEM 裁剪未生成有效结果。')
            result.FlushCache();result = None
    finally:
        gdal.WarpOptions, gdal.TermProgress = original_options, original_progress
    validate_raster(gdal, output)
    if transformation:verify_heights(gdal, transformation, vrt, output)
    dataset = gdal.Open(str(output), gdal.GA_Update)
    datum = osr.SpatialReference();datum.SetFromUserInput('EPSG:4979' if plan['height']=='ellipsoid' else source_srs)
    dataset.SetProjection(datum.ExportToWkt())
    dataset.SetMetadataItem('VERTICAL_DATUM', 'WGS84 ellipsoid' if plan['height']=='ellipsoid' else source_height.upper())
    dataset.SetMetadataItem('DEM_SOURCE', SOURCES[source_key]['label'] + (' (DSM)' if source_key=='COP' else ''))
    dataset.FlushCache();dataset = None
    if plan['format'] == 'ISCE':
        result = gdal.Translate(str(stage / 'elevation.dem.vrt'), str(output), format='VRT')
        result.FlushCache();result = None
        # ISCE otherwise defaults a DEM without REFERENCE to EGM96.
        xml=Path(str(output)+'.xml');document=ET.parse(xml);root=document.getroot()
        property=ET.SubElement(root,'property',name='REFERENCE')
        ET.SubElement(property,'value').text='WGS84' if plan['height']=='ellipsoid' else source_height.upper()
        document.write(xml,encoding='utf-8',xml_declaration=True)
    return output, version


def srtm_raster(archive, tile):
    """Read the expected HGT member through GDAL, without extracting paths."""
    with zipfile.ZipFile(archive) as zipped:
        members = [item for item in zipped.infolist() if item.filename == tile['id']+'.hgt']
        if len(members) != 1 or members[0].file_size != 3601*3601*2:
            raise RuntimeError('SRTM 压缩包缺少有效的 3601×3601 HGT 瓦片。')
        # GDAL reads the ZIP member directly; the ZIP CRC is checked separately.
        with zipped.open(members[0]) as stream:
            while stream.read(1024*1024):pass
    return '/vsizip/' + archive.as_posix() + '/' + tile['id'] + '.hgt'


def run(request, auth=None):
    from auxiliary_api import preview_dem
    gdal, _, _, _ = engine()
    work, target, cache = (Path(request[key]).resolve() for key in ('work', 'target', 'cache'))
    if target.exists():raise RuntimeError('结果目录已存在，未覆盖。')
    emit(progress=.01, phase='核对 DEM 数据源和范围')
    plan = preview_dem(request['plan'])
    key = plan['source']
    if key != 'COP' and (auth is None or not auth.status()['loggedIn']):
        from earthdata_auth import AuthError
        raise AuthError('请先在个人中心登录 NASA Earthdata。')
    if key == 'NISAR':
        gdal.PushErrorHandler('CPLQuietErrorHandler')
        session, memory = auth.session(), []
        try:
            source, memory, assets = nisar_vrt(gdal, session, plan['paddedBounds'],
                lambda phase: emit(progress=.15, phase=phase))
            return publish_product(plan, source, request, assets)
        finally:
            for path in memory:gdal.Unlink(path)
            session.close();gdal.PopErrorHandler()
    tiles = [tile for tile in plan['tiles'] if tile['available']]
    if not tiles:raise RuntimeError('当前范围没有可下载的 DEM 瓦片。')
    files = []
    for index, tile in enumerate(tiles):
        def progress(ratio):emit(progress=.03+.57*(index+ratio)/len(tiles), phase=f'缓存原始瓦片 {index+1}/{len(tiles)}')
        path = download_asset(tile['url'], cache / 'tiles', progress, auth if key=='SRTM' else None)
        raster = srtm_raster(path, tile) if key=='SRTM' else path
        validate_raster(gdal, raster)
        files.append(raster)
    return publish_product(plan, files, request, [tile['filename'] for tile in tiles])


def publish_product(plan, files, request, assets):
    gdal, _, _, _ = engine()
    work, target, cache = (Path(request[key]).resolve() for key in ('work', 'target', 'cache'))
    source_height = SOURCES[plan['source']]['height']
    grid = None
    if plan['height'] == 'ellipsoid' and source_height != 'ellipsoid':
        emit(progress=.61, phase='准备 ' + source_height.upper() + ' 高程转换网格')
        grid = download_asset(GEOID_URLS[source_height], cache / 'geoids', lambda ratio: emit(progress=.61+.08*ratio, phase='缓存高程转换网格'))
        validate_raster(gdal, grid, full=False)
    stage = work / 'result'
    if stage.exists():
        if not stage.resolve().is_relative_to(work) or stage.is_symlink():raise RuntimeError('临时目录无效。')
        for entry in stage.rglob('*'):
            if entry.is_symlink() or getattr(entry.stat(follow_symlinks=False),'st_file_attributes',0)&1024:raise RuntimeError('临时目录含重解析点。')
        shutil.rmtree(stage)
    output, version = create_product(plan, files, stage, grid)
    report = {key: plan[key] for key in ('bounds','paddedBounds','buffer','height','format','missing','warnings')}
    report.update(sardem=version, source=SOURCES[plan['source']]['label'], dataSource=plan['source'], files=assets,
                  sourceHeight=source_height, geoid=grid.name if grid else None, verticalConversionValidated=bool(grid),
                  heightAlreadyEllipsoidal=source_height=='ellipsoid', sourceCoverageValidated=True)
    (stage/'processing.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    emit(progress=.98, phase='校验完成，发布结果')
    target.parent.mkdir(parents=True, exist_ok=True)
    # Rename an entire result directory so GeoTIFF/ISCE companions stay together.
    os.rename(stage, target)
    validate_raster(gdal, target/output.name, full=False)
    emit(progress=1, phase='处理完成', done=True)


def self_test(folder):
    """Offline packaging check: real mosaic/crop, zero heights and ISCE XML."""
    gdal, osr, _, version = engine()
    folder.mkdir(parents=True, exist_ok=False)
    step=1/3600;files=[]
    for index,height in enumerate((0.,100.)):
        file=folder/f'tile{index}.tif';files.append(file)
        raster=gdal.GetDriverByName('GTiff').Create(str(file),10,10,1,gdal.GDT_Float32)
        raster.SetGeoTransform((86+index*10*step,step,0,43.01,0,-step))
        srs=osr.SpatialReference();srs.ImportFromEPSG(4326);raster.SetProjection(srs.ExportToWkt())
        raster.GetRasterBand(1).WriteRaster(0,0,10,10,struct.pack('100f',*([height]*100)))
        raster.FlushCache();raster=None
    plan={'paddedBounds':[86+2*step,43.01-8*step,86+18*step,43.01-2*step], 'height':'egm2008'}
    for format in ('GTiff','ISCE'):
        output,_=create_product({**plan,'format':format},files,folder/format/'result')
        published=folder/('published-'+format);output.parent.rename(published)
        raster=gdal.Open(str(published/output.name));band=raster.GetRasterBand(1)
        values=struct.unpack(f'{raster.RasterXSize*raster.RasterYSize}f',band.ReadRaster(buf_type=gdal.GDT_Float32))
        if not 0. in values or not 100. in values:raise RuntimeError('DEM 拼接/零高程校验失败。')
        raster=None
        if format=='ISCE':validate_raster(gdal,published/'elevation.dem.vrt')
    emit(ok=True,frozen=bool(getattr(sys,'frozen',False)),sardem=version,mosaic=True,crop=True,ISCE=True)


def main():
    from https_runtime import configure_https
    configure_https()
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--run', type=Path)
    parser.add_argument('--self-test', type=Path)
    args = parser.parse_args()
    try:
        if args.check:
            from https_runtime import ca_bundle, https_context
            gdal, _, _, version = engine()
            emit(ok=True, sardem=version, gdal=gdal.VersionInfo(), frozen=bool(getattr(sys,'frozen',False)),
                 https={'caBundle': ca_bundle(), 'rootCertificates': https_context().cert_store_stats()['x509_ca']})
        elif args.self_test:self_test(args.self_test)
        elif args.run:
            request = json.loads(args.run.read_text(encoding='utf-8'))
            auth = None
            if request.get('plan', {}).get('source', 'COP') != 'COP':
                from earthdata_auth import EarthdataAuth
                auth = EarthdataAuth()
                auth._restore(json.loads(sys.stdin.readline(1024*1024)))
            try:run(request, auth)
            finally:
                if auth:auth.close()
        else:parser.error('Specify --check or --run.')
        return 0
    except Exception as error:
        # GDAL messages may include temporary CDN signatures. Never persist them.
        message = re.sub(r'https?://[^\s\x27\x22<>]+', '[官方数据地址]', str(error))[:1000]
        emit(error=message, ok=False, authRequired=type(error).__name__=='AuthError')
        return 1


if __name__ == '__main__':raise SystemExit(main())
