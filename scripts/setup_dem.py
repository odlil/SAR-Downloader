"""Configure an isolated GDAL+sardem engine without modifying base environments."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def usable(executable):
    if not executable or not Path(executable).is_file():return False
    try:
        return subprocess.run([str(executable), '-B', '-c', 'from osgeo import gdal; import numpy,requests; assert gdal.GetDriverByName("GTiff")'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', default=sys.executable)
    parser.add_argument('--managed', action='store_true')
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--prefix', type=Path, default=ROOT / '.dem-env')
    args = parser.parse_args()
    if args.build:args.managed=True
    runtime = ROOT / '.runtime'
    runtime.mkdir(exist_ok=True)
    candidate = os.environ.get('SAR_DEM_PYTHON') or args.candidate
    if not args.managed and usable(candidate):
        print('[DEM] Reusing the selected GDAL Python; installing sardem only in the project.', flush=True)
        library = runtime / 'dem-python'
        subprocess.run([sys.executable,'-B','-m','pip','install','--disable-pip-version-check','--no-input','--upgrade',
                        '--no-deps','--target',str(library),'sardem==0.14.0'],check=True)
        executable = Path(candidate).resolve()
    else:
        if sys.platform not in ('win32', 'linux'):
            print('Install GDAL, numpy, requests and sardem>=0.14 with conda-forge, then set SAR_DEM_PYTHON.')
            return 1
        prefix = args.prefix.resolve()
        if not prefix.is_relative_to(ROOT) or prefix == ROOT:
            raise ValueError('DEM environment must stay in a project subdirectory.')
        tools = runtime / 'tools';tools.mkdir(exist_ok=True)
        windows = sys.platform == 'win32'
        mamba = tools / ('micromamba-2.0.5.exe' if windows else 'micromamba-2.0.5')
        if not mamba.is_file():
            print('[DEM] Downloading the official micromamba bootstrap ...', flush=True)
            base = 'https://github.com/mamba-org/micromamba-releases/releases/download/2.0.5-0/'
            asset = 'micromamba-win-64' if windows else 'micromamba-linux-64'
            with urllib.request.urlopen(base+asset+('.exe' if windows else ''),timeout=60) as response:
                content = response.read(32*1024*1024+1)
            if len(content)>32*1024*1024:raise RuntimeError('Micromamba archive is too large.')
            with urllib.request.urlopen(base+asset+'.sha256',timeout=30) as response:
                expected = response.read(1024).decode('ascii').split()[0]
            if hashlib.sha256(content).hexdigest() != expected:raise RuntimeError('Micromamba checksum mismatch.')
            mamba.write_bytes(content)
            if not windows:
                mamba.chmod(0o755)
        print('[DEM] Creating a private conda-forge engine. First setup needs additional downloads.',flush=True)
        # libmamba's Windows cache reader cannot reliably handle non-ASCII paths.
        # Only the download cache is outside the project; the environment stays in it.
        cache = Path(os.environ.get('SAR_MAMBA_ROOT_PREFIX') or
                     str(Path(os.environ.get('LOCALAPPDATA',tempfile.gettempdir()))/'SARDownloader'/'setup-cache'))
        environment = dict(os.environ, MAMBA_ROOT_PREFIX=str(cache))
        probe = subprocess.run([str(mamba),'--version'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        conda = shutil.which('conda.exe' if windows else 'conda')
        if probe.returncode == 0:
            installer = [str(mamba),'--no-rc']
        elif conda:
            print('[DEM] Micromamba is unavailable; using the existing conda CLI with an isolated package cache.',flush=True)
            environment['CONDA_PKGS_DIRS'] = str(runtime/'conda-pkgs')
            installer = [conda]
        else:
            raise RuntimeError('Micromamba could not start. Install Miniforge, reopen CMD and rerun this setup.')
        subprocess.run([*installer,'create','--yes','--prefix',str(prefix),'--override-channels',
                        '--channel','https://conda.anaconda.org/conda-forge','--strict-channel-priority','python=3.11','gdal>=3.6,<4','numpy','libblas=*=*openblas','requests','tk','pip'],
                       env=environment,check=True)
        executable = prefix/('python.exe' if windows else 'bin/python')
        subprocess.run([str(executable),'-B','-m','pip','install','--disable-pip-version-check','--no-input','--no-deps','sardem==0.14.0'],check=True)
    if args.build:
        subprocess.run([str(executable),'-B','-m','pip','install','--disable-pip-version-check','--no-input',
                        'pyinstaller==6.22.3','pyinstaller-hooks-contrib==2026.8'],check=True)
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if (runtime/'dem-python').is_dir():environment['PYTHONPATH']=str(runtime/'dem-python')
    subprocess.run([str(executable),'-B',str(ROOT/'dem_worker.py'),'--check'],env=environment,check=True)
    (runtime/'dem-engine.json').write_text(json.dumps({'python':str(executable)},ensure_ascii=False),encoding='utf-8')
    print('[DEM] Engine ready.',flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
