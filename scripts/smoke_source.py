"""Install the extracted source using its real first-time setup and CMD launcher."""
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
import tempfile
import threading
import time
from urllib.request import ProxyHandler, build_opener
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('This test exercises Windows CMD launchers.')
    parent = ROOT / '.runtime' / 'source-smoke'
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='源码 配置 ', dir=parent)).resolve()
    if not folder.is_relative_to(parent.resolve()) or folder.is_symlink():
        raise RuntimeError('Unexpected source-test directory.')
    process, reader = None, None
    try:
        with zipfile.ZipFile(args.archive) as archive:
            for member in archive.infolist():
                if not (folder / member.filename).resolve().is_relative_to(folder):
                    raise ValueError('Unsafe ZIP member.')
            archive.extractall(folder)
        source = next(path for path in folder.iterdir() if path.is_dir())
        environment = os.environ.copy()
        environment.update(SAR_PYTHON=sys.executable, SAR_NO_PAUSE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                           PIP_INDEX_URL='https://pypi.org/simple', PIP_DISABLE_PIP_VERSION_CHECK='1',
                           PIP_CACHE_DIR=str(ROOT / '.runtime' / 'pip-cache'), TEMP=str(folder), TMP=str(folder))
        environment.pop('PYTHONHOME', None);environment.pop('PYTHONPATH', None)
        cmd = environment.get('COMSPEC', r'C:\Windows\System32\cmd.exe')
        command = f'"{cmd}" /d /s /c ""{source / "首次配置.cmd"}""'
        setup = subprocess.run(command, cwd=folder, env=environment, capture_output=True,
                               encoding='utf-8', errors='replace', timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
        if setup.returncode:
            raise RuntimeError('First-time setup failed:\n' + setup.stdout + setup.stderr)
        executable = source / '.venv' / 'Scripts' / 'python.exe'
        probe = subprocess.run([str(executable), '-B', str(source / 'check_environment.py'), '--native', '--picker-child'],
                               capture_output=True, encoding='utf-8', timeout=30, env=environment,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        if probe.returncode or json.loads(probe.stdout)['ok'] is not True:
            raise RuntimeError('Source environment check failed:\n' + probe.stdout + probe.stderr)
        environment.pop('SAR_PYTHON', None)
        state = folder / '独立状态'
        command = f'"{cmd}" /d /s /c ""{source / "启动界面.cmd"}" --no-browser --port 0 --state-dir "{state}""'
        process = subprocess.Popen(command, cwd=folder, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
        lines, captured = queue.Queue(), []
        def read():
            for line in process.stdout:lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True);reader.start()
        deadline, base = time.monotonic() + 40, None
        while time.monotonic() < deadline:
            try:line = lines.get(timeout=.5)
            except queue.Empty:continue
            if line is None:break
            captured.append(line)
            match = re.search(r'http://127\.0\.0\.1:\d+', line)
            if match:base = match.group();break
        if base is None:
            raise RuntimeError('Source launcher failed:\n' + ''.join(captured))
        assert '.venv\\Scripts\\python.exe' in ''.join(captured)
        opener = build_opener(ProxyHandler({}))
        with opener.open(base + '/api/health', timeout=10) as response:
            health = json.load(response)
            assert health['app'] == 'sar-downloader'
        print(json.dumps({'ok': True, 'version': health['version'], 'realFirstTimeSetup': True,
                          'projectVenvSelected': True, 'chineseAndSpacedPath': True}))
    finally:
        if process:
            if process.poll() is None:
                # Terminate only the exact CMD process tree started by this test.
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            process.wait(timeout=15)
            if reader:reader.join(timeout=3)
            if process.stdout:process.stdout.close()
        if folder.resolve().is_relative_to(parent.resolve()) and not folder.is_symlink():
            for entry in folder.rglob('*'):
                if entry.is_symlink() or getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0) & 1024:
                    raise RuntimeError('Unexpected reparse point; test cleanup stopped.')
            shutil.rmtree(folder)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
