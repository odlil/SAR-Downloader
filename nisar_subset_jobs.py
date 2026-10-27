"""RSLC task lifecycle reused from the standalone subset downloader."""
from __future__ import annotations
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time

from earthdata_auth import AuthError, TransferError, validate_url
from nisar_subset_engine import POLS, Paused, download_subset, validate_corners
from storage import atomic_json, load_records
from task_scheduler import TaskBudget, Cancelled
from nisar_summary import summary_record, write_summaries

ROOT = Path(__file__).resolve().parent
ACTIVE = {"queued", "running", "pausing"}
RESUMABLE = {"queued", "paused", "error", "auth_required"}
TRANSFER_DEFAULTS = {"downloaded": 0, "total": 0, "speed": 0, "error": "", "detail": "",
                     "network_bytes": 0, "network_requests": 0, "network_retries": 0,
                     "network_speed": 0, "network_last_activity": None, "network_note": "",
                     "prep_current": 0, "prep_total": 0}


def clipped_output(directory, scene_id):
    safe_name=re.sub(r"[^A-Za-z0-9_.-]", "_", scene_id)[:190]
    if safe_name.lower().endswith('.h5'):
        safe_name = safe_name[:-3]
    return Path(directory) / f"{safe_name}_clipped.h5"


def subset_config(payload):
    """Validate both new requests and persisted tasks without changing task signatures."""
    corners = payload['corners']
    validate_corners(corners)
    frequencies = payload.get('frequencies', ['A'])
    polarizations = payload.get('polarizations', ['HH'])
    if not isinstance(frequencies, list) or not frequencies or any(value not in ('A', 'B') for value in frequencies):
        raise ValueError('请选择频率 A、B 或 A+B。')
    if not isinstance(polarizations, list) or not polarizations or any(not isinstance(value, str) or value not in POLS for value in polarizations):
        raise ValueError('请选择有效极化。')
    height = {}
    for name in ('min_height', 'max_height'):
        value = payload.get(name)
        if isinstance(value, bool):
            raise ValueError('高程范围应在 -1000 至 10000 米之间。')
        height[name] = None if value in (None, '') else float(value)
        if height[name] is not None and (not math.isfinite(height[name]) or not -1000 <= height[name] <= 10000):
            raise ValueError('高程范围应在 -1000 至 10000 米之间。')
    if all(value is not None for value in height.values()) and height['min_height'] > height['max_height']:
        raise ValueError('最低高程不能高于最高高程。')
    return deepcopy({'corners': corners, 'frequencies': frequencies, 'polarizations': polarizations, **height})


class Jobs:
    def __init__(self, state_dir, auth, budget=None):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tasks.json"
        self.lock = threading.RLock()
        self.credentials = auth
        self.tasks, self.stops = {}, {}
        self.summary_records, self.summary_warning = {}, ''
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.closing = False
        self.budget = budget or TaskBudget(2)
        records, self.storage_error = load_records(self.path, self.restore_task, header=self.restore_summaries)
        for task in records:
            if task.get("remove_requested"):
                continue
            task = {**TRANSFER_DEFAULTS, "phase_started": time.time(), **task}
            task.update(speed=0, network_speed=0)
            if task["status"] in ACTIVE:
                self.update(task, status="paused", phase="服务已重启，可继续下载", speed=0)
            if task["status"]=="complete" and task.get("output") and not Path(task["output"]).exists():
                self.reset_deleted(task, Path(task['output']))
            self.tasks[task["id"]] = task
        if self.summary_records:
            try:
                self.save()
            except OSError:
                self.storage_error = '裁剪记录保存失败，请检查本地磁盘。'

    def restore_summaries(self, saved):
        records = saved.get('summaries', [])
        if not isinstance(records, list) or len(records) > 10000:
            raise ValueError('Invalid summary history')
        for entry in records:
            status = entry.get('status')
            record = self.restore_task({**entry, 'status': 'paused' if status == 'removed' else status})
            record['status'] = status
            self.summary_records[record['id']] = summary_record(record)

    @staticmethod
    def restore_task(task):
        if not isinstance(task, dict) or not re.fullmatch(r'[a-f0-9]{16}', task.get('id', '')):
            raise ValueError('Invalid task identifier')
        if task['status'] not in ACTIVE | RESUMABLE | {'complete'}:
            raise ValueError('Invalid task state')
        if not isinstance(task['scene']['id'], str) or not task['scene']['id'].startswith('NISAR_'):
            raise ValueError('Invalid scene')
        try:
            validate_url(task['scene']['url'], asset=True)
        except TransferError as error:
            raise ValueError('Invalid saved scene URL') from error
        if not isinstance(task['output'], str) or '\x00' in task['output'] or not Path(task['output']).is_absolute() or Path(task['output']).suffix.lower() != '.h5':
            raise ValueError('Invalid task path')
        task['config'] = subset_config(task['config'])
        if 'summary_root' in task:
            root = Path(task['summary_root'])
            if not root.is_absolute() or Path(task['output']).parent not in (root, root / 'rslc'):
                raise ValueError('Invalid summary directory')
        for key, default in TRANSFER_DEFAULTS.items():
            value = task.get(key, default)
            if isinstance(default, (int, float)) and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                raise ValueError('Invalid saved progress')
        task['connections'] = max(1, min(16, int(task.get('connections', 6))))
        return task

    @staticmethod
    def update(task, **values):
        if "phase" in values and values["phase"] != task.get("phase"):
            task.update(phase_started=time.time(), detail="", prep_current=0, prep_total=0)
        task.update(values)

    def reset_deleted(self, task, output):
        task.update(TRANSFER_DEFAULTS)
        self.update(task, status="paused", phase="成品已删除，可重新下载", output=str(output))

    def save(self):
        history = deepcopy(self.summary_records)
        for identifier, item in history.items():
            if identifier not in self.tasks and item['status'] != 'complete':
                item['status'] = 'removed'
        for identifier, task in self.tasks.items():
            if task.get('summary_root'):
                history[identifier] = summary_record(task)
        atomic_json(self.path, {'version': 1, 'tasks': list(self.tasks.values()), 'summaries': list(history.values())})
        self.summary_records = history
        self.summary_warning = write_summaries(history.values())

    def snapshot(self):
        with self.lock:
            return deepcopy(list(self.tasks.values()))

    def create(self, payload, scenes):
        config = subset_config(payload)
        selected = payload.get("scenes", [])
        if not selected or len(selected) > 500:
            raise ValueError("请选择 1—500 景影像。")
        directory = Path(payload.get("directory") or ROOT / "downloads").expanduser()
        # All outputs have deterministic generated names, never remotely supplied paths.
        if not directory.is_absolute():
            raise ValueError("请使用绝对保存路径。")
        directory = directory.resolve()
        connections = max(1, min(16, int(payload.get("connections", 6))))
        with self.lock:
            if self.closing:
                raise RuntimeError("服务正在关闭，请重新启动后添加任务。")
            if any(scene not in scenes for scene in selected):
                raise ValueError("检索结果已过期，请重新检索后选择影像。")
            result, proposed = [], {}
            reserved = {Path(task["output"]): task["id"] for task in self.tasks.values()
                        if task["status"] != "complete" or Path(task["output"]).exists()}
            for scene_id in dict.fromkeys(selected):
                scene = scenes[scene_id]
                signature = hashlib.sha256(json.dumps([scene_id, config, str(directory)], sort_keys=True).encode()).hexdigest()[:16]
                output=Path(self.tasks[signature]['output']) if signature in self.tasks else clipped_output(directory / 'rslc',scene_id)
                if output in reserved and reserved[output] != signature:
                    raise ValueError("同一影像的其他裁剪范围或参数已占用 _clipped.h5 文件名，请为本次任务选择其他影像目录。")
                reserved[output] = signature
                if signature in self.tasks:
                    existing=dict(self.tasks[signature])
                    if existing.get("remove_requested"):
                        raise ValueError("该任务正在移除，请等待停止后重新添加。")
                    if existing["status"]=="complete" and not Path(existing["output"]).exists():
                        self.reset_deleted(existing, output)
                        proposed[signature] = existing
                    result.append(signature)
                    continue
                proposed[signature] = {**TRANSFER_DEFAULTS, "id": signature, "scene": dict(scene), "config": deepcopy(config),
                    "output": str(output), "summary_root": str(directory), "connections": connections, "status": "queued",
                    "phase": "待启动",
                    "phase_started": time.time(), "created": datetime.now(timezone.utc).isoformat()}
                result.append(signature)
            # Nothing mutates the live queue until every scene passes validation.
            if len(self.tasks.keys() | proposed.keys()) > 1000:
                raise ValueError('裁剪队列最多保留 1000 景，请先移除旧记录。')
            previous = dict(self.tasks)
            self.tasks.update(proposed)
            if proposed:
                try:
                    self.save()
                except OSError:
                    self.tasks = previous
                    raise
            return result

    def start_locked(self, identifiers):
        pending = []
        previous = {}
        if self.closing:
            return 0
        for identifier in identifiers:
            task = self.tasks[identifier]
            if identifier in self.stops or task["status"] not in RESUMABLE or task.get("remove_requested"):
                continue
            stop = threading.Event()
            previous[identifier] = dict(task)
            self.stops[identifier] = stop
            task.update(TRANSFER_DEFAULTS)
            self.update(task, status="queued", phase="排队等待")
            pending.append((identifier, stop))
        if pending:
            try:
                self.save()
            except OSError:
                for identifier, _ in pending:
                    self.tasks[identifier].clear()
                    self.tasks[identifier].update(previous[identifier])
                    self.stops.pop(identifier, None)
                raise
            for index, (identifier, stop) in enumerate(pending):
                try:
                    self.pool.submit(self.run, identifier, stop)
                except RuntimeError:
                    for unsent, _ in pending[index:]:
                        self.update(self.tasks[unsent], status='error', phase='等待重试',
                                    error='裁剪工作线程无法启动，请重启服务后重试。', speed=0)
                        self.stops.pop(unsent, None)
                    self.save()
                    raise
        return len(pending)

    def resume(self, identifier):
        with self.lock:
            return self.start_locked([identifier])

    def pause_locked(self, identifiers):
        count = 0
        for identifier in identifiers:
            task = self.tasks[identifier]
            if task["status"] in {"queued", "running"} and identifier in self.stops:
                self.stops[identifier].set()
                self.update(task, status="pausing", phase="正在保存进度", speed=0)
                count += 1
        if count:
            self.save()
        return count

    def pause(self, identifier):
        with self.lock:
            return self.pause_locked([identifier])

    def run(self, identifier, stop):
        try:
            with self.budget.slot(stop):
                self.execute(identifier, stop)
        except Cancelled:
            with self.lock:
                task = self.tasks[identifier]
                self.update(task, status='paused', phase='已暂停', speed=0)
        finally:
            with self.lock:
                self.finish_locked(identifier, self.tasks[identifier])

    def execute(self, identifier, stop):
        task = self.tasks[identifier]
        def progress(**values):
            with self.lock:
                if task.get("remove_requested"):
                    return
                if stop.is_set():
                    values.pop('status', None)
                    values.pop('phase', None)
                    values.update(speed=0, network_speed=0)
                self.update(task, **values)
        try:
            with self.lock:
                if stop.is_set():
                    raise Paused()
                self.update(task, status="running", phase="连接与读取元数据")
                self.save()
                scene, config, output, connections = task["scene"], task["config"], task["output"], task["connections"]
            credentials = self.credentials if self.credentials.status()["loggedIn"] else None
            if not credentials:
                raise PermissionError("请先在个人中心登录 Earthdata，再继续下载。")
            download_subset(scene["url"], config["corners"], output, credentials=credentials,
                            frequencies=config["frequencies"], polarizations=config["polarizations"],
                            connections=connections, stop=stop, progress=progress,
                            min_height=config["min_height"], max_height=config["max_height"])
            with self.lock:
                self.update(task, status="complete", phase="已完成", speed=0)
        except Paused:
            with self.lock:
                self.update(task, status="paused", phase="已暂停，可继续", speed=0)
        except (AuthError, PermissionError) as exc:
            with self.lock:
                self.update(task, status='auth_required', phase='需要登录', error=str(exc), speed=0)
        except Exception as exc:
            with self.lock:
                message = str(exc) if isinstance(exc, (ValueError, RuntimeError, TransferError)) else f"{type(exc).__name__}：处理失败，已保留进度。"
                self.update(task, status="error", phase="等待重试", error=message, speed=0)

    def finish_locked(self, identifier, task):
        self.stops.pop(identifier, None)
        task.update(speed=0, network_speed=0)
        if task.get("remove_requested"):
            self.tasks.pop(identifier, None)
        try:
            self.save()
        except OSError:
            self.storage_error = '裁剪记录保存失败，请检查本地磁盘。'

    def shutdown(self):
        self.request_shutdown()
        self.pool.shutdown(wait=True, cancel_futures=False)

    def request_shutdown(self):
        with self.lock:
            self.closing = True
            for event in self.stops.values():
                event.set()


