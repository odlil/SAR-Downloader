"""Freeze only the separate sardem engine from its private GDAL environment."""
from pathlib import Path
import os
import sys
from PyInstaller.utils.hooks import copy_metadata

root=Path(SPECPATH).parent
prefix=Path(sys.prefix)
if sys.platform == 'win32':
    os.environ['PATH']=str(prefix/'Library'/'bin')+os.pathsep+os.environ.get('PATH','')
    share = prefix/'Library'/'share'
    libraries = [(str(prefix/'Library'/'bin'/'gdal.dll'),'.')]
else:
    share = prefix/'share'
    libraries = [(str(path),'.') for path in (prefix/'lib').glob('libgdal.so*') if path.is_file() and not path.is_symlink()]
    if not libraries:
        raise RuntimeError('The Linux GDAL shared library was not found in this environment.')
gdal_data=share/'gdal'
proj_data=share/'proj'
if not gdal_data.is_dir() or not (proj_data/'proj.db').is_file():
    raise RuntimeError('Build DEM.spec inside the configured conda-forge GDAL environment.')
metadata=[]
for name in ('sardem','GDAL','numpy','requests','certifi','charset-normalizer','idna','urllib3'):
    metadata+=copy_metadata(name)
a=Analysis([str(root/'dem_worker.py')],pathex=[str(root)],
    binaries=libraries,datas=[(str(gdal_data),'gdal_data'),(str(proj_data),'proj_data'),*metadata],
    hiddenimports=['osgeo.gdal','osgeo.osr','sardem.cop_dem'],
    hookspath=[],runtime_hooks=[],hooksconfig={},
    excludes=['torch','matplotlib','IPython','pytest','tkinter','pyproj','shapely','rasterio','pandas','scipy','pyarrow'],
    noarchive=False,optimize=0)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='SAR-DEM',debug=False,
    bootloader_ignore_signals=False,strip=False,upx=False,console=True,icon='NONE')
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='dem-engine')
