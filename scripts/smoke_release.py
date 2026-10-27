"""Validate the extracted Windows ZIP with no installed Python on its PATH."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROCESS_OPTIONS = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}


def clean_environment():
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(('PYTHON', 'CONDA', '_PYI', 'PYINSTALLER', 'SAR_'))
                   and key.upper() not in ('PROJ_LIB', 'PROJ_DATA', 'GDAL_DATA', 'TCL_LIBRARY', 'TK_LIBRARY', 'VIRTUAL_ENV', 'LD_LIBRARY_PATH',
                                           'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'GDAL_HTTP_CAINFO')}
    windows = Path(environment.get('SystemRoot', r'C:\Windows'))
    environment['PATH'] = str(windows / 'System32') + os.pathsep + str(windows) if sys.platform == 'win32' else ''
    environment['PYTHONUTF8'] = '1'
    return environment


def run_json(command, **options):
    result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace', timeout=60,
                            **PROCESS_OPTIONS, **options)
    if result.returncode:
        raise RuntimeError(result.stdout + '\n' + result.stderr)
    value = json.loads(result.stdout.strip().splitlines()[-1])
    if value.get('ok') is not True or value.get('frozen') is not True:
        raise RuntimeError('Frozen runtime check failed: ' + str(value))
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('--online', action='store_true', help='Also perform one small public ASF catalog search')
    parser.add_argument('--dem-cache', type=Path, help='Reuse this test cache for the optional real DEM integration')
    args = parser.parse_args()
    if sys.platform not in ('win32', 'linux'):
        parser.error('Run the portable smoke test natively on Windows or Linux.')
    parent = ROOT / '.runtime' / 'release-smoke'
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='解压 测试 ', dir=parent)).resolve()
    if not folder.is_relative_to(parent.resolve()) or folder.is_symlink():
        raise RuntimeError('Unexpected smoke-test directory.')
    process, reader = None, None
    try:
        if args.archive.name.endswith('.tar.gz'):
            with tarfile.open(args.archive) as archive:
                archive.extractall(folder, filter='data')
        else:
            with zipfile.ZipFile(args.archive) as archive:
                for member in archive.infolist():
                    if not (folder / member.filename).resolve().is_relative_to(folder):
                        raise ValueError('Unsafe ZIP member.')
                archive.extractall(folder)
        executable = folder / 'SAR' / ('SAR.exe' if sys.platform == 'win32' else 'SAR')
        environment = clean_environment()
        environment['TEMP'] = environment['TMP'] = str(folder)
        environment['TMPDIR'] = str(folder)
        native_arguments = [str(executable), '--self-check', '--native', '--picker-child']
        for suffix in ('.shp', '.kml', '.kmz'):
            fixture = ROOT / 'tests' / 'fixtures' / ('aoi' + suffix)
            if fixture.is_file():
                native_arguments += ['--aoi', str(fixture)]
        report = run_json(native_arguments, env=environment, cwd=folder)
        if sys.platform == 'linux':
            bundle = executable.parent
            installer = bundle / '设置应用图标.sh'
            assert installer.stat().st_mode & 0o111
            assert b'<svg' in (bundle / 'SAR.svg').read_bytes()
            desktop_home = folder / '图标测试用户'
            desktop_home.mkdir()
            data_home = desktop_home / '应用 数据'
            desktop_environment = {**environment, 'PATH': '/usr/bin:/bin',
                                   'HOME': str(desktop_home), 'XDG_DATA_HOME': str(data_home)}
            subprocess.run(['/bin/sh', str(installer)], env=desktop_environment, cwd=bundle,
                           check=True, capture_output=True, timeout=30)
            assert (data_home / 'applications/sar-downloader.desktop').read_bytes() == (bundle / 'SAR Downloader.desktop').read_bytes()
            assert (data_home / 'icons/hicolor/scalable/apps/sar-downloader.svg').read_bytes() == (bundle / 'SAR.svg').read_bytes()
            desktop_report = run_json(['/bin/sh', str(data_home / 'sar-downloader/launch.sh'), '--self-check'],
                                      env=environment, cwd=folder)
            assert desktop_report['build'] == report['build']
        engine = folder/'SAR'/'dem-engine'/('SAR-DEM.exe' if sys.platform == 'win32' else 'SAR-DEM')
        dem_report=run_json([str(engine),'--check'],env=environment,cwd=folder)
        for executable_report, resource_root in ((report, executable.parent/'_internal'), (dem_report, engine.parent/'_internal')):
            assert executable_report['https']['rootCertificates'] > 0
            assert Path(executable_report['https']['caBundle']).resolve().is_relative_to(resource_root.resolve())
        run_json([str(engine),'--self-test',str(folder/'DEM离线校验')],env=environment,cwd=folder)
        assert report['pickerChild'] is True, 'The frozen parent could not run its picker child.'
        assert report['aoi'], 'No AOI fixture was tested.'
        for result in report['aoi']:
            assert result['featureCount'] > 0
        run_json([str(executable), '--native-dialog', '--check'], env=environment, cwd=folder)
        state = folder / '隔离队列'
        process = subprocess.Popen([str(executable), '--no-browser', '--port', '0', '--state-dir', str(state)],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8',
                                   env=environment, cwd=folder, **PROCESS_OPTIONS)
        lines, captured = queue.Queue(), []
        def read():
            for line in process.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True);reader.start()
        deadline = time.monotonic() + 40
        base = None
        while time.monotonic() < deadline:
            try:line = lines.get(timeout=.5)
            except queue.Empty:continue
            if line is None:break
            captured.append(line)
            match = re.search(r'http://127\.0\.0\.1:\d+', line)
            if match:
                base = match.group();break
        if base is None:
            raise RuntimeError('Packaged server did not start:\n' + ''.join(captured))
        opener = build_opener(ProxyHandler({}))
        def request(path, payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            query = Request(base + path, data=data, headers={'Content-Type': 'application/json'})
            try:
                timeout = 120 if path.startswith(('/api/orbits/', '/api/dem/')) else 60 if path == '/api/search' else 10
                with opener.open(query, timeout=timeout) as response:
                    return response.read()
            except HTTPError as error:
                if path != '/api/search':
                    raise
                detail = error.read(4096).decode('utf-8', errors='replace')
                raise RuntimeError(f'Frozen catalog request failed (HTTP {error.code}): {detail}') from error
        health = json.loads(request('/api/health'))
        assert health['version'] == report['version'] and health['app'] == 'sar-downloader'
        assert health['revision'] == report['build']['revision']
        repeated = subprocess.run([str(executable), '--no-browser', '--port', '0', '--state-dir', str(state)],
                                  capture_output=True, encoding='utf-8', errors='replace', timeout=20,
                                  env=environment, cwd=folder, **PROCESS_OPTIONS)
        assert repeated.returncode == 0 and base in repeated.stdout, repeated.stdout + repeated.stderr
        assert process.poll() is None, 'Duplicate launch stopped the original service.'
        html = request('/')
        assert b'SAR' in html and b'id="help-button"' in html
        manual = request('/help/manual.pdf')
        assert manual.startswith(b'%PDF-'), 'Bundled user manual is missing or invalid.'
        assert request('/help/manual-data') == manual
        assert request('/help/download') == manual
        for path in ('/app.js', '/map.js', '/downloads-ui.js', '/preflight-ui.js', '/workspace-state.js', '/queue-view.js',
                     '/subset-dialog.js', '/cleanup-ui.js', '/basemap.js', '/search-controller.js', '/updates-ui.js', '/dem-settings.js', '/vendor/leaflet.js',
                     '/help/viewer.html', '/help/viewer.js', '/help/viewer.css', '/vendor/pdfjs/pdf.mjs', '/vendor/pdfjs/pdf.worker.mjs'):
            assert request(path), path
        updates = json.loads(request('/api/updates'))
        assert updates['currentVersion'] == report['version']
        assert updates['packageKind'] == ('windows-x64.zip' if sys.platform == 'win32' else 'linux-x86_64.tar.gz')
        for source, height in (('NISAR', 'ELLIPSOID'), ('SRTM', 'EGM96')):
            dem = json.loads(request('/api/dem/preview', {'bounds': [86, 43, 86.01, 43.01], 'buffer': 0,
                                                        'source': source, 'height': height.lower(), 'format': 'GTiff'}))
            assert dem['source'] == source and dem['sourceHeight'] == height and dem['available'] > 0
        added = json.loads(request('/api/downloads/add', {'tasks': [{'url': 'https://datapool.asf.alaska.edu/fixture/test.zip'}]}))
        assert added['added'] == 1
        try:
            request('/api/downloads/action', {'action': 'start'})
            raise AssertionError('Unauthenticated download was accepted.')
        except HTTPError as error:
            assert error.code == 401
        manifest = json.loads((state / 'downloads.json').read_text(encoding='utf-8'))
        assert len(manifest['jobs']) == 1 and not manifest.get('credentials')
        assert health['capabilities']['nisarDEM'] and health['capabilities']['simpleClippedName']
        request('/api/downloads/settings', {'output': str(folder/'影像'), 'concurrent': 2})
        subset = json.loads(request('/api/nisar/subsets/add', {
            'scenes': [{'id': 'NISAR_PORTABLE_RSLC', 'dataset': 'NISAR', 'product': 'RSLC',
                        'url': 'https://datapool.asf.alaska.edu/TEST/NISAR_PORTABLE_RSLC.h5'}],
            'corners': [[86.79, 43.12], [86.82, 43.12], [86.82, 43.10], [86.79, 43.10]],
            'includeDEM': False,
        }))
        assert any(task['filename'] == 'NISAR_PORTABLE_RSLC_clipped.h5' for task in subset['tasks'])
        crop = next(task for task in subset['tasks'] if task['kind'] == 'nisar-subset')
        assert Path(crop['target']).parent == folder / '影像' / 'rslc'
        summary = (folder / '影像' / 'summary.txt').read_text(encoding='utf-8')
        assert 'NISAR_PORTABLE_RSLC' in summary and '86.79 至 86.82' in summary and '待下载' in summary
        assert health['capabilities']['alos'] and health['capabilities']['unfinishedCleanup']
        alos = json.loads(request('/api/downloads/scene-bundle', {
            'scenes': [{'dataset': 'ALOS-2', 'name': 'ALOS2_PORTABLE_FIXTURE',
                        'url': 'https://datapool.asf.alaska.edu/L1.1/ALOS2/ALOS2_PORTABLE_FIXTURE.zip'}],
            'auxiliary': {'orbit': False, 'dem': False},
        }))
        assert alos['added'] == 1 and alos['bundle']['imagery'] == 1
        preview = json.loads(request('/api/cache/unfinished/preview', {}))
        subset_id = next(task['id'] for task in subset['tasks'] if task['kind'] == 'nisar-subset')
        cleared = json.loads(request('/api/cache/unfinished/clear', {'token': preview['token'], 'ids': [subset_id]}))
        assert cleared['cleanup']['filesRemoved'] == 0
        assert next(task for task in cleared['tasks'] if task['id'] == subset_id)['state'] == 'paused'
        if sys.platform == 'linux':
            saved = json.loads(request('/api/basemap', {'provider': 'esri', 'kind': 'satellite', 'key': 'portable-fixture-key'}))
            assert saved['keys']['esri'] == 'portable-fixture-key'
            assert b'portable-fixture-key' not in (state/'private-settings.bin').read_bytes()
            assert (state/'private-settings.key').stat().st_mode & 0o777 == 0o600
        scene_count = None
        catalog_counts = {}
        if args.online:
            payload = {'dataset': 'SENTINEL-1', 'product': 'SLC', 'start': '2024-01-01', 'end': '2024-01-31',
                       'frameScheme': 'asf', 'limit': 100,
                       'aoi': {'type': 'Polygon', 'coordinates': [[[86.79,43.10],[86.82,43.10],[86.82,43.13],[86.79,43.13],[86.79,43.10]]]}}
            catalog = json.loads(request('/api/search', payload))
            assert catalog['scenes'], 'The known historical search returned no scenes.'
            scene_count = len(catalog['scenes'])
            catalog_counts['SENTINEL-1'] = scene_count
            location=json.loads(request('/api/last-search'))['location']
            assert location['aoiBounds']==[86.79,43.10,86.82,43.13]
            assert (state/'last-search.json').is_file()
            for dataset, product in (('SLC-BURST', 'BURST'), ('NISAR', 'RSLC')):
                query = {**payload, 'dataset': dataset, 'product': product, 'limit': 3}
                if dataset == 'NISAR':
                    query.pop('start'); query.pop('end')
                    query['frameScheme'] = 'nisar'
                catalog = json.loads(request('/api/search', query))
                assert catalog['scenes'], dataset + ' search returned no scenes.'
                catalog_counts[dataset] = len(catalog['scenes'])
            for dataset, product in (('ALOS', 'L1.0'), ('ALOS-2', 'L1.1')):
                query = {**payload, 'dataset': dataset, 'product': product, 'limit': 3, 'frameScheme': 'asf',
                         'aoi': {'type': 'Polygon', 'coordinates': [[[-180,-60],[180,-60],[180,85],[-180,85],[-180,-60]]]}}
                if dataset == 'ALOS':
                    query.update(start='2006-01-01', end='2011-04-30')
                else:
                    query.pop('start'); query.pop('end')
                    query.update(path='86', frame='1150')
                catalog = json.loads(request('/api/search', query))
                assert catalog['scenes'], dataset + ' search returned no scenes.'
                assert all(scene['dataset'] == dataset and scene['product'] == product for scene in catalog['scenes'])
                catalog_counts[dataset] = len(catalog['scenes'])
            orbit=json.loads(request('/api/orbits/match',{'scenes':['S1A_IW_SLC__1SDV_20240130T121007_20240130T121034_052336_065405_7131'],'strategy':'precise'}))
            assert orbit['matched']==1 and orbit['files']==1
            if args.dem_cache:
                for name in ('tiles','geoids'):
                    target=state/'dem-cache'/name;target.mkdir(parents=True,exist_ok=True)
                    for file in (args.dem_cache/name).glob('*.tif'):
                        shutil.copyfile(file,target/file.name)
                plan={'bounds':[86.795,43.105,86.805,43.115],'buffer':0,'height':'ellipsoid','format':'ISCE'}
                assert json.loads(request('/api/dem/preview',plan))['available']==1
                job=json.loads(request('/api/dem/add',plan))['tasks'][-1]
                request('/api/downloads/settings',{'output':str(folder/'DEM结果'),'concurrent':2})
                request('/api/downloads/action',{'action':'start','ids':[job['id']]})
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    job=next(t for t in json.loads(request('/api/downloads'))['tasks'] if t['id']==job['id'])
                    if job['state'] in ('completed','error') and not job['busy']:break
                    time.sleep(.3)
                assert job['state']=='completed',job
                report_path=Path(job['target'])/'processing.json'
                assert json.loads(report_path.read_text(encoding='utf-8'))['verticalConversionValidated'] is True
                assert not (Path(job['output'])/'.sar-dem-parts'/job['id'].split(':')[1]).exists()
                assert not (state/'dem-cache/tiles').exists()
                assert list((state/'dem-cache/geoids').glob('*.tif'))
            cleanup=json.loads(request('/api/cache/clear',{}))
            assert 'bytesFreed' in cleanup['cleanup']
        print(json.dumps({'ok': True, 'version': report['version'], 'build': report['build'], 'duplicateLaunch': True, 'noInstalledPythonOnPath': True,
                          'linuxDesktopIcon': sys.platform == 'linux',
                          'nativePickerEntrypoint': True, 'aoiFormats': [Path(value['filename']).suffix for value in report['aoi']],
                          'api': True, 'https': report['https'], 'demEngine': dem_report, 'realCatalogSceneCount': scene_count,
                          'realCatalogCounts': catalog_counts, 'dependencies': report['dependencies']}, ensure_ascii=True))
    finally:
        if process:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
            if reader:reader.join(timeout=3)
            if process.stdout:process.stdout.close()
        # Only remove the exact directory created above, under our smoke root.
        if folder.resolve().is_relative_to(parent.resolve()) and not folder.is_symlink():
            for entry in folder.rglob('*'):
                if (entry.is_symlink() and (sys.platform == 'win32' or not entry.resolve().is_relative_to(folder))) or getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0) & 1024:
                    raise RuntimeError('Unexpected reparse point in test output; cleanup stopped.')
            shutil.rmtree(folder)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
