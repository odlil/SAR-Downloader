"""Shared paths and a small cross-process lock; no third-party dependencies."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

VERSION = "1.0"
REVISION = "1.0-github-source-20261027"
APP_ID = "sar-downloader"
APP_ROOT = Path(__file__).resolve().parent
WEB_ROOT = APP_ROOT / "web"


def default_state_dir():
    if os.environ.get("SAR_STATE_DIR"):
        return Path(os.environ["SAR_STATE_DIR"]).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "SARDownloader"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SARDownloader"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / APP_ID


def picker_command(folder=False, *, check=False, orbit=False):
    arguments = ["--check"] if check else (["--orbit-folder"] if orbit else ["--folder"] if folder else [])
    if getattr(sys, "frozen", False):
        return [sys.executable, "--native-dialog", *arguments]
    return [sys.executable, "-B", str(APP_ROOT / "sar_app.py"), "--native-dialog", *arguments]


def migrate_legacy_state(destination, legacy=None):
    """Copy the old manifest once, preserving its source and all partial files."""
    destination = Path(destination) / "downloads.json"
    legacy = Path(legacy) if legacy is not None else APP_ROOT / ".runtime" / "downloads.json"
    if destination.exists() or not legacy.is_file() or destination.resolve() == legacy.resolve():
        return False
    content = legacy.read_bytes()
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("旧队列文件过大，请保留原文件并手动检查。")
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) or not isinstance(data.get("settings"), dict):
        raise ValueError("旧队列格式无法识别，请保留原文件并手动检查。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # The caller holds StateLock. Exclusive creation also prevents overwrites.
    with destination.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    return True


class StateLock:
    def __init__(self, directory):
        self.path = Path(directory) / "instance.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                stream.write(b"0");stream.flush()
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError("该用户数据目录已被另一个 SAR 服务使用，请先关闭旧启动窗口。") from None
        self.stream = stream
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None
