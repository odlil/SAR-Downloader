"""Preview and clear only registered tasks' unfinished files; never accept paths from clients."""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time

from cache_cleanup import Cleanup, TASK_ID, SUBSET_RESUME_SUFFIXES
from earthdata_auth import TransferError
from task_scheduler import MAX_TASK_IDS


class UnfinishedCleanup:
    def __init__(self, hub):
        self.hub, self.previews = hub, {}

    @contextmanager
    def locked(self):
        with self.hub.lock, self.hub.files.lock, ExitStack() as stack:
            if self.hub.subsets:
                stack.enter_context(self.hub.subsets.lock)
            if self.hub.files.running or (self.hub.subsets and self.hub.subsets.stops):
                raise TransferError('请先暂停全部任务，等待下载和处理完全停止后再清理未完成数据。')
            yield

    def registry(self):
        records = {key: ('file', task) for key, task in self.hub.files.jobs.items()}
        if self.hub.subsets:
            records.update({'subset-' + key: ('subset', task) for key, task in self.hub.subsets.tasks.items()})
        return records

    @staticmethod
    def complete(record):
        engine, task = record
        return task.get('status' if engine == 'subset' else 'state') in ('complete', 'completed')

    def specs(self, record):
        engine, task = record
        if engine == 'subset':
            output = Path(task['output'])
            if not output.is_absolute() or output.suffix.lower() != '.h5':
                raise OSError('Invalid subset output')
            return [(Path(str(output) + suffix), output.parent) for suffix in SUBSET_RESUME_SUFFIXES]
        from downloads import output_directory
        output = output_directory(task, self.hub.files.settings['output'])
        if not output:
            return []  # Never started and no output directory configured.
        root = Path(output)
        key = task['id'].removeprefix('dem:')
        if not root.is_absolute() or not TASK_ID.fullmatch(key):
            raise OSError('Invalid task cache path')
        container = root / ('.sar-dem-parts' if task['kind'] == 'dem-process' else '.sar-parts')
        return [(container / key, container)]

    @staticmethod
    def inspect(specs, formal):
        entries = []
        for path, root in specs:
            path = Path(os.path.abspath(path))
            if any(target == path or target.is_relative_to(path) or path.is_relative_to(target) for target in formal):
                raise OSError('Cache overlaps a registered output')
            entries.extend(Cleanup.inspect(path, root))
        fingerprint = sorted((str(path), info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev) for path, info in entries)
        return {'bytes': sum(info.st_size for _, info in entries if stat.S_ISREG(info.st_mode)),
                'files': sum(stat.S_ISREG(info.st_mode) for _, info in entries), 'fingerprint': fingerprint}

    @staticmethod
    def signature(record):
        return hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()

    def plan(self):
        registry = self.registry()
        formal = {Path(os.path.abspath(task['output'] if engine == 'subset' else task['target']))
                  for engine, task in registry.values() if task.get('output' if engine == 'subset' else 'target')}
        paths, rows = {}, {}
        for key, record in registry.items():
            if self.complete(record):
                continue
            engine, task = record
            row = {'id': key, 'title': 'NISAR 裁剪 · ' + task['scene']['id'] if engine == 'subset' else task['title'],
                   'kind': 'nisar-subset' if engine == 'subset' else task['kind'], 'bytes': 0, 'files': 0,
                   'paths': [], 'error': ''}
            try:
                specs = self.specs(record)
                paths[key] = specs
                info = self.inspect(specs, formal)
                row.update({name: info[name] for name in ('bytes', 'files')}, paths=[str(path) for path, _ in specs])
                row['_fingerprint'], row['_signature'] = info['fingerprint'], self.signature(record)
            except OSError:
                row['error'] = '无法安全读取临时文件：请检查路径、权限、文件占用或目录链接。'
            rows[key] = row
        owners = {}
        for key, specs in paths.items():
            for path, _ in specs:
                owners.setdefault(os.path.normcase(os.path.abspath(path)), []).append(key)
        for keys in owners.values():
            if len(keys) > 1:
                for key in keys:
                    rows[key]['error'] = '断点路径被多个任务使用，请先核对任务记录。'
        dem_ids = [key for key, record in registry.items() if record[1].get('kind') == 'dem-process' and not self.complete(record)]
        cache = self.hub.runtime / 'dem-cache'
        shared_specs = [(cache / 'tiles', cache), (cache / 'geoids' / '.sar-parts', cache / 'geoids')]
        shared = {'bytes': 0, 'files': 0, 'error': ''}
        try:
            shared.update(self.inspect(shared_specs, formal))
        except OSError:
            shared['error'] = '共享 DEM 缓存无法安全清理，将保留。'
        return rows, paths, dem_ids, shared, shared_specs

    @staticmethod
    def select(ids, rows):
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_TASK_IDS or any(not isinstance(key, str) or key not in rows for key in ids):
            raise TransferError('任务选择无效或已变化，请重新预览。')
        selected = list(dict.fromkeys(ids))
        if any(rows[key]['error'] for key in selected):
            raise TransferError('所选任务存在无法安全清理的路径，请先处理提示。')
        return selected

    def preview(self, payload):
        if set(payload) - {'ids'}:
            raise TransferError('清理预览只接受任务编号。')
        with self.locked():
            rows, _, dem_ids, shared, _ = self.plan()
            ids = self.select(payload['ids'], rows) if 'ids' in payload else list(rows)
            now = time.monotonic()
            self.previews = {key: value for key, value in self.previews.items() if value['expires'] > now}
            while len(self.previews) >= 8:
                self.previews.pop(next(iter(self.previews)))
            token = secrets.token_urlsafe(24)
            self.previews[token] = {'expires': now + 300, 'rows': {key: rows[key] for key in ids}, 'shared': shared, 'demIds': dem_ids}
            return {'token': token, 'tasks': [{k: v for k, v in rows[key].items() if not k.startswith('_')} for key in ids],
                    'demIds': dem_ids, 'shared': {key: shared[key] for key in ('bytes', 'files', 'error')}}

    def clear(self, payload):
        if set(payload) - {'token', 'ids', 'removeTasks'} or not isinstance(payload.get('removeTasks', False), bool):
            raise TransferError('清理请求无效。')
        token = payload.get('token')
        if not isinstance(token, str):
            raise TransferError('请先预览待清理文件。')
        with self.locked():
            old = self.previews.pop(token, None)
            if not old or old['expires'] <= time.monotonic():
                raise TransferError('预览已过期，请重新打开清理窗口。')
            ids = self.select(payload.get('ids'), old['rows'])
            rows, paths, dem_ids, shared, shared_specs = self.plan()
            for key in ids:
                if key not in rows or rows[key]['error'] or any(rows[key].get(field) != old['rows'][key].get(field) for field in ('_signature', '_fingerprint')):
                    raise TransferError('任务或临时文件在预览后发生变化，请重新预览。')
            clear_shared = bool(dem_ids) and set(dem_ids).issubset(ids)
            if clear_shared and (dem_ids != old['demIds'] or shared != old['shared']):
                raise TransferError('共享 DEM 缓存在预览后发生变化，请重新预览。')
            files, subsets = self.hub.files, self.hub.subsets
            records = self.registry()
            # Persist stopped, reset records BEFORE deletion. A crash or partial
            # deletion must never leave tasks enabled or claiming stale progress.
            for key in ids:
                engine, task = records[key]
                if engine == 'subset':
                    from nisar_subset_jobs import TRANSFER_DEFAULTS
                    task.update(TRANSFER_DEFAULTS)
                    subsets.update(task, status='paused', phase='未完成数据已重置，需重新下载')
                else:
                    task.update(state='paused', enabled=False, completed=0, total=None, speed=0, progress=0,
                                error='', phase='未完成数据已重置，需重新下载')
                    task.pop('cleanupWarning', None)
            try:
                files._save()
                if subsets: subsets.save()
            except OSError as error:
                raise TransferError('任务状态无法保存，未删除文件；请检查本机存储权限。') from error
            cleanup, succeeded, failed = Cleanup(), [], []
            for key in ids:
                before = cleanup.skipped
                for path, root in paths[key]:
                    cleanup.remove(path, root)
                (failed if cleanup.skipped != before else succeeded).append(key)
            shared_skipped = cleanup.skipped
            if clear_shared and not shared['error']:
                for path, root in shared_specs:
                    cleanup.remove(path, root)
            shared_failed = cleanup.skipped != shared_skipped
            removed = []
            for key in succeeded:
                if payload.get('removeTasks'):
                    engine, task = records[key]
                    (subsets.tasks if engine == 'subset' else files.jobs).pop(task['id'], None)
                    removed.append(key)
            for key in failed:
                engine, task = records[key]
                task['error'] = '部分未完成文件未能清理，任务记录已保留；请检查文件占用后重新预览。'
            try:
                files._save()
                if subsets: subsets.save()
            except OSError as error:
                raise TransferError('未完成文件已执行清理，但任务记录保存失败，请检查存储权限并刷新队列。') from error
            return {**self.hub.snapshot(), 'cleanup': {**cleanup.report(), 'resetCount': len(succeeded),
                    'removedCount': len(removed), 'failedIds': failed,
                    'sharedPreserved': bool(dem_ids) and (not clear_shared or bool(shared['error']) or shared_failed)}}
