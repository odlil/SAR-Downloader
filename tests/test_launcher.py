"""Run with the gdal/pytorch Python on Windows; exercises the actual CMD launcher."""
from __future__ import annotations

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
import unittest
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "启动界面.cmd"


def command_for(launcher: Path, state: Path) -> str:
    cmd = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    return f'"{cmd}" /d /s /c ""{launcher}" --no-browser --port 0 --state-dir "{state}""'


@unittest.skipUnless(sys.platform == "win32", "Exercises Windows cmd.exe")
class LauncherTests(unittest.TestCase):
    def test_batch_is_ascii_with_windows_line_endings(self):
        for script in (LAUNCHER, ROOT / "启动SAR Downloader.cmd", ROOT / "首次配置.cmd", ROOT / "配置DEM环境.cmd", ROOT / "scripts" / "find_python.cmd"):
            content = script.read_bytes()
            content.decode("ascii")
            self.assertIn(b"\r\n", content)
            self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
            self.assertNotIn(b"chcp", content.lower())

    def assert_starts(self, launcher: Path, *, custom_python: bool):
        env = os.environ.copy()
        env.pop("SAR_PYTHON", None)
        if custom_python:
            env["SAR_PYTHON"] = sys.executable
        env["SAR_NO_PAUSE"] = "1"
        state = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        process = subprocess.Popen(
            command_for(launcher, Path(state.name)), cwd=ROOT.parent, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        lines = queue.Queue()

        def read_output():
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        captured = []
        try:
            deadline = time.monotonic() + 20
            url = None
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                if line is None:
                    break
                captured.append(line)
                match = re.search(r"http://127\.0\.0\.1:\d+", line)
                if match:
                    url = match.group()
                    break
            self.assertIsNotNone(url, "Launcher did not start:\n" + "".join(captured))
            self.assertRegex("".join(captured), r"(?i)\[SAR(?: Downloader)?\] Python: .+python\.exe")
            opener = build_opener(ProxyHandler({}))
            with opener.open(url + "/api/health", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)["mode"], "live-search")
            with opener.open(url + "/", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("SAR", response.read().decode("utf-8"))
        finally:
            # Only terminate the exact process tree this test started.
            if process.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW, check=False,
                )
            process.wait(timeout=10)
            reader.join(timeout=3)
            process.stdout.close()
            state.cleanup()

    def test_default_launcher_from_another_working_directory(self):
        self.assert_starts(LAUNCHER, custom_python=False)

    def test_new_named_launcher(self):
        self.assert_starts(ROOT / "启动SAR Downloader.cmd", custom_python=True)

    def test_chinese_and_spaced_app_path_with_custom_environment(self):
        folder = Path(tempfile.mkdtemp(prefix="启动 测试 ", dir=ROOT / "tests")).resolve()
        self.assertTrue(folder.is_relative_to(ROOT.resolve()))
        try:
            launcher = folder / LAUNCHER.name
            shutil.copyfile(LAUNCHER, launcher)
            for module in ('storage.py', 'task_scheduler.py', 'preflight.py', 'https_runtime.py', 'dem_sources.py', 'updates.py'):
                shutil.copyfile(ROOT / module, folder / module)
            for module in ("server.py", "sar_app.py", "app_runtime.py", "check_environment.py", "asf_api.py", "aoi_import.py", "aoi_dialog.py", "native_dialog.py", "earthdata_auth.py", "local_secrets.py", "downloads.py", "auxiliary_api.py", "public_assets.py", "dem_jobs.py", "dem_worker.py", "last_search.py", "cache_cleanup.py", "unfinished_cleanup.py", "download_hub.py", "nisar_subset_jobs.py", "nisar_subset_engine.py"):
                shutil.copyfile(ROOT / module, folder / module)
            shutil.copytree(ROOT / "web", folder / "web")
            (folder / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts" / "find_python.cmd", folder / "scripts" / "find_python.cmd")
            self.assert_starts(launcher, custom_python=True)
        finally:
            shutil.rmtree(folder)


if __name__ == "__main__":
    unittest.main(verbosity=2)
