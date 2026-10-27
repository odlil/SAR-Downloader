"""Small persistent download queue: bounded parallelism, HTTP Range and atomic completion."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import unquote, urlsplit

import requests

from earthdata_auth import AuthError, ScopedSession, TransferError, validate_url
from public_assets import public_asset_kind
from cache_cleanup import Cleanup
from storage import atomic_json, load_records
from task_scheduler import TaskBudget, Cancelled, validate_action
from dem_sources import needs_auth

CHUNK = 256 * 1024


class Paused(Exception):
    pass


def output_directory(job, root):
    """Keep bound transfers in place; route new auxiliary files below the imagery root."""
    if job.get('output'):
        return job['output']
    if not root:
        return ''
    subfolder = {'orbit': 'orbit', 'dem': 'dem', 'dem-process': 'dem'}.get(job['kind'])
    return str(Path(root) / subfolder) if subfolder else root


def target_path(job, output, occupied):
    if job.get('target'):
        return Path(job['target'])
    target = Path(output) / job['filename']
    if str(target) in occupied:
        target = target.with_name(target.stem + '--' + job['id'][:10] + target.suffix)
    return target


def filename_for(value, url):
    name = value or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    if not isinstance(name, str) or len(name) > 230 or name in ("", ".", "..") or name[-1:] in (".", " "):
        raise TransferError("下载文件名无效。")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise TransferError("下载文件名包含无效字符。")
    if name.lower().endswith((".exe", ".cmd", ".bat", ".ps1", ".js", ".html", ".lnk")):
        raise TransferError("此队列只接受 SAR 数据资产。")
    if name.split(".")[0].isdigit():
        stem, suffix = os.path.splitext(name)
        name = stem + "--" + hashlib.sha256(url.encode()).hexdigest()[:10] + suffix
    return name


class DownloadManager:
    def __init__(self, auth, runtime, *, retry_delay=2, generation_delay=10, budget=None):
        self.auth, self.runtime = auth, Path(runtime)
        self.lock = threading.RLock()
        self.jobs, self.running, self.cancels = {}, {}, {}
        self.settings = {"concurrent": 2, "output": ""}
        self.retry_delay, self.generation_delay = retry_delay, generation_delay
        self.closed = False
        self.storage_error = ""
        self._load()
        self.budget = budget or TaskBudget(self.settings['concurrent'])

    def _load(self):
        def header(data):
            try:
                self.settings = self._settings(data.get('settings', {}))
            except TransferError as error:
                raise ValueError() from error
        records, self.storage_error = load_records(self.runtime / 'downloads.json', self._restore_job,
                                                   field='jobs', header=header, label='下载')
        self.jobs = {job['id']: job for job in records}

    def _restore_job(self, original):
        try:
            job = self._new_job(original)
        except TransferError as error:
            raise ValueError() from error
        for key in ('output', 'target'):
            value = original.get(key, '')
            if not isinstance(value, str) or '\x00' in value or (value and not Path(value).is_absolute()):
                raise ValueError('Invalid saved path')
            job[key] = value
        if job['target'] and (not job['output'] or Path(job['target']).parent.resolve() != Path(job['output']).resolve()):
            raise ValueError('Target is outside its saved directory')
        for key in ('completed', 'total', 'expectedBytes'):
            value = original.get(key, job.get(key))
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                raise ValueError('Invalid saved progress')
            if value is not None:
                job[key] = int(value)
        progress = original.get('progress', job.get('progress', 0))
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not math.isfinite(progress) or not 0 <= progress <= 1:
            raise ValueError('Invalid processing progress')
        for key in ('createdAt', 'phase', 'cleanupWarning'):
            if key in original:
                job[key] = original[key]
        job['progress'] = progress
        job['state'] = 'completed' if original.get('state') == 'completed' else 'paused'
        target = Path(job['target']) if job['target'] else None
        exists = bool(target and (target.is_dir() if job['kind'] == 'dem-process' else target.is_file()))
        if job['state'] == 'completed' and not exists:
            job.update(state='error', error='已完成的文件已移动或删除。')
        return job

    @staticmethod
    def _settings(settings):
        if not isinstance(settings, dict):
            raise TransferError('下载设置格式无效。')
        count = settings.get("concurrent", 2)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 4:
            raise TransferError("同时运行任务数需要为 1–4。")
        output = settings.get("output", "")
        if not isinstance(output, str) or len(output) > 2048 or "\x00" in output:
            raise TransferError("保存目录无效。")
        if output and not Path(output).is_absolute():
            raise TransferError("请填写保存目录的绝对路径，或使用选择目录。")
        return {"concurrent": count, "output": str(Path(output).resolve()) if output else ""}

    @staticmethod
    def _new_job(item):
        if not isinstance(item, dict):
            raise TransferError("下载任务格式无效。")
        if item.get('kind') == 'dem-process':
            from dem_jobs import new_dem_job
            return new_dem_job(item)
        url = validate_url(item.get("url"), asset=True)
        kind = public_asset_kind(url) or 'sar'
        filename = filename_for(item.get("filename"), url)
        size = item.get('size')
        expected = int(size) if isinstance(size, (int, float)) and not isinstance(size, bool) and math.isfinite(size) and 0 < size <= 2**53 else None
        return {"id": hashlib.sha256(url.encode()).hexdigest()[:24], "kind": kind, "url": url,
                "filename": filename, "title": str(item.get("title") or filename)[:500],
                "state": "queued", "completed": 0, "total": None, "speed": 0,
                "error": "", "createdAt": time.time(), "output": "", "target": "", 'expectedBytes': expected}

    def _save(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        jobs = [{key: value for key, value in job.items() if key not in ("speed", "error")}
                for job in self.jobs.values()]
        atomic_json(self.runtime / "downloads.json", {"version": 1, "settings": self.settings, "jobs": jobs})

    def snapshot(self):
        with self.lock:
            return {"tasks": [{**job, "busy": job["id"] in self.running} for job in self.jobs.values()],
                    "settings": dict(self.settings), "warning": self.storage_error,
                    "auth": self.auth.status()}

    def configure(self, settings):
        validated = self._settings(settings)
        if validated["output"]:
            folder = Path(validated["output"])
            if folder.exists() and not folder.is_dir():
                raise TransferError("保存位置不是文件夹。")
        with self.lock:
            previous = self.settings
            self.settings = validated
            try:
                self._save()
            except OSError:
                self.settings = previous
                raise
            self.budget.set_limit(validated['concurrent'])
            self._schedule()
            return self.snapshot()

    def add(self, items):
        if not isinstance(items, list) or not 1 <= len(items) <= 1000:
            raise TransferError("一次可加入 1–1000 个下载文件。")
        jobs = [self._new_job(item) for item in items]
        with self.lock:
            if len(self.jobs.keys() | {job["id"] for job in jobs}) > 1000:
                raise TransferError("队列最多保留 1000 个文件，请先移除旧记录。")
            proposed = {}
            for job in jobs:
                if job['id'] not in self.jobs:
                    proposed.setdefault(job['id'], job)
            self.jobs.update(proposed)
            try:
                self._save()
            except OSError:
                for identifier in proposed:
                    self.jobs.pop(identifier)
                raise
            return {**self.snapshot(), "added": len(proposed)}

    def preflight(self, payload=None, *, check_environment=True):
        from preflight import inspect
        payload = payload or {}
        snapshot = self.snapshot()
        return inspect(snapshot['tasks'], snapshot['settings'], snapshot['auth'],
                       action=payload.get('action', 'resume'), ids=payload.get('ids'), check_environment=check_environment)

    def action(self, action, ids=None):
        validate_action(action, ids)
        with self.lock:
            if action in ("start", "resume", "retry"):
                from preflight import ensure_ready
                ensure_ready(self.preflight({'action': action, 'ids': ids}, check_environment=False))
            self._apply_action(action, ids)
            return self.snapshot()

    def _apply_action(self, action, ids=None):
        """Apply an operation already validated by this manager or DownloadHub."""
        with self.lock:
            wanted = set(ids) if ids is not None else None
            selected = [job for key, job in self.jobs.items() if wanted is None or key in wanted]
            previous = {job['id']: dict(job) for job in selected}
            registry = dict(self.jobs)
            if action in ("start", "resume", "retry"):
                logged_in = self.auth.status()["loggedIn"]
                for output in {output_directory(job, self.settings['output']) for job in selected if job['state'] != 'completed'}:
                    if output:
                        Path(output).mkdir(parents=True, exist_ok=True)
                for job in selected:
                    if job["state"] == "completed" or job["id"] in self.running:
                        continue
                    if needs_auth(job) and not logged_in:
                        job.update(state='auth_required', error='请先在个人中心登录 NASA Earthdata。', enabled=False)
                        continue
                    if job["state"] == "queued" or action in ("resume", "retry"):
                        job["state"], job["error"] = "queued", ""
                        job["enabled"] = True
            elif action == "pause":
                for job in selected:
                    if job["state"] != "completed":
                        job["state"], job["speed"], job["enabled"] = "paused", 0, False
                        if job["id"] in self.cancels:
                            self.cancels[job["id"]].set()
            elif action == "remove":
                for job in selected:
                    if job["id"] in self.running:
                        raise TransferError("请先暂停任务，等待传输停止后再移除。")
                for job in selected:
                    del self.jobs[job["id"]]
            try:
                self._save()
            except OSError:
                # A failed start must never become enabled by a later settings save.
                # Pauses remain effective even when the disk cannot record them.
                if action != 'pause':
                    self.jobs = registry
                    for identifier, values in previous.items():
                        self.jobs[identifier].clear()
                        self.jobs[identifier].update(values)
                raise
            self._schedule()

    def clean_cache(self):
        with self.lock:
            if self.running:
                raise TransferError('请先暂停全部任务，等待下载和处理停止后再清理缓存。')
            cleanup = Cleanup()
            outputs = {job['output'] for job in self.jobs.values() if job.get('output')}
            if self.settings['output']:
                root = Path(self.settings['output'])
                outputs.update(str(path) for path in (root, root / 'orbit', root / 'dem'))
            for output in outputs:
                unfinished = [job for job in self.jobs.values()
                              if job.get('output') == output and job['state'] != 'completed']
                cleanup.tasks(Path(output) / '.sar-parts',
                              protected={job['id'] for job in unfinished if job['kind'] != 'dem-process'})
                cleanup.tasks(Path(output) / '.sar-dem-parts',
                              protected={job['id'].split(':')[1] for job in unfinished if job['kind'] == 'dem-process'})
            dem_pending = any(job['kind'] == 'dem-process' and job['state'] != 'completed' for job in self.jobs.values())
            if not dem_pending:
                cleanup.tiles(self.runtime / 'dem-cache' / 'tiles')
            # Grid data is intentionally reusable. Only completed transfer metadata is disposable.
            cleanup.completed_parts(self.runtime / 'dem-cache' / 'geoids' / '.sar-parts')
            if not cleanup.skipped:
                for job in self.jobs.values():
                    if job['state'] == 'completed':
                        job.pop('cleanupWarning', None)
                self._save()
            return {**self.snapshot(), 'cleanup': {**cleanup.report(), 'demCachePreserved': dem_pending}}

    def _clean_completed_dem(self, job):
        cleanup = Cleanup()
        cleanup.tasks(Path(job['output']) / '.sar-dem-parts', only={job['id'].split(':')[1]})
        if not any(other is not job and other['kind'] == 'dem-process' and other['state'] != 'completed'
                   for other in self.jobs.values()):
            cleanup.tiles(self.runtime / 'dem-cache' / 'tiles')
        if cleanup.skipped:
            self._update(job, cleanupWarning='部分缓存未能清理，可在任务停止后点击“清理闲置缓存”重试。')

    def _schedule(self):
        if self.closed:
            return
        for job in self.jobs.values():
            if len(self.running) >= self.settings["concurrent"]:
                break
            if job["state"] != "queued" or not job.get("enabled") or job["id"] in self.running:
                continue
            if needs_auth(job) and not self.auth.status()['loggedIn']:continue
            if job['kind'] == 'dem-process' and any(self.jobs[key]['kind'] == 'dem-process' for key in self.running):continue
            event = self.cancels[job["id"]] = threading.Event()
            thread = threading.Thread(target=self._run, args=(job, event), daemon=True)
            self.running[job["id"]] = thread
            try:
                thread.start()
            except RuntimeError:
                self.running.pop(job['id'], None)
                self.cancels.pop(job['id'], None)
                job.update(state='error', enabled=False, speed=0,
                           error='下载工作线程无法启动，请重启服务后重试。')
                self._save()
                raise

    def _update(self, job, **values):
        with self.lock:
            # A late packet must not overwrite an already requested pause.
            if job["state"] == "paused":
                values.pop("state", None)
                values["speed"] = 0
            job.update(values)

    @staticmethod
    def _wait(event, delay):
        if event.wait(delay):
            raise Paused()

    def _run(self, job, event):
        try:
            with self.budget.slot(event):
                self._execute(job, event)
        except Cancelled:
            self._update(job, state='paused', speed=0, enabled=False)
        finally:
            self._finish(job)

    def _execute(self, job, event):
        session = None
        try:
            if job['kind'] != 'dem-process':
                session = self.auth.session(burst=urlsplit(job["url"]).hostname == "sentinel1-burst.asf.alaska.edu") if job['kind'] in ('sar', 'orbit') else ScopedSession()
            with self.lock:
                if not job["output"]:
                    job["output"] = output_directory(job, self.settings["output"])
                    occupied = {other['target'] for other in self.jobs.values() if other.get('target')}
                    job["target"] = str(target_path(job, job["output"], occupied))
                self._save()
            if job['kind'] == 'dem-process':
                from dem_jobs import run_dem_job
                run_dem_job(self, job, event)
                with self.lock:
                    self._update(job, state='completed', speed=0, error='', enabled=False, cleanupWarning='')
                    self._clean_completed_dem(job)
                return
            for attempt in range(4):
                try:
                    self._transfer(job, event, session)
                    self._update(job, state="completed", speed=0, error="", enabled=False)
                    break
                except (requests.RequestException, TimeoutError, ConnectionError):
                    if event.is_set():
                        raise Paused()
                    if attempt == 3:
                        raise TransferError("网络中断，自动重试已用尽；可点击重试从断点继续。") from None
                    self._update(job, state="retrying", speed=0, error=f"网络中断，正在重试 {attempt + 1}/3")
                    self._wait(event, self.retry_delay * (2 ** attempt))
        except Paused:
            completed = job["completed"]
            if job['kind'] != 'dem-process':
                part = Path(job["output"]) / ".sar-parts" / job["id"] / "payload.part"
                completed = part.stat().st_size if part.exists() else completed
            self._update(job, state="paused", speed=0, enabled=False, completed=completed)
        except AuthError as error:
            self._update(job, state="auth_required", speed=0, error=str(error), enabled=False)
        except (TransferError, OSError) as error:
            message = str(error) if isinstance(error, TransferError) else "磁盘写入失败，请检查目录权限、剩余空间或文件占用。"
            self._update(job, state="error", speed=0, error=message, enabled=False)
        except Exception:
            self._update(job, state="error", speed=0, error="传输处理失败，已保留断点，可重试。", enabled=False)
        finally:
            if session:
                session.close()

    def _finish(self, job):
        with self.lock:
            self.running.pop(job['id'], None)
            self.cancels.pop(job['id'], None)
            try:
                self._save()
            except OSError:
                self.storage_error = '下载记录保存失败，请检查本地磁盘。'
            self._schedule()

    def _transfer(self, job, event, session):
        target = Path(job["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        part_dir = target.parent / ".sar-parts" / job["id"]
        part_dir.mkdir(parents=True, exist_ok=True)
        part, meta_path = part_dir / "payload.part", part_dir / "resume.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if target.exists():
            raise TransferError("目标文件已存在，未覆盖；请选择其他目录或移走同名文件后重试。")
        offset = part.stat().st_size if part.exists() else 0
        validator = meta.get("etag") or meta.get("lastModified")
        if meta.get("url") != job["url"] or not validator:
            offset = 0  # An unvalidated fragment must never be appended to new bytes.
        headers = {"Range": f"bytes={offset}-", "If-Range": validator} if offset else {}
        began_wait = time.monotonic()
        while True:
            if event.is_set():
                raise Paused()
            response = session.request("GET", job["url"], headers=headers, stream=True)
            if response.status_code != 202:
                break
            response.close()
            if time.monotonic() - began_wait > 1800:
                raise TransferError("Burst 生成等待超过 30 分钟，已保留任务，请稍后重试。")
            self._update(job, state="waiting", speed=0, error="ASF 正在生成 Burst，稍后自动重试。")
            self._wait(event, self.generation_delay)
        with response:
            status = response.status_code
            if status in (401, 403) or urlsplit(response.url).hostname == "urs.earthdata.nasa.gov":
                if job['kind'] not in ('sar', 'orbit'):
                    raise TransferError(f'公共源拒绝访问（HTTP {status}），请确认资源已公开或稍后重试；此任务无需 Earthdata 登录。')
                raise AuthError("下载认证失效或尚无此数据权限，请在个人中心重新登录并确认官方授权。")
            if status == 429 or 500 <= status <= 599:
                raise ConnectionError()
            if (status == 416 and offset and meta.get("total") == offset
                    and response.headers.get("Content-Range") == f"bytes */{offset}"
                    and (response.headers.get("ETag") if meta.get("etag") else response.headers.get("Last-Modified")) == validator):
                complete = True
                total = offset
            else:
                complete = False
                if status not in (200, 206):
                    raise TransferError(f"源站返回 HTTP {status}；请确认资产可用后重试。")
                media_type = response.headers.get("Content-Type", "").split(";")[0].lower()
                if media_type in ("text/html", "application/json", "text/json"):
                    if job['kind'] not in ('sar', 'orbit'):
                        raise TransferError('公共源返回了错误页面，未保存为数据文件，请稍后重试。')
                    raise AuthError("源站返回登录页面或错误信息，请重新登录后重试。")
                if response.headers.get("Content-Encoding", "identity").lower() not in ("identity", ""):
                    raise TransferError("源站返回压缩传输，无法安全校验断点。")
                if status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match or int(match[1]) != offset or int(match[2]) < offset or int(match[2]) >= int(match[3]):
                        raise TransferError("源站断点响应范围不匹配，已停止以保护文件。")
                    total = int(match[3])
                    current_validator = response.headers.get("ETag") if meta.get("etag") else response.headers.get("Last-Modified")
                    if offset and current_validator != validator:
                        raise TransferError("源文件校验标识已变化，请更换保存目录后重新下载。")
                else:
                    offset = 0  # If Range was ignored / If-Range changed, truncate rather than append.
                    length = response.headers.get("Content-Length")
                    total = int(length) if length and length.isdigit() else None
                etag = response.headers.get("ETag")
                if etag and etag.startswith("W/"):
                    etag = None
                meta = {"url": job["url"], "etag": etag,
                        "lastModified": response.headers.get("Last-Modified"), "total": total}
                atomic_json(meta_path, meta)
                self._update(job, state="downloading", completed=offset, total=total, error="")
                count, checkpoint, last_time = offset, offset, time.monotonic()
                with part.open("ab" if offset else "wb") as stream:
                    for chunk in response.iter_content(chunk_size=CHUNK):
                        if event.is_set():
                            raise Paused()
                        session.guard() if getattr(session, "guard", None) else None
                        if not chunk:
                            continue
                        stream.write(chunk)
                        count += len(chunk)
                        if total is not None and count > total:
                            raise TransferError("收到的数据超过源文件长度，已停止下载。")
                        now = time.monotonic()
                        if now - last_time >= .4:
                            self._update(job, completed=count, speed=(count - checkpoint) / (now - last_time))
                            checkpoint, last_time = count, now
                    stream.flush()
                    os.fsync(stream.fileno())
                if total is not None and count != total:
                    raise ConnectionError()
                if not count:
                    raise TransferError("源站返回空文件，请稍后重试。")
                total = count
            if event.is_set():
                raise Paused()
            meta.update(total=total, ready=True)
            atomic_json(meta_path, meta)
            # Windows rename never replaces an existing target. POSIX uses an atomic hard link.
            try:
                if os.name == "nt":
                    os.rename(part, target)
                else:
                    os.link(part, target)
                    part.unlink()
            except FileExistsError:
                raise TransferError("目标文件已存在，未覆盖。") from None
            self._update(job, completed=total, total=total, cleanupWarning='')
            cleanup = Cleanup()
            cleanup.tasks(part_dir.parent, only={job['id']})
            if cleanup.skipped:
                self._update(job, cleanupWarning='文件已完成，部分临时文件未能清理；可点击“清理闲置缓存”重试。')

    def close(self):
        with self.lock:
            self.closed = True
            for event in self.cancels.values():
                event.set()
            threads = list(self.running.values())
        for thread in threads:
            thread.join()
