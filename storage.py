"""Atomic file writes and recoverable local queue loading."""
import json
import os
from pathlib import Path
import shutil
import time


def atomic_write(path, content):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    try:
        with temporary.open('wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def load_records(path, restore, *, field='tasks', header=None, label='裁剪'):
    """Keep valid records and back up bad input before a future save replaces it."""
    path = Path(path)
    if not path.exists():
        return [], ''
    records, invalid = [], False
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError()
        saved = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(saved, dict):
            if saved.get('version', 1) != 1:
                raise ValueError()
            if header:
                try:
                    header(saved)
                except (ValueError, TypeError, KeyError, AttributeError):
                    invalid = True
            saved = saved.get(field)
        if not isinstance(saved, list) or len(saved) > 1000:
            raise ValueError()
        for item in saved:
            try:
                records.append(restore(item))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                invalid = True
    except (OSError, ValueError, UnicodeError):
        invalid = True
    if not invalid:
        return records, ''
    backup = path.with_name(f'{path.name}.invalid-{time.time_ns()}.bak')
    try:
        shutil.copyfile(path, backup)
        warning = f'部分{label}记录无法恢复，已保留有效任务并备份原记录：{backup}'
    except OSError:
        warning = f'{label}记录无法完整恢复，原记录仍在：{path}；请检查目录权限。'
    return records, warning
