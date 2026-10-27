"""Manual release checks, with platform downloads owned by the browser.

Only public GitHub release metadata is requested here. This client is separate
from Earthdata sessions, never executes downloads, and never replaces app files.
"""
from __future__ import annotations

import json
from pathlib import Path
import platform
import re
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from app_runtime import APP_ROOT, VERSION
from earthdata_auth import TransferError
from https_runtime import https_context
from storage import atomic_json

VERSION_RE = re.compile(r'v?(\d{1,9})\.(\d{1,9})(?:\.(\d{1,9}))?', re.I)
REPOSITORY_RE = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}')
ASSET_RE = re.compile(r'SAR-v(\d{1,9}\.\d{1,9}(?:\.\d{1,9})?)-(windows-x64\.zip|linux-x86_64\.tar\.gz|source\.zip)')


class UpdateError(TransferError):
    pass


class UpdateNetworkError(UpdateError):
    pass


def parse_version(value):
    match = VERSION_RE.fullmatch(value) if isinstance(value, str) else None
    if not match:raise UpdateError('发布版本号须为 v1.1、v1.1.0 等数字版本格式。')
    return tuple(int(part or 0) for part in match.groups())


def normalize_repository(value):
    if not isinstance(value, str) or len(value) > 256:
        raise UpdateError('请填写 GitHub 仓库地址或“用户名/仓库名”。')
    value = value.strip()
    if value.startswith('https://'):
        try:
            parsed = urlsplit(value)
            if (parsed.hostname != 'github.com' or parsed.port not in (None,443) or parsed.username
                    or parsed.password or parsed.query or parsed.fragment):raise ValueError()
            value = parsed.path.strip('/')
        except ValueError:
            raise UpdateError('更新源仅支持公开 GitHub 仓库地址。') from None
    value = value.removesuffix('.git')
    if not REPOSITORY_RE.fullmatch(value) or value.split('/')[1] in ('.','..'):
        raise UpdateError('请填写 GitHub 仓库主页地址，例如 https://github.com/用户名/仓库名。')
    return value


def platform_asset():
    machine = platform.machine().lower()
    if platform.system() == 'Windows' and machine in ('amd64','x86_64'):
        return 'windows-x64.zip', 'Windows x64 免安装包'
    if platform.system() == 'Linux' and machine in ('amd64','x86_64') and platform.libc_ver()[0] == 'glibc':
        return 'linux-x86_64.tar.gz', 'Linux x86_64 免安装包'
    return 'source.zip', '源码包（需自行配置环境）'


def official_asset(asset, repository, tag, latest):
    """Accept only this release's named package on this repository's GitHub URL."""
    if not isinstance(asset, dict) or asset.get('state') != 'uploaded':return None
    name, url = asset.get('name'), asset.get('browser_download_url')
    match = ASSET_RE.fullmatch(name) if isinstance(name,str) else None
    if not match or parse_version(match[1]) != latest or not isinstance(url,str):return None
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname == 'github.com' and parsed.port in (None,443)
                 and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                 and unquote(parsed.path)[:len(repository)+1].casefold() == f'/{repository}'.casefold()
                 and unquote(parsed.path)[len(repository)+1:] == f'/releases/download/{tag}/{name}')
    except ValueError:return None
    if not valid:return None
    size = asset.get('size')
    return {'name':name,'url':url,'size':size if type(size) is int and size > 0 else None,
            'kind':match[2]}


class UpdateService:
    def __init__(self, directory, config_path=None):
        self.path = Path(directory) / 'update-settings.json'
        self.config_path = Path(config_path) if config_path else APP_ROOT / 'update_config.json'
        self.lock = threading.RLock()
        self.check_lock = threading.Lock()

    @staticmethod
    def _repository_file(path):
        try:
            with path.open('rb') as stream:raw = stream.read(4097)
        except FileNotFoundError:return ''
        if len(raw) > 4096:raise UpdateError('更新源配置文件无效，请重新设置发布仓库。')
        try:
            saved = json.loads(raw)
            value = saved.get('repository','')
            return normalize_repository(value) if value else ''
        except (ValueError,AttributeError,UnicodeError):
            raise UpdateError('更新源配置文件无效，请重新设置发布仓库。') from None

    def settings(self):
        with self.lock:
            repository = self._repository_file(self.path) if self.path.exists() else self._repository_file(self.config_path)
        kind, label = platform_asset()
        return {'currentVersion':VERSION,'repository':repository,'packageKind':kind,'packageLabel':label}

    def check(self, payload):
        if not isinstance(payload,dict) or set(payload)-{'repository'}:
            raise UpdateError('检查更新只接受 GitHub 仓库地址。')
        # An explicit new value can repair a corrupt saved setting.
        repository = normalize_repository(payload['repository']) if 'repository' in payload else self.settings()['repository']
        if not repository:raise UpdateError('尚未配置发布仓库；创建 GitHub 仓库后，请填写仓库地址再检查。')
        if not self.check_lock.acquire(blocking=False):raise UpdateError('正在检查更新，请稍候。')
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                atomic_json(self.path,{'repository':repository})
            record = self._latest(repository)
            return self._result(repository,record)
        finally:self.check_lock.release()

    @staticmethod
    def _latest(repository):
        for attempt in range(2):
            try:return UpdateService._fetch_latest(repository)
            except UpdateNetworkError:
                if attempt:raise
                time.sleep(.5)

    @staticmethod
    def _fetch_latest(repository):
        request = Request(f'https://api.github.com/repos/{repository}/releases/latest',headers={
            'Accept':'application/vnd.github+json','User-Agent':'SAR-Downloader/'+VERSION,
            'X-GitHub-Api-Version':'2026-03-10'})
        try:
            # Same verified urllib transport used by the catalog; no Earthdata
            # credentials, cookies or implicit .netrc lookup. Reject redirected
            # metadata before accepting any release information.
            with urlopen(request,context=https_context(),timeout=8) as response:
                if response.geturl() != request.full_url:
                    raise UpdateError('GitHub 仓库地址已变更，请填写新的仓库主页地址。')
                if response.status != 200:raise UpdateError(f'GitHub 更新服务暂不可用（HTTP {response.status}）。')
                raw = response.read(2*1024*1024+1)
                if len(raw)>2*1024*1024:raise UpdateError('GitHub 返回的更新信息过大，请前往发布页查看。')
                try:return json.loads(raw)
                except (ValueError,UnicodeError):raise UpdateError('GitHub 返回了无效的更新信息。') from None
        except HTTPError as error:
            error.close()
            if error.code == 404:raise UpdateError('仓库不存在、尚未发布正式 Release，或仓库不是公开仓库。') from None
            if error.code in (403,429):raise UpdateError('GitHub 暂时限制访问，请稍后重试或前往发布页查看。') from None
            if error.code in (301,302,307,308):raise UpdateError('GitHub 仓库地址已变更，请填写新的仓库主页地址。') from None
            raise UpdateError(f'GitHub 更新服务暂不可用（HTTP {error.code}）。') from None
        except (URLError,TimeoutError,OSError,ssl.SSLError):
            raise UpdateNetworkError('无法连接 GitHub，请检查网络或稍后重试。') from None

    def _result(self, repository, record):
        if not isinstance(record,dict) or record.get('draft') is not False or record.get('prerelease') is not False:
            raise UpdateError('更新源未返回正式发行版本。')
        tag = record.get('tag_name')
        latest, current = parse_version(tag), parse_version(VERSION)
        assets = record.get('assets')
        if not isinstance(assets,list) or len(assets)>500:raise UpdateError('GitHub 返回的下载列表无效。')
        packages = [item for asset in assets if (item := official_asset(asset,repository,tag,latest))]
        kind, label = platform_asset()
        selected = next((asset for asset in packages if asset['kind']==kind),None)
        source = next((asset for asset in packages if asset['kind']=='source.zip'),None)
        return {**self.settings(),'latestVersion':tag.lstrip('vV'),'updateAvailable':latest>current,
                'localNewer':current>latest,'releaseUrl':f'https://github.com/{repository}/releases/tag/{quote(tag,safe="")}',
                'releaseNotes':str(record.get('body') or '')[:30000],
                'publishedAt':str(record.get('published_at') or '')[:40],
                'package':selected,'sourcePackage':source,'packageLabel':label}
