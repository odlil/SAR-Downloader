# Build from a clean environment: python -m PyInstaller packaging/SAR.spec
from pathlib import Path
import sys
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).parent
metadata = []
for package in ('requests', 'pyshp', 'pyproj', 'shapely', 'numpy', 'h5py', 'certifi', 'charset-normalizer', 'idna', 'urllib3'):
    metadata += copy_metadata(package)
if sys.platform != 'win32':
    metadata += copy_metadata('cryptography')

a = Analysis(
    [str(root / 'sar_app.py')], pathex=[str(root)],
    binaries=[], datas=[(str(root / 'web'), 'web'), (str(root / '.runtime' / 'build-info.json'), '.'), (str(root / 'update_config.json'), '.'), *metadata],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['torch', 'osgeo', 'matplotlib', 'IPython', 'pytest'],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='SAR', debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=True,
    disable_windowed_traceback=False,
    icon=str(root / 'packaging' / 'SAR.ico') if sys.platform == 'win32' else 'NONE',
    version=str(root / 'packaging' / 'version_info.txt'),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='SAR')
