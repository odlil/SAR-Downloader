"""Validate DEM jobs and supervise their isolated sardem/GDAL worker process."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from app_runtime import APP_ROOT
from auxiliary_api import dem_plan
from earthdata_auth import TransferError
from storage import atomic_json
from dem_sources import SOURCES, needs_auth


def new_dem_job(item):
    plan = dem_plan(item.get('plan'))
    if plan['format'] == 'tiles':raise TransferError('原始瓦片请通过普通下载队列添加。')
    # Reconstruct derived bounds, never accept worker paths/URLs from the client.
    canonical = {key: plan[key] for key in ('bounds', 'buffer', 'height', 'format')}
    # Preserve existing COP task IDs and their resume directories.
    if plan['source'] != 'COP':canonical['source'] = plan['source']
    label = SOURCES[plan['source']]['short']
    key = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:24]
    return {'id': 'dem:' + key, 'kind': 'dem-process', 'url': '', 'plan': canonical,
            'filename': label + '_' + key[:12],
            'title': label.replace('_', ' ') + ' · ' + ('WGS84 椭球高' if plan['height'] == 'ellipsoid' else plan['height'].upper()) + ' · ' + plan['format'],
            'state': 'queued', 'completed': 0, 'total': None, 'speed': 0, 'progress': 0,
            'phase': '待启动', 'error': '', 'createdAt': time.time(), 'output': '', 'target': ''}


def engine_command():
    if getattr(sys, 'frozen', False):
        name = 'SAR-DEM.exe' if sys.platform == 'win32' else 'SAR-DEM'
        executable = Path(sys.executable).parent / 'dem-engine' / name
        if not executable.is_file():raise TransferError('此发行包缺少 DEM 引擎，请完整解压最新版免安装包。')
        return [str(executable)]
    executable = os.environ.get('SAR_DEM_PYTHON')
    if not executable:
        configured = APP_ROOT / '.runtime' / 'dem-engine.json'
        try:executable = json.loads(configured.read_text(encoding='utf-8')).get('python')
        except (OSError, ValueError, AttributeError):pass
    if not executable:
        candidate = APP_ROOT / '.dem-env' / ('python.exe' if sys.platform == 'win32' else 'bin/python')
        executable = str(candidate) if candidate.is_file() else sys.executable
    return [executable, '-B', str(APP_ROOT / 'dem_worker.py')]


def worker_environment():
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    local_lib = APP_ROOT / '.runtime' / 'dem-python'
    if not getattr(sys, 'frozen', False) and local_lib.is_dir():
        environment['PYTHONPATH'] = str(local_lib)
    return environment


def run_dem_job(manager, job, event):
    from downloads import Paused
    target = Path(job['target'])
    if target.exists():raise TransferError('DEM 结果目录已存在，未覆盖；请更换保存目录后重试。')
    work = Path(job['output']) / '.sar-dem-parts' / job['id'].split(':')[1]
    work.mkdir(parents=True, exist_ok=True)
    planfile = work / 'request.json'
    atomic_json(planfile, {'plan': job['plan'], 'work': str(work), 'target': str(target),
                           'cache': str(manager.runtime / 'dem-cache')})
    environment = worker_environment()
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    messages = queue.Queue()
    credentials = manager.auth.worker_state() if needs_auth(job) else None
    with (work / 'worker.log').open('w', encoding='utf-8') as errorlog:
        process = subprocess.Popen([*engine_command(), '--run', str(planfile)], stdout=subprocess.PIPE,
                                   stdin=subprocess.PIPE if credentials else subprocess.DEVNULL,
                                   stderr=errorlog, encoding='utf-8', errors='replace', env=environment, **options)
        if credentials:
            try:
                process.stdin.write(json.dumps(credentials) + '\n')
                process.stdin.close()
            except (OSError, BrokenPipeError):
                process.terminate();process.wait(timeout=10)
                raise TransferError('无法向 DEM 引擎传递登录会话，请重试。') from None
            finally:
                credentials = None
        def read():
            try:
                for line in process.stdout:
                    try:messages.put(json.loads(line))
                    except ValueError:pass
            except OSError:
                messages.put({'readerError': True})
            finally:
                messages.put(None)
        reader = threading.Thread(target=read, daemon=True)
        failure, done, reader_done, auth_failed = '', False, False, False
        try:
            reader.start()
            while not reader_done or process.poll() is None:
                if event.is_set():raise Paused()
                try:message = messages.get(timeout=.2)
                except queue.Empty:continue
                if message is None:reader_done=True;continue
                if not isinstance(message, dict):continue
                if message.get('readerError'):
                    raise TransferError('无法读取 DEM 子进程进度，已保留缓存；请重试。')
                if message.get('error'):failure = str(message['error'])[:1000]
                if message.get('authRequired'):auth_failed = True
                if message.get('done'):done = True
                if isinstance(message.get('progress'), (int, float)):
                    manager._update(job, progress=max(0, min(1, message['progress'])),
                                    phase=str(message.get('phase', '处理中'))[:160], state='processing', error='')
            process.wait(timeout=5)
            if process.returncode or not done or not target.is_dir():
                if auth_failed:
                    from earthdata_auth import AuthError
                    raise AuthError(failure or 'DEM 下载需要重新登录 Earthdata。')
                raise TransferError(failure or 'DEM 处理未完成；已保留原始瓦片缓存，可重试。详情见任务临时目录 worker.log。')
            manager._update(job, progress=1, phase='处理完成', completed=0, total=None)
        finally:
            if process.poll() is None:
                process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            if reader.ident is not None:
                reader.join(timeout=3)
            process.stdout.close()
