"""One queue facade for files, auxiliary data and native RSLC subsets."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import threading
from urllib.parse import urlsplit

from auxiliary_api import dem_plan, match_orbits, preview_dem
from downloads import DownloadManager
from earthdata_auth import AuthError, TransferError, validate_url
from unfinished_cleanup import UnfinishedCleanup
from task_scheduler import validate_action
from dem_sources import needs_auth


class DownloadHub:
    def __init__(self, auth, directory):
        self.files = DownloadManager(auth, directory)
        self.auth, self.runtime = auth, self.files.runtime
        self.lock = threading.RLock()
        self.subsets = None
        self.unfinished = UnfinishedCleanup(self)
        self.subset_warning = ''
        if (self.runtime / 'nisar-subsets' / 'tasks.json').is_file():
            try:
                self._subsets()
            except TransferError as error:
                self.subset_warning = str(error)

    @staticmethod
    def subset_available():
        return all(importlib.util.find_spec(name) is not None for name in ('h5py', 'numpy'))

    def _subsets(self):
        with self.lock:
            if self.subsets is None:
                if not self.subset_available():
                    raise TransferError('NISAR 裁剪需要 h5py 和 numpy，请运行首次配置或在 gdal 环境安装项目依赖。')
                from nisar_subset_jobs import Jobs
                self.subsets = Jobs(self.runtime / 'nisar-subsets', self.auth, self.files.budget)
                self.subset_warning = ''
            return self.subsets

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _snapshot(self):
        result = self.files.snapshot()
        result['warning'] = '；'.join(filter(None, [result.get('warning'), self.subset_warning,
                                                  self.subsets.storage_error if self.subsets else '',
                                                  self.subsets.summary_warning if self.subsets else '']))
        if self.subsets is not None:
            with self.subsets.lock:
                tasks = self.subsets.snapshot()
                busy_ids = set(self.subsets.stops)
            for task in tasks:
                status = task['status']
                busy = task['id'] in busy_ids
                state = {'running': 'processing', 'pausing': 'paused', 'complete': 'completed'}.get(status, status)
                result['tasks'].append({
                    'id': 'subset-' + task['id'], 'kind': 'nisar-subset', 'state': state,
                    'title': 'NISAR 裁剪 · ' + task['scene']['id'],
                    'filename': Path(task['output']).name, 'target': task['output'],
                    'completed': task.get('downloaded', 0), 'total': task.get('total', 0) or None,
                    'speed': task.get('network_speed', 0), 'error': task.get('error', ''),
                    'phase': task.get('phase', ''), 'detail': task.get('detail', ''),
                    'networkBytes': task.get('network_bytes', 0),
                    'prepCurrent': task.get('prep_current', 0), 'prepTotal': task.get('prep_total', 0),
                    'busy': busy,
                })
        return result

    def configure(self, payload):
        with self.lock:
            self.files.configure(payload)
            return self.snapshot()

    def add(self, tasks):
        with self.lock:
            result = self.files.add(tasks)
            return {**self.snapshot(), 'added': result['added']}

    def add_scenes(self, payload):
        scenes = payload.get('scenes')
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= 500 or any(not isinstance(scene, dict) or scene.get('dataset') != 'NISAR' for scene in scenes):
            raise TransferError('请选择 1–500 景 NISAR 整景。')
        assets = [{'url': scene.get('url'), 'filename': scene.get('filename'), 'title': scene.get('name'), 'size': scene.get('size')} for scene in scenes]
        for asset in assets:
            self.files._new_job(asset)
        dem, warnings = self._nisar_dem(payload, scenes)
        result = self.add([*assets, *dem])
        return {**result, 'warnings': warnings}

    def _nisar_dem(self, payload, scenes, corners=None):
        enabled = payload.get('includeDEM', False)
        if not isinstance(enabled, bool):
            raise TransferError('DEM 下载选项无效。')
        if not enabled:
            return [], []
        if corners is not None:
            from nisar_subset_engine import validate_corners
            try:
                validate_corners(corners)
            except (ValueError, TypeError, IndexError) as error:
                raise TransferError('NISAR 裁剪范围无效。') from error
            boxes = [[min(p[0] for p in corners), min(p[1] for p in corners),
                      max(p[0] for p in corners), max(p[1] for p in corners)]]
        else:
            boxes = [dem_plan({'bounds': scene.get('bounds')})['bounds'] for scene in scenes]
        bounds = [min(box[0] for box in boxes), min(box[1] for box in boxes),
                  max(box[2] for box in boxes), max(box[3] for box in boxes)]
        plan = preview_dem({'bounds': bounds, 'buffer': 5, 'height': 'ellipsoid', 'format': 'GTiff', 'source': 'NISAR'})
        return self._dem_assets(plan)

    @staticmethod
    def _dem_assets(plan):
        """Translate one verified coverage plan into queue assets for all sensors."""
        warnings = list(plan.get('warnings', []))
        if not plan['available']:
            return [], [*warnings, 'DEM 范围内没有公开瓦片，未创建空任务。']
        if plan['format'] == 'tiles':
            return [{'kind': 'dem', 'url': tile['url'], 'filename': tile['filename'],
                     'title': 'CopDEM · ' + tile['id']} for tile in plan['tiles'] if tile['available']], warnings
        asset = {'kind': 'dem-process', 'plan': {key: plan[key] for key in ('bounds', 'buffer', 'height', 'format')}}
        if plan.get('source', 'COP') != 'COP':asset['plan']['source'] = plan['source']
        return [asset], warnings

    def add_subset(self, payload):
        with self.lock:
            return self._add_subset(payload)

    def _add_subset(self, payload):
        scenes = payload.get('scenes')
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= 500:
            raise TransferError('请选择 1–500 景 NISAR RSLC。')
        directory = self.files.snapshot()['settings']['output']
        if not directory:
            raise TransferError('请先在下载任务中设置并保存下载目录。')
        registry = {}
        for scene in scenes:
            if not isinstance(scene, dict) or scene.get('dataset') != 'NISAR' or scene.get('product') != 'RSLC':
                raise TransferError('按范围裁剪仅支持 NISAR RSLC。其他产品请选择整景下载。')
            identifier = scene.get('id')
            if not isinstance(identifier, str) or not identifier.startswith('NISAR_') or len(identifier) > 240:
                raise TransferError('NISAR 影像标识无效。')
            url = validate_url(scene.get('url'), asset=True)
            if not urlsplit(url).path.lower().endswith('.h5'):
                raise TransferError('裁剪需要 ASF 官方 RSLC HDF5 链接。')
            registry[identifier] = {key: scene[key] for key in ('id', 'direction', 'startTime', 'stopTime', 'date', 'path', 'frame', 'platform') if key in scene}
            registry[identifier]['url'] = url
        jobs = self._subsets()
        dem, warnings = self._nisar_dem(payload, scenes, payload.get('corners', []))
        # Check file-queue capacity before creating any subset tasks.
        planned = [self.files._new_job(asset) for asset in dem]
        with self.files.lock:
            if len(self.files.jobs.keys() | {job['id'] for job in planned}) > 1000:
                raise TransferError('队列最多保留 1000 个文件，请先移除旧记录。')
        with jobs.lock:
            before = len(jobs.tasks)
            try:
                identifiers = jobs.create({**payload, 'scenes': list(registry), 'directory': directory}, registry)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                raise TransferError(str(exc) if isinstance(exc, ValueError) else 'NISAR 裁剪参数无效。') from None
            added = len(jobs.tasks) - before
            ids = ['subset-' + value for value in identifiers]
        if dem:
            added += self.files.add(dem)['added']
            ids.extend(job['id'] for job in planned)
        return {**self.snapshot(), 'added': added, 'ids': ids, 'warnings': warnings}

    def action(self, action, ids=None):
        validate_action(action, ids)
        with self.lock:
            if action in ('start', 'resume', 'retry'):
                from preflight import ensure_ready
                ensure_ready(self.preflight({'action': action, 'ids': ids}))
            return self._action(action, ids)

    def preflight(self, payload=None):
        from preflight import inspect
        payload = payload or {}
        snapshot = self.snapshot()
        return inspect(snapshot['tasks'], snapshot['settings'], snapshot['auth'],
                       action=payload.get('action', 'resume'), ids=payload.get('ids'))

    def _action(self, action, ids=None):
        subset_ids = None if ids is None else [value[7:] for value in ids if value.startswith('subset-')]
        file_ids = None if ids is None else [value for value in ids if not value.startswith('subset-')]
        jobs = self.subsets
        if jobs:
            with jobs.lock:
                selected = [key for key, task in jobs.tasks.items() if subset_ids is None or key in subset_ids]
                eligible = [key for key in selected if jobs.tasks[key]['status'] != 'complete' and
                            (action != 'start' or jobs.tasks[key]['status'] == 'queued')]
                if action == 'remove' and any(key in jobs.stops for key in selected):
                    raise TransferError('请先暂停裁剪任务，等待停止后再移除。')
                if action in ('start', 'resume', 'retry') and eligible and not self.auth.status()['loggedIn']:
                    public = [task for task in self.files.snapshot()['tasks']
                              if (file_ids is None or task['id'] in file_ids)
                              and not needs_auth(task) and not task['busy']
                              and task['state'] != 'completed'
                              and (action != 'start' or task['state'] == 'queued')]
                    if not public:
                        raise AuthError('请先在个人中心登录 NASA Earthdata。')
                    for key in eligible:
                        jobs.update(jobs.tasks[key], status='auth_required', phase='需要登录',
                                    error='请先在个人中心登录 NASA Earthdata。', speed=0)
                    eligible = []
                    jobs.save()
        self.files._apply_action(action, file_ids)
        if jobs:
            with jobs.lock:
                if action in ('start', 'resume', 'retry'):
                    jobs.start_locked(eligible)
                elif action == 'pause':
                    jobs.pause_locked(selected)
                    for key in selected:
                        if key not in jobs.stops and jobs.tasks[key]['status'] != 'complete':
                            jobs.update(jobs.tasks[key], status='paused', phase='已暂停', speed=0)
                    jobs.save()
                elif action == 'remove':
                    for key in selected:
                        jobs.tasks.pop(key, None)
                    jobs.save()
        return self.snapshot()

    def clean_cache(self):
        # Subset sidecars live beside their outputs and are owned by that engine.
        with self.unfinished.locked():
            result = self.files.clean_cache()
            return {**self.snapshot(), 'cleanup': result['cleanup']}

    def scene_bundle(self, payload):
        scenes, options = payload.get('scenes'), payload.get('auxiliary', {})
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= 500 or not isinstance(options, dict):
            raise TransferError('请选择 1–500 景 Sentinel-1、ALOS 或 ALOS-2 影像。')
        assets, warnings, counts = [], [], {'imagery': 0, 'orbit': 0, 'dem': 0}
        for scene in scenes:
            if not isinstance(scene, dict) or scene.get('dataset') not in ('SENTINEL-1', 'SLC-BURST', 'ALOS', 'ALOS-2'):
                raise TransferError('配套数据下载仅适用于 Sentinel-1、ALOS 或 ALOS-2。')
            if scene.get('url'):
                assets.append({'url': scene['url'], 'filename': scene.get('filename'), 'title': scene.get('name'), 'size': scene.get('size')})
            if scene.get('dataset') == 'SLC-BURST' and payload.get('includeXML'):
                assets.extend({'url': url, 'title': str(scene.get('name', 'Burst')) + ' · XML'} for url in scene.get('xmlUrls', []))
        if not assets:
            raise TransferError('所选记录没有可下载影像资产。')
        # Validate before contacting catalogs, and commit only after all lookups succeed.
        for asset in assets:
            validate_url(asset['url'], asset=True)
        counts['imagery'] = len(assets)
        acquisitions = [{'id': scene.get('name') or scene.get('id'), 'platform': scene.get('platform'),
                         'start': scene.get('startTime'), 'end': scene.get('stopTime')} for scene in scenes]
        # Legacy pages may still submit both old flags until refreshed. They
        # receive the same precise-first behavior as the new single checkbox.
        include_orbit = options.get('orbit', bool(options.get('precise') or options.get('restituted')))
        if include_orbit:
            if any(scene['dataset'] not in ('SENTINEL-1', 'SLC-BURST') for scene in scenes):
                raise TransferError('自动匹配精密 / 恢复轨道仅适用于 Sentinel-1；ALOS / ALOS-2 使用产品内的轨道信息。')
            result = match_orbits({'scenes': acquisitions, 'strategy': 'auto'})
            assets.extend(result['assets'])
            counts['orbit'] = result['files']
            if result['missing']:
                warnings.append(f"轨道：{result['missing']} 景暂未找到可用的精密或恢复轨道，未创建空任务。")
        if options.get('dem'):
            dem = options.get('demOptions', {})
            if not isinstance(dem, dict):
                raise TransferError('DEM 参数无效。')
            if dem.get('extent') == 'aoi':
                bounds = payload.get('bounds')
            else:
                try:
                    boxes = [scene['bounds'] for scene in scenes]
                    bounds = [min(box[0] for box in boxes), min(box[1] for box in boxes),
                              max(box[2] for box in boxes), max(box[3] for box in boxes)]
                except (KeyError, TypeError, IndexError):
                    raise TransferError('影像缺少有效覆盖范围，无法准备 DEM。') from None
            plan = preview_dem({'bounds': bounds, 'buffer': dem.get('buffer', 5),
                                'source': dem.get('source', 'COP'),
                                'height': dem.get('height', 'ellipsoid'), 'format': dem.get('format', 'GTiff')})
            dem_assets, dem_warnings = self._dem_assets(plan)
            assets.extend(dem_assets)
            warnings.extend(dem_warnings)
            counts['dem'] = len(dem_assets)
        result = self.add(assets)
        return {**result, 'warnings': warnings, 'bundle': counts}

    def close(self):
        if self.subsets:
            self.subsets.request_shutdown()
        self.files.close()
        if self.subsets:
            self.subsets.shutdown()
