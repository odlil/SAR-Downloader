"""Local preflight checks; never contact data sources or start downloads."""
from functools import lru_cache
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from earthdata_auth import AuthError, TransferError
from task_scheduler import validate_action
from dem_sources import needs_auth


class PreflightError(TransferError):
    status = 409
    def __init__(self, report):
        super().__init__('下载前检查未通过，请处理检查结果后重试。')
        self.report = report


def selected_tasks(tasks, action='resume', ids=None):
    validate_action(action, ids, starting_only=True)
    wanted = set(ids) if ids is not None else None
    return [task for task in tasks if (wanted is None or task['id'] in wanted)
            and not task.get('busy') and task['state'] != 'completed'
            and (action != 'start' or task['state'] == 'queued')]


def writable_directory(directory):
    """Probe an existing ancestor; do not create the requested output directory."""
    path = Path(directory)
    while not path.exists() and path.parent != path:
        path = path.parent
    if not path.is_dir():
        raise OSError('保存路径的上级不是文件夹。')
    with tempfile.NamedTemporaryFile(prefix='.sar-write-check-', dir=path):
        pass
    usage = shutil.disk_usage(path)
    return path, usage.free, (path.stat().st_dev, path.anchor)


@lru_cache(maxsize=8)
def _dem_environment(command, bucket):
    from dem_jobs import worker_environment
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:
        result = subprocess.run([*command, '--check'], capture_output=True, encoding='utf-8',
                                errors='replace', timeout=12,
                                env=worker_environment(), **options)
        record = json.loads(result.stdout.strip().splitlines()[-1])
        return result.returncode == 0 and record.get('ok') is True
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return False


def dem_environment():
    from dem_jobs import engine_command
    try:
        return _dem_environment(tuple(engine_command()), int(time.monotonic() // 15))
    except TransferError:
        return False


def inspect(tasks, settings, auth, *, action='resume', ids=None, check_environment=True):
    from downloads import output_directory, target_path
    chosen = selected_tasks(tasks, action, ids)
    occupied = {task['target'] for task in tasks if task.get('target')}
    checks, rows, destinations, volumes = [], [], {}, {}
    unknown, known = 0, 0
    def issue(level, code, message, identifiers):
        checks.append({'level': level, 'code': code, 'message': message, 'taskIds': identifiers})

    dem_ready = None
    for task in chosen:
        ident, kind = task['id'], task['kind']
        failures = []
        if needs_auth(task) and not auth.get('loggedIn'):
            failures.append('AUTH_REQUIRED')
            issue('warning', 'AUTH_REQUIRED', '需要登录 NASA Earthdata；公共 DEM 任务仍可运行。', [ident])
        elif kind == 'sar' and 'sentinel1-burst.asf.alaska.edu' in task.get('url', '') and not auth.get('burstReady'):
            failures.append('AUTH_REQUIRED')
            issue('warning', 'AUTH_REQUIRED', '此 Burst 需要完成 ASF 应用授权并重新登录。', [ident])
        target = Path(task['target']) if task.get('target') else None
        output = str(target.parent) if kind == 'nisar-subset' and target else output_directory(task, settings.get('output', ''))
        if not output:
            failures.append('OUTPUT_REQUIRED')
            issue('error', 'OUTPUT_REQUIRED', '请先选择保存目录并保存下载设置。', [ident])
        else:
            folder = Path(output)
            if not folder.is_absolute():
                failures.append('OUTPUT_INVALID')
                issue('error', 'OUTPUT_INVALID', '保存目录必须为绝对路径。', [ident])
            else:
                target = target_path(task, folder, occupied)
                occupied.add(str(target))
                try:
                    exists = target.exists()
                except OSError:
                    exists = False
                    failures.append('OUTPUT_UNREADABLE')
                    issue('error', 'OUTPUT_UNREADABLE', f'无法检查目标路径：{target}', [ident])
                if exists:
                    if kind == 'nisar-subset' and target.is_file():
                        issue('warning', 'SUBSET_EXISTS', '裁剪输出已存在；引擎会校验完成标记，仅复用兼容结果。', [ident])
                    else:
                        failures.append('TARGET_EXISTS')
                        issue('error', 'TARGET_EXISTS', f'目标已存在，下载不会覆盖：{target}', [ident])
                key = str(folder)
                if key not in destinations:
                    try:
                        _, free, volume = writable_directory(folder)
                        destinations[key] = {'path': key, 'freeBytes': free, 'knownBytes': 0, 'unknownCount': 0, 'volume': volume}
                        volumes.setdefault(volume, {'free': free, 'required': 0, 'ids': []})
                    except OSError:
                        destinations[key] = {'path': key, 'freeBytes': None, 'knownBytes': 0, 'unknownCount': 0, 'error': True}
                destination = destinations[key]
                if destination.get('error'):
                    failures.append('OUTPUT_UNWRITABLE')
                    issue('error', 'OUTPUT_UNWRITABLE', f'保存目录或其上级无法写入：{folder}', [ident])
                size = task.get('total') or task.get('expectedBytes')
                if kind in ('nisar-subset', 'dem-process') or not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
                    unknown += 1
                    destination['unknownCount'] += 1
                else:
                    part = folder / '.sar-parts' / ident / 'payload.part'
                    try:
                        partial = part.stat().st_size if part.is_file() else 0
                    except OSError:
                        partial = 0
                    remaining = max(0, int(size) - partial)
                    known += remaining
                    destination['knownBytes'] += remaining
                    if not destination.get('error'):
                        volume = volumes[destination['volume']]
                        volume['required'] += remaining
                        volume['ids'].append(ident)
        if kind == 'dem-process' and check_environment:
            if dem_ready is None:
                dem_ready = dem_environment()
            if not dem_ready:
                failures.append('DEM_ENVIRONMENT')
                issue('error', 'DEM_ENVIRONMENT', 'DEM 处理环境不可用，请运行“配置DEM环境.cmd”后重试。', [ident])
        rows.append({'id': ident, 'title': task['title'], 'ready': not failures, 'issues': failures})

    for volume in volumes.values():
        if volume['required'] > volume['free']:
            issue('error', 'SPACE_INSUFFICIENT', '同一磁盘上的已知剩余下载量超过可用空间。', volume['ids'])
            for row in rows:
                if row['id'] in volume['ids']:
                    row['ready'] = False
                    row['issues'].append('SPACE_INSUFFICIENT')
    if unknown:
        issue('warning', 'SIZE_UNKNOWN', f'{unknown} 个任务的最终体积未知，未计入空间估算；裁剪、DEM 处理和缓存还需额外空间。', [])
    ready = sum(row['ready'] for row in rows)
    return {'canStart': bool(ready) and not any(check['level'] == 'error' for check in checks),
            'taskCount': len(chosen), 'readyCount': ready, 'blockedCount': len(chosen)-ready,
            'knownBytes': known, 'unknownCount': unknown, 'checks': checks, 'tasks': rows,
            'destinations': [{key: value for key, value in item.items() if key not in ('volume', 'error')}
                             for item in destinations.values()]}


def ensure_ready(report):
    if not report['taskCount']:
        return
    if not report['readyCount'] and all('AUTH_REQUIRED' in row['issues'] for row in report['tasks']):
        raise AuthError('请先登录 NASA Earthdata 并完成所需 ASF 授权。')
    if any(check['level'] == 'error' for check in report['checks']):
        raise PreflightError(report)
    if not report['readyCount']:
        raise AuthError('请先登录 NASA Earthdata 并完成所需 ASF 授权。')
