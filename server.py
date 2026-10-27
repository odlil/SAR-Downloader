"""Local UI host for the source distribution and Windows portable release."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

from asf_api import SearchError, search_asf
from earthdata_auth import AuthError, EarthdataAuth, TransferError
from download_hub import DownloadHub
from auxiliary_api import match_orbits, preview_dem, scan_scene_folder
from last_search import read_location, remember_location
from local_secrets import LocalSecrets, BasemapSettings, SettingsReadError
from preflight import PreflightError
from app_runtime import APP_ID, VERSION, REVISION, WEB_ROOT, StateLock, default_state_dir, picker_command
from storage import atomic_json
from updates import UpdateService, UpdateError

PICKER_LOCK = threading.Lock()
SEARCH_SLOTS = threading.BoundedSemaphore(2)
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/geo+json", ".geojson")
mimetypes.add_type("application/pdf", ".pdf")


def choose_aoi(folder=False, *, check=False, orbit=False):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    process = subprocess.run(
        picker_command(folder, check=check, orbit=orbit),
        capture_output=True, encoding="utf-8", timeout=300, **options,
    )
    if process.returncode:
        raise RuntimeError("无法启动本地文件选择器。")
    return json.loads(process.stdout)


class Handler(SimpleHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        if urlsplit(self.path).path not in ("/api/downloads", "/api/account"):
            super().log_request(code, size)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def json_response(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        target = urlsplit(self.path).path
        if target in ('/help/manual-data', '/help/download'):
            try:
                with (WEB_ROOT / 'help' / 'manual.pdf').open('rb') as manual:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(os.fstat(manual.fileno()).st_size))
                    if target == '/help/download':
                        name = quote('SAR Downloader使用说明书.pdf')
                        self.send_header('Content-Disposition', f'attachment; filename="SAR-Downloader-manual.pdf"; filename*=UTF-8\'\'{name}')
                    self.end_headers()
                    self.copyfile(manual, self.wfile)
            except FileNotFoundError:
                self.send_error(404, 'User manual is not included in this installation.')
            return
        if target.startswith("/api/") and not self.local_request():
            return
        if target == "/api/health":
            return self.json_response(200, {
                "app": APP_ID, "version": VERSION, "revision": REVISION, "mode": "live-search",
                "instanceId": getattr(self.server, 'instance_id', None),
                "capabilities": {"search": True, "orbits": True, "dem": True,
                                 "downloads": True, "credentials": True, "aoiImport": True, "nisarSubset": DownloadHub.subset_available(),
                                 "orbitAuto": True, 'preflight': True, 'auxiliaryFolders': True,
                                 'nisarDEM': True, 'simpleClippedName': True, 'subsetCompanionIDs': True, 'rslcDirectorySummary': True, 'alos': True,
                                 'unfinishedCleanup': True, 'demSources': ['COP', 'NISAR', 'SRTM']},
            })
        if target == "/api/account":
            return self.json_response(200, self.server.auth.status())
        if target == '/api/updates':
            return self.update_api()
        if target == '/api/basemap':
            return self.json_response(200, self.server.basemap.get())
        if target == "/api/downloads":
            return self.json_response(200, self.server.downloads.snapshot())
        if target == '/api/last-search':
            manager=getattr(self.server,'downloads',None)
            return self.json_response(200,{'location':read_location(manager.runtime) if manager else None})
        if target.startswith("/api/"):
            return self.json_response(404, {"error": "Unknown API endpoint"})
        return super().do_GET()

    def do_POST(self):
        if not self.local_request():
            return
        target = urlsplit(self.path).path
        if target == "/api/search":
            return self.search_catalog()
        if target == "/api/aoi/open":
            return self.open_aoi()
        if target == '/api/updates/check':
            return self.update_api(check=True)
        if target in ("/api/downloads/scenes", "/api/downloads/preflight", "/api/basemap", "/api/account/login", "/api/account/logout", "/api/downloads/add",
                                      "/api/downloads/action", "/api/downloads/settings", "/api/downloads/folder",
                                      "/api/downloads/scene-bundle", "/api/nisar/subsets/add",
                                      "/api/orbits/folder", "/api/orbits/match", "/api/dem/preview", "/api/dem/add", "/api/cache/clear",
                                      "/api/cache/unfinished/preview", "/api/cache/unfinished/clear"):
            return self.download_api()
        self.close_connection = True
        return self.json_response(501, {
            "error": "该功能尚未接入。",
            "code": "ADAPTER_NOT_CONNECTED",
        })

    def local_request(self):
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in allowed_hosts or (origin and origin not in {"http://" + host for host in allowed_hosts}):
            self.close_connection = True
            self.json_response(403, {"error": "仅允许本地界面访问。"})
            return False
        return True

    def update_api(self, *, check=False):
        payload = self.read_json('更新请求格式无效。',limit=1024) if check else None
        if check and payload is None:return
        try:
            service = getattr(self.server,'updates',None)
            if service is None:
                service = self.server.updates = UpdateService(self.server.downloads.runtime)
            result = service.check(payload) if check else service.settings()
            return self.json_response(200,result)
        except UpdateError as error:
            return self.json_response(error.status,{'error':str(error)})
        except OSError:
            return self.json_response(500,{'error':'无法读取或保存更新设置，请检查用户目录权限。'})

    def read_json(self, message, limit=2 * 1024 * 1024):
        self.close_connection = True
        if self.headers.get_content_type() != "application/json":
            self.json_response(415, {"error": "需要 application/json 请求。"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= limit:
                raise ValueError()
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except (ValueError, TypeError):
            self.json_response(400, {"error": message})
            return None

    def download_api(self):
        payload = self.read_json("请求格式无效或过大。")
        if payload is None:
            return
        target = urlsplit(self.path).path
        try:
            manager = self.server.downloads
            if target == '/api/downloads/scenes':
                result = manager.add_scenes(payload)
            elif target == '/api/downloads/preflight':
                result = manager.preflight(payload)
            elif target == '/api/basemap':
                result = self.server.basemap.save(payload)
            elif target == '/api/downloads/scene-bundle':
                result = manager.scene_bundle(payload)
            elif target == '/api/nisar/subsets/add':
                result = manager.add_subset(payload)
            elif target == '/api/cache/clear':
                if payload:
                    raise TransferError('清理缓存不接受自定义目录。')
                result = manager.clean_cache()
            elif target == '/api/cache/unfinished/preview':
                result = manager.unfinished.preview(payload)
            elif target == '/api/cache/unfinished/clear':
                result = manager.unfinished.clear(payload)
            elif target == '/api/orbits/match':
                result = match_orbits(payload)
            elif target == '/api/dem/preview':
                result = preview_dem(payload)
            elif target == '/api/dem/add':
                result = manager.add([{'kind': 'dem-process', 'plan': payload}])
            elif target == "/api/account/login":
                result = self.server.auth.login(payload.get("username"), payload.get("password"))
            elif target == "/api/account/logout":
                manager.action("pause")
                result = self.server.auth.logout()
            elif target == "/api/downloads/add":
                result = manager.add(payload.get("tasks"))
            elif target == "/api/downloads/action":
                result = manager.action(payload.get("action"), payload.get("ids"))
            elif target == "/api/downloads/settings":
                result = manager.configure(payload)
            else:
                if payload:
                    raise TransferError("请通过目录选择器选择保存位置。")
                if not PICKER_LOCK.acquire(blocking=False):
                    raise TransferError("已有文件选择器打开，请先完成或取消。")
                try:
                    result = choose_aoi(folder=True, orbit=target == '/api/orbits/folder')
                    if target == '/api/orbits/folder' and result.get('output'):
                        result = scan_scene_folder(result['output'])
                finally:
                    PICKER_LOCK.release()
            return self.json_response(200, result)
        except PreflightError as error:
            return self.json_response(error.status, {'error': str(error), 'preflight': error.report})
        except (AuthError, TransferError) as error:
            return self.json_response(error.status, {"error": str(error)})
        except subprocess.TimeoutExpired:
            return self.json_response(408, {"error": "目录选择超时，请重试。"})
        except SettingsReadError as error:
            return self.json_response(409, {'error': str(error)})
        except OSError:
            return self.json_response(500, {"error": "本地文件操作失败，请检查保存目录和磁盘权限。"})
        except Exception:
            return self.json_response(500, {"error": "操作未完成，请稍后重试。"})

    def search_catalog(self):
        payload = self.read_json("检索参数无效或超过 2 MB，请简化研究区。")
        if payload is None:
            return
        if not SEARCH_SLOTS.acquire(blocking=False):
            return self.json_response(429, {"error": "本地已有两个检索请求进行中，请稍后重试。"})
        try:
            started_at=int(time.time()*1000)
            result=search_asf(payload)
            manager=getattr(self.server,'downloads',None)
            if manager:remember_location(manager.runtime,payload,result,started_at)
            return self.json_response(200,result)
        except SearchError as error:
            return self.json_response(error.status, {"error": str(error)})
        except Exception:
            return self.json_response(500, {"error": "检索处理失败，请检查研究区几何或稍后重试。"})
        finally:
            SEARCH_SLOTS.release()

    def open_aoi(self):
        payload = self.read_json("请通过文件选择器选取文件，不接受任意磁盘路径。", limit=128)
        if payload is None:
            return
        if payload:
            return self.json_response(400, {"error": "请通过文件选择器选取文件，不接受任意磁盘路径。"})
        if not PICKER_LOCK.acquire(blocking=False):
            return self.json_response(409, {"error": "文件选择器已打开，请先完成或取消当前选择。"})
        try:
            result = choose_aoi()
            return self.json_response(400 if result.get("error") else 200, result)
        except subprocess.TimeoutExpired:
            return self.json_response(408, {"error": "文件选择超时，请重新导入。"})
        except Exception:
            return self.json_response(500, {"error": "范围导入失败，请确认启动器与代码位于同一目录。"})
        finally:
            PICKER_LOCK.release()


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits a second listener to steal an occupied port.
    allow_reuse_address = sys.platform != 'win32'

    def server_bind(self):
        if sys.platform == 'win32':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind_local_server(port=None, *, preferred_port=8770):
    """Only the default port falls back; an explicitly requested port stays exact."""
    handler = partial(Handler, directory=str(WEB_ROOT))
    try:
        return LocalHTTPServer(('127.0.0.1', preferred_port if port is None else port), handler)
    except OSError:
        if port is not None:
            raise
        return LocalHTTPServer(('127.0.0.1', 0), handler)


def existing_instance(state_dir, *, attempts=10):
    """Read only this user's locked state, then verify its loopback service identity."""
    path = Path(state_dir) / 'instance.json'
    opener = build_opener(ProxyHandler({}))  # This probe is strictly loopback.
    for attempt in range(attempts):
        try:
            with path.open('rb') as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                return None
            record = json.loads(raw)
            parsed = urlsplit(record['url'])
            if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                    or not parsed.port or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment):
                return None
            if record.get('app') != APP_ID or record.get('version') != VERSION or record.get('revision') != REVISION:
                return None
            if not isinstance(record.get('instanceId'), str) or len(record['instanceId']) != 32:
                return None
            with opener.open(Request(record['url'] + '/api/health'), timeout=.5) as response:
                raw = response.read(8193)
            health = json.loads(raw) if len(raw) <= 8192 else {}
            capabilities = health.get('capabilities', {})
            if (all(health.get(key) == record[key] for key in ('app', 'version', 'revision', 'instanceId'))
                    and all(capabilities.get(key) for key in ('search', 'downloads', 'alos', 'unfinishedCleanup'))):
                return record['url']
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            if attempt + 1 < attempts:
                time.sleep(.1)
    return None


def main():
    parser = argparse.ArgumentParser(description="SAR · 本地界面")
    parser.add_argument("--port", type=int, default=None, help="Exact port (0: automatic); default: prefer 8770, fall back when occupied")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--state-dir", type=Path, help="Override the per-user queue/settings directory")
    args = parser.parse_args()
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error('端口需要为 0–65535。')
    state_dir = (args.state_dir or default_state_dir()).resolve()
    lock = StateLock(state_dir)
    try:
        lock.__enter__()
    except RuntimeError:
        url = existing_instance(state_dir)
        if url:
            print(f'SAR: {url}', flush=True)
            print('本用户已有兼容的 SAR 服务，已复用现有窗口与队列。', flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        print('该用户数据目录已有服务运行，但无法确认其版本和能力。请先暂停任务并等待停止，再关闭旧启动窗口后重试。', file=sys.stderr)
        return 1
    except OSError:
        print('无法打开用户数据目录，请检查目录权限。', file=sys.stderr)
        return 1
    server = None
    info = state_dir / 'instance.json'
    instance_id = secrets.token_hex(16)
    try:
        try:
            server = bind_local_server(args.port)
        except OSError:
            print('无法监听本机端口，请使用 --port 0 自动选择空闲端口。', file=sys.stderr)
            return 1
        try:
            private_settings = LocalSecrets(state_dir)
            server.auth = EarthdataAuth(private_settings)
            server.basemap = BasemapSettings(private_settings)
            server.downloads = DownloadHub(server.auth, state_dir)
            server.updates = UpdateService(state_dir)
            server.instance_id = instance_id
            url = f"http://127.0.0.1:{server.server_port}"
            atomic_json(info, {'app': APP_ID, 'version': VERSION, 'revision': REVISION,
                               'instanceId': instance_id, 'pid': os.getpid(), 'url': url})
            if sys.platform != 'win32':
                info.chmod(0o600)
            print(f"SAR: {url}", flush=True)
            print(f"用户数据：{state_dir}", flush=True)
            print("ASF 检索与下载服务已就绪；按 Ctrl+C 或关闭此窗口停止。", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        try:
            if server:
                if hasattr(server, 'downloads'):
                    server.downloads.close()
                if hasattr(server, 'auth'):
                    server.auth.close()
                server.server_close()
            info.unlink(missing_ok=True)
        finally:
            lock.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
