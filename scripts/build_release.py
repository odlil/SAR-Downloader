"""Create clean source/portable ZIPs from explicit project inputs; never ship local state."""
from __future__ import annotations

import argparse
import hashlib
import json
from importlib import metadata, util
import os
import platform
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_runtime import VERSION, REVISION

SOURCE_FILES = (
    'README.md', 'QUICKSTART.md', 'RELEASING.md', 'CHANGELOG.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'SECURITY.md', 'CONTRIBUTING.md', 'RELEASE_NOTES.md',
    '.gitignore', '.gitattributes', 'requirements.txt', 'requirements-lock.txt', 'requirements-build.txt',
    'package.json', '启动SAR Downloader.cmd', '启动界面.cmd', '首次配置.cmd', '配置DEM环境.cmd',
    'sar_app.py', 'app_runtime.py', 'https_runtime.py', 'check_environment.py', 'server.py', 'asf_api.py', 'storage.py', 'task_scheduler.py', 'preflight.py',
    'aoi_import.py', 'aoi_dialog.py', 'native_dialog.py', 'earthdata_auth.py', 'local_secrets.py', 'downloads.py', 'download_hub.py', 'nisar_subset_jobs.py', 'nisar_subset_engine.py',
    'auxiliary_api.py', 'public_assets.py', 'dem_jobs.py', 'dem_worker.py', 'dem_sources.py', 'last_search.py', 'cache_cleanup.py', 'unfinished_cleanup.py', 'alos2_catalog.py',
    'web/help/manual.pdf', 'updates.py', 'update_config.json', 'nisar_summary.py',
)
SOURCE_DIRS = ('web', 'scripts', 'packaging', 'tests', '.github')
SUFFIXES = {'.py', '.cmd', '.md', '.txt', '.json', '.js', '.mjs', '.css', '.html', '.svg', '.geojson',
            '.spec', '.ico', '.yml', '.yaml', '.shp', '.prj', '.shx', '.dbf', '.cpg', '.kml', '.kmz'}


def owned_path(path):
    path = Path(path).absolute()
    if not path.resolve().is_relative_to(ROOT.resolve()) or path.resolve() == ROOT.resolve():
        raise ValueError('Build outputs must stay inside a project subdirectory.')
    for component in (path, *path.parents):
        if component.exists() and (component.is_symlink() or getattr(component.stat(follow_symlinks=False), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Build outputs must not traverse a Windows reparse point.')
        if component == ROOT:
            break
    return path.resolve()


def source_files():
    for name in SOURCE_FILES:
        path = ROOT / name
        if not path.is_file():
            raise FileNotFoundError('Missing release input: ' + name)
        yield path
    for directory in SOURCE_DIRS:
        for path in sorted((ROOT / directory).rglob('*')):
            if not path.is_file() or any(part in ('__pycache__', '.venv', '.runtime', '.git', '_output') or part.startswith(('tmp', '启动 测试')) for part in path.relative_to(ROOT).parts):
                continue
            if path.suffix.lower() not in SUFFIXES | {'.sh'} and path.name != 'LEAFLET-LICENSE':
                continue
            if path.is_symlink():
                raise ValueError('Release inputs cannot be symlinks: ' + str(path.relative_to(ROOT)))
            yield path


def source_identity():
    entries = sorted((path.relative_to(ROOT).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
                     for path in source_files())
    fingerprint = hashlib.sha256(json.dumps(entries, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'version': VERSION, 'revision': REVISION, 'sourceTreeSha256': fingerprint, 'sourceFileCount': len(entries)}


def zip_files(target, files):
    temporary = target.with_suffix('.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=7) as archive:
        for path, name in files:
            archive.write(path, name)
    os.replace(temporary, target)


def tar_bundle(target, directory):
    for path in directory.rglob('*'):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(directory.resolve()):
            raise ValueError('Bundle link leaves the application directory: ' + str(path))
    temporary = target.with_suffix('.tmp')
    with tarfile.open(temporary, 'w:gz', compresslevel=7) as archive:
        archive.add(directory, arcname='SAR')
    os.replace(temporary, target)


def collect_linux_desktop(bundle):
    shutil.copyfile(ROOT / 'web' / 'assets' / 'favicon.svg', bundle / 'SAR.svg')
    installer = bundle / '设置应用图标.sh'
    shutil.copyfile(ROOT / 'packaging' / 'install-linux-desktop.sh', installer)
    installer.chmod(0o755)


def collect_licenses(bundle):
    target = bundle / 'licenses'
    target.mkdir(exist_ok=True)
    packages = ('requests', 'pyshp', 'pyproj', 'shapely', 'numpy', 'h5py', 'certifi', 'charset-normalizer', 'idna', 'urllib3',
                'pyinstaller', 'pyinstaller-hooks-contrib')
    if sys.platform != 'win32':
        packages += ('cryptography', 'cffi', 'pycparser')
    for package in packages:
        distribution = metadata.distribution(package)
        folder = target / package
        folder.mkdir(exist_ok=True)
        summary = f'{distribution.metadata["Name"]} {distribution.version}\n'
        summary += 'License: ' + str(distribution.metadata.get('License-Expression') or distribution.metadata.get('License') or 'See included license files') + '\n'
        (folder / 'PACKAGE.txt').write_text(summary, encoding='utf-8')
        index = 0
        for file in distribution.files or []:
            name = Path(file).name.lower()
            if not (name.startswith(('license', 'copying', 'notice')) or '/licenses/' in file.as_posix()):
                continue
            source = Path(distribution.locate_file(file))
            if source.is_file():
                index += 1
                shutil.copyfile(source, folder / f'{index:03d}-{source.name}')
    for name in ('LICENSE.txt', 'LICENSE_PYTHON.txt', 'LICENSE'):
        candidate = Path(sys.base_prefix) / name
        if candidate.is_file():
            shutil.copyfile(candidate, target / ('Python-' + name))
    for name in ('tcl8.6', 'tk8.6'):
        for parent in (Path(sys.base_prefix) / 'Library' / 'lib', Path(sys.base_prefix) / 'tcl', Path(sys.base_prefix) / 'lib'):
            candidate = parent / name / 'license.terms'
            if candidate.is_file():
                shutil.copyfile(candidate, target / (name + '-license.terms'))
    for source in (ROOT / 'packaging' / 'licenses').glob('*.txt'):
        shutil.copyfile(source, target / source.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-only', action='store_true')
    parser.add_argument('--dem-python', type=Path, help='Python from a private GDAL+sardem+PyInstaller environment')
    parser.add_argument('--output', type=Path, default=ROOT / 'release-artifacts')
    args = parser.parse_args()
    if not args.source_only and (sys.platform not in ('win32', 'linux') or platform.machine().lower() not in ('amd64', 'x86_64')):
        parser.error('Build natively on Windows x64 or Linux x86_64.')
    output = owned_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    identity = source_identity()
    build_info = ROOT / '.runtime' / 'build-info.json'
    build_info.parent.mkdir(parents=True, exist_ok=True)
    build_info.write_text(json.dumps(identity, indent=2), encoding='utf-8')
    source = output / f'SAR-v{VERSION}-source.zip'
    zip_files(source, ((path, f'SAR-v{VERSION}-source/' + path.relative_to(ROOT).as_posix()) for path in source_files()))
    artifacts.append(source)
    if not args.source_only:
        if util.find_spec('PyInstaller') is None:
            parser.error('Install requirements-build.txt into a clean build environment first.')
        target = 'windows-x64' if sys.platform == 'win32' else 'linux-x86_64'
        build = owned_path(ROOT / 'build' / f'v{VERSION}' / target)
        distribution = owned_path(build / 'dist' / 'SAR')
        # PyInstaller may replace this exact owned directory on a rebuild.
        environment = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(ROOT / '.runtime' / 'pyinstaller-cache'),
                           PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        if sys.platform == 'linux':
            for key in ('LD_LIBRARY_PATH', 'PROJ_LIB', 'PROJ_DATA', 'GDAL_DATA', 'PYTHONPATH', 'PYTHONHOME'):
                environment.pop(key, None)
        subprocess.run([sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm', '--clean',
                        '--distpath', str(build / 'dist'), '--workpath', str(build / 'work'),
                        str(ROOT / 'packaging' / 'SAR.spec')], cwd=ROOT, env=environment, check=True)
        for name in ('README.md', 'QUICKSTART.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CHANGELOG.md', 'RELEASE_NOTES.md'):
            shutil.copyfile(ROOT / name, distribution / name)
        collect_licenses(distribution)
        dem_python=args.dem_python or os.environ.get('SAR_DEM_PYTHON')
        if not dem_python:
            try:dem_python=json.loads((ROOT/'.runtime'/'dem-engine.json').read_text(encoding='utf-8'))['python']
            except (OSError,KeyError,ValueError):parser.error('Configure the DEM engine with scripts/setup_dem.py --managed --build first.')
        subprocess.run([str(dem_python),'-B','-m','PyInstaller','--noconfirm','--clean',
                        '--distpath',str(distribution),'--workpath',str(build/'dem-work'),
                        str(ROOT/'packaging'/'DEM.spec')],cwd=ROOT,env=environment,check=True)
        subprocess.run([str(dem_python),'-B',str(ROOT/'scripts'/'collect_dem_licenses.py'),
                        str(distribution/'dem-engine'/'licenses')],cwd=ROOT,env=environment,check=True)
        if sys.platform == 'win32':
            binary = output / f'SAR-v{VERSION}-{target}.zip'
            zip_files(binary, ((path, 'SAR/' + path.relative_to(distribution).as_posix())
                               for path in sorted(distribution.rglob('*')) if path.is_file()))
        else:
            collect_linux_desktop(distribution)
            launcher = distribution / '启动SAR Downloader.sh'
            shutil.copyfile(ROOT / 'packaging' / 'start-linux.sh', launcher)
            launcher.chmod(0o755)
            binary = output / f'SAR-v{VERSION}-{target}.tar.gz'
            tar_bundle(binary, distribution)
        artifacts.append(binary)
    if source_identity() != identity:
        raise RuntimeError('Source changed during build; do not publish these artifacts. Build again from one fixed source tree.')
    checksums = output / 'SHA256SUMS.txt'
    # Preserve checksums of other platforms already built in this release folder.
    release_files = sorted(path for path in output.iterdir()
                           if path.name.startswith(f'SAR-v{VERSION}-') and path.name.endswith(('.zip', '.tar.gz')))
    checksums.write_text(''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n'
                                 for path in release_files), encoding='ascii')
    for path in artifacts:
        print(f'{path.name}: {path.stat().st_size / 1024 / 1024:.1f} MiB', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
