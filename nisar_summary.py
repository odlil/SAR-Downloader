"""Human-readable, cumulative RSLC crop records; no credentials or signed URLs."""
from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

HEADER = 'SAR Downloader · NISAR RSLC 下载摘要'
SCENE_FIELDS = ('id', 'url', 'direction', 'startTime', 'stopTime', 'date', 'path', 'frame', 'platform')
STATUS = {'queued': '待下载', 'running': '下载中', 'pausing': '正在暂停', 'paused': '已暂停',
          'auth_required': '等待登录', 'error': '失败/等待重试', 'complete': '已完成', 'removed': '已移出队列（未完成）'}


def summary_record(task):
    return deepcopy({key: task[key] for key in ('id', 'config', 'output', 'status', 'created', 'summary_root')}) | {
        'scene': {key: task['scene'][key] for key in SCENE_FIELDS if key in task['scene']}}


def text(value):
    return ' '.join(str(value if value not in (None, '') else '未提供').split())[:500]


def coordinate(value):
    value = float(value)
    return f'{value:.2f}' if abs(value - round(value, 2)) < 1e-10 else f'{value:.8f}'.rstrip('0').rstrip('.')


def render_summary(root, records):
    records = sorted(records, key=lambda item: (str(item.get('created', '')), item['scene']['id']))
    complete = sum(item['status'] == 'complete' and Path(item['output']).is_file() for item in records)
    directions = Counter({'ASCENDING': 0, 'DESCENDING': 0})
    for item in records:
        directions[str(item['scene'].get('direction') or '').upper()] += 1
    lines = [HEADER, '产品：NISAR L-band RSLC · 按经纬度裁剪',
             f'更新于：{datetime.now().astimezone().isoformat(timespec="seconds")}',
             f'下载根目录：{root}', f'记录：{len(records)} 景；已完成且文件存在：{complete} 景；其余：{len(records)-complete} 景',
             f'轨道方向：升轨 {directions["ASCENDING"]} 景；降轨 {directions["DESCENDING"]} 景；未提供 {len(records)-directions["ASCENDING"]-directions["DESCENDING"]} 景',
             '经纬度采用 WGS84、十进制度；时间保留源数据的 UTC 标记。',
             '范围为请求范围；成品按原生雷达数据块向外扩展，各景尺寸可能不同，尚未进行多景配准。',
             '清单包含已提交任务；请按逐景状态区分已完成、未完成和已移除记录。', '']
    groups = {}
    for item in records:
        groups.setdefault(json.dumps(item['config'], sort_keys=True), []).append(item)
    for number, (encoded, items) in enumerate(groups.items(), 1):
        config = json.loads(encoded)
        low, high = config.get('min_height'), config.get('max_height')
        height = '全部高度层（默认）' if low is None and high is None else f'{text(low) if low is not None else "不限"} 至 {text(high) if high is not None else "不限"} m'
        corners = config['corners']
        w, e = min(p[0] for p in corners), max(p[0] for p in corners)
        s, n = min(p[1] for p in corners), max(p[1] for p in corners)
        lines += [f'范围/参数组 {number}（{len(items)} 景）',
                  f'经度：{coordinate(w)} 至 {coordinate(e)}；纬度：{coordinate(s)} 至 {coordinate(n)}',
                  '四角（经度, 纬度）：' + '；'.join(f'({coordinate(x)}, {coordinate(y)})' for x, y in corners),
                  '频率：' + ', '.join(config['frequencies']) + '；极化：' + ', '.join(config['polarizations']),
                  f'定位高程层：{height}']
        for index, item in enumerate(items, 1):
            scene = item['scene']
            direction = {'ASCENDING': '升轨', 'DESCENDING': '降轨'}.get(str(scene.get('direction') or '').upper(), '未提供')
            status = STATUS.get(item['status'], '未提供')
            if item['status'] == 'complete' and not Path(item['output']).is_file():
                status = '已完成记录，文件已移动或删除'
            lines += [f'  {index}. {text(scene["id"])}',
                      f'     状态：{status}；轨道：{direction}；Track：{text(scene.get("path"))}；Frame：{text(scene.get("frame"))}',
                      f'     成像时间：{text(scene.get("startTime") or scene.get("date"))} 至 {text(scene.get("stopTime"))}',
                      f'     文件：{item["output"]}']
        lines.append('')
    return '\n'.join(lines) + '\n'


def write_summaries(records):
    groups = {}
    for item in records:
        groups.setdefault(item['summary_root'], []).append(item)
    warnings = []
    for root, items in groups.items():
        target = Path(root) / 'summary.txt'
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            owned = not target.exists()
            if target.exists():
                with target.open('r', encoding='utf-8') as stream:
                    owned = stream.readline(len(HEADER) + 8).rstrip('\r\n') == HEADER
            if target.is_symlink() or not owned:
                raise FileExistsError('已有 summary.txt 不属于本软件，未覆盖，请先重命名该文件')
            content = render_summary(root, items)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=target.parent,
                                             prefix='.sar-summary-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        except (OSError, UnicodeError, ValueError) as error:
            warnings.append(f'下载摘要未更新：{target}（{error}）')
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    warnings.append(f'下载摘要临时文件暂无法清理：{temporary}')
    return '；'.join(warnings)
