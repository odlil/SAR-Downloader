"""Earthdata sessions use scoped authentication and optional encrypted persistence."""
from __future__ import annotations

import threading
from urllib.parse import urlencode, urljoin, urlsplit
import re
from http.cookiejar import Cookie

import requests
from requests.auth import AuthBase, HTTPBasicAuth
from app_runtime import VERSION
from public_assets import public_asset_kind
from dem_sources import protected_dem_url

EDL = "https://urs.earthdata.nasa.gov"
CLIENT_ID = "BO_n7nTIlMljdvU6kRRB3g"  # ASF's public Earthdata OAuth application ID.


class AuthError(Exception):
    status = 401


class TransferError(Exception):
    status = 400


class NoAuth(AuthBase):
    def __call__(self, request):
        return request  # Prevent implicit .netrc credentials on redirected requests.


def auth_host(host):
    return host == 'data.lpdaac.earthdatacloud.nasa.gov' or any(host == domain or host.endswith("." + domain)
               for domain in ("asf.alaska.edu", "earthdata.nasa.gov", "asf.earthdatacloud.nasa.gov"))


def asf_asset_host(host):
    return any(host == domain or host.endswith("." + domain)
               for domain in ("asf.alaska.edu", "asf.earthdatacloud.nasa.gov"))


def validate_url(url, *, asset=False, redirect_from=None):
    if not isinstance(url, str) or len(url) > 8192:
        raise TransferError("下载地址无效。")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        raise TransferError("下载地址端口无效。") from None
    s3 = bool(re.fullmatch(r"(?:[a-z0-9.-]+\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host))
    # ASF delegates signed image transfers to CloudFront as well as S3. Accept
    # a distribution only when reached from ASF, or within that same validated
    # distribution; it is never an accepted original queue URL or auth host.
    cloudfront = bool(re.fullmatch(r"d[a-z0-9]+\.cloudfront\.net", host))
    previous = urlsplit(redirect_from) if redirect_from else None
    previous_host = (previous.hostname or "").lower() if previous else ""
    delegated = bool(cloudfront and previous and previous.scheme == "https"
                     and (asf_asset_host(previous_host) or protected_dem_url(redirect_from) or previous_host == host))
    public = public_asset_kind(url)
    allowed = ((asf_asset_host(host) and host != 's1qc.asf.alaska.edu') or protected_dem_url(url) or public) if asset else (auth_host(host) or s3 or delegated or public)
    if parsed.scheme != "https" or not allowed or port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
        if redirect_from:
            # Never expose signed query strings, authorization codes or URL userinfo.
            display_host = host if re.fullmatch(r"[a-z0-9.-]{1,253}", host) else "无效主机"
            raise TransferError(f"下载重定向被拦截（目标主机：{display_host}）；仅允许官方 HTTPS 分发地址。")
        raise TransferError("仅支持 ASF 官方 HTTPS 影像链接及其官方下载重定向。")
    if asset and parsed.query:
        raise TransferError("请使用目录中的原始 ASF 链接，不使用临时签名地址。")
    return url


class ScopedSession:
    """Follow ASF redirects, keeping Earthdata credentials off CDN/storage hosts."""
    def __init__(self, token=None, cookies=None, basic=None, guard=None, on_auth_failure=None):
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "SAR-workbench/" + VERSION, "Accept-Encoding": "identity"})
        self.http.auth = NoAuth()
        if cookies is not None:
            self.http.cookies.update(cookies)
        self.token, self.basic, self.guard = token, basic, guard
        self.on_auth_failure = on_auth_failure

    def request(self, method, url, **kwargs):
        headers = {key: value for key, value in kwargs.pop("headers", {}).items()
                   if key.lower() not in ("authorization", "proxy-authorization", "cookie")}
        redirect_from = kwargs.pop("_redirect_from", None)
        if redirect_from:
            validate_url(redirect_from, asset=True)
        for _ in range(12):
            validate_url(url, redirect_from=redirect_from)
            if self.guard:
                self.guard()
            host = urlsplit(url).hostname.lower()
            auth = HTTPBasicAuth(*self.basic) if self.basic and host == "urs.earthdata.nasa.gov" else NoAuth()
            scoped = dict(headers)
            if self.token and auth_host(host):
                scoped["Authorization"] = "Bearer " + self.token
            response = self.http.request(method, url, auth=auth, headers=scoped,
                                         allow_redirects=False, timeout=(15, 30), **kwargs)
            if response.status_code not in (301, 302, 303, 307, 308):
                if response.status_code == 401 and auth_host(host) and self.on_auth_failure:
                    self.on_auth_failure()
                return response
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise TransferError("下载重定向缺少目标地址。")
            redirect_from, url = url, urljoin(url, location)
            if response.status_code == 303 or (method == "POST" and response.status_code in (301, 302)):
                method = "GET"
                kwargs.pop("data", None)
        raise TransferError("下载重定向次数过多，请重新登录后重试。")

    def close(self):
        self.basic = self.token = None
        self.http.cookies.clear()
        self.http.close()


class EarthdataAuth:
    def __init__(self, secrets=None):
        self.lock = threading.RLock()
        self.login_lock = threading.Lock()
        self.token = None
        self.cookies = requests.cookies.RequestsCookieJar()
        self.username = ""
        self.generation = 0
        self.burst_ready = False
        self.secrets = secrets
        if secrets:
            self._restore(secrets.get('earthdata'))

    def _restore(self, saved):
        if not isinstance(saved, dict):
            return
        token, username = saved.get('token'), saved.get('username')
        if not isinstance(token, str) or not token or len(token) > 8192 or not isinstance(username, str):
            return
        try:
            cookies = requests.cookies.RequestsCookieJar()
            for item in saved.get('cookies', []):
                cookie = Cookie(**item)
                if auth_host(cookie.domain.lstrip('.')) and not cookie.is_expired():
                    cookies.set_cookie(cookie)
        except (TypeError, ValueError, KeyError):
            return
        self.token, self.username, self.cookies = token, username, cookies
        self.burst_ready = self._burst_ready(cookies)

    @staticmethod
    def _burst_ready(cookies):
        return any(cookie.name == 'asf-urs' and not cookie.is_expired()
                   and (cookie.domain.lstrip('.') == 'asf.alaska.edu' or cookie.domain.endswith('.asf.alaska.edu'))
                   for cookie in cookies)

    def _save(self):
        if not self.secrets:
            return
        self.secrets.set('earthdata', self._session_state())

    def _session_state(self):
        fields = ('version', 'name', 'value', 'port', 'port_specified', 'domain',
                  'domain_specified', 'domain_initial_dot', 'path', 'path_specified',
                  'secure', 'expires', 'discard', 'comment', 'comment_url', 'rfc2109')
        cookies = []
        for cookie in self.cookies:
            item = {field: getattr(cookie, field) for field in fields}
            item['rest'] = dict(cookie._rest)
            cookies.append(item)
        return {'token': self.token, 'username': self.username, 'cookies': cookies}

    def worker_state(self):
        """An in-memory snapshot for a child process's private stdin pipe."""
        with self.lock:
            if not self.token:raise AuthError('请先在个人中心登录 NASA Earthdata。')
            return self._session_state()

    def status(self):
        with self.lock:
            self.burst_ready = bool(self.token) and self._burst_ready(self.cookies)
            return {"loggedIn": bool(self.token), "username": self.username,
                    "burstReady": self.burst_ready}

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password:
            raise TransferError("请填写 Earthdata 用户名和密码。")
        if len(username) > 128 or len(password) > 1024 or any(c in username + password for c in "\r\n\x00"):
            raise TransferError("账号或密码格式无效。")
        if not self.login_lock.acquire(blocking=False):
            raise TransferError("正在验证账号，请稍候。")
        with self.lock:
            generation = self.generation
        session = ScopedSession(basic=(username.strip(), password))
        try:
            with session.request("POST", EDL + "/api/users/find_or_create_token") as response:
                if response.status_code in (401, 403):
                    raise AuthError("Earthdata 账号或密码错误，或账号尚未完成启用。")
                if response.status_code != 200:
                    raise TransferError("Earthdata 登录服务暂时不可用，请稍后重试。")
                try:
                    token = response.json().get("access_token")
                except (ValueError, AttributeError):
                    token = None
                if not isinstance(token, str) or not token or len(token) > 8192:
                    raise TransferError("Earthdata 未返回有效凭据，请稍后重试。")
            query = urlencode({"splash": "false", "client_id": CLIENT_ID,
                               "response_type": "code", "redirect_uri": "https://cumulus.asf.alaska.edu/login"})
            # OAuth uses Basic only at Earthdata; ASF receives its authorization code.
            try:
                with session.request("GET", EDL + "/oauth/authorize?" + query):
                    pass
            except (requests.RequestException, TransferError):
                pass  # A valid EDL token still permits ordinary image downloads.
            burst_ready = self._burst_ready(session.http.cookies)
            with self.lock:
                if generation != self.generation:
                    raise AuthError('此次登录已取消，请重新登录。')
                self.token = token
                self.cookies = session.http.cookies.copy()
                self.username = username.strip()
                self.burst_ready = burst_ready
                self.generation += 1
                try:
                    self._save()
                except OSError:
                    result = self.status()
                    result['message'] = 'Earthdata 登录成功，但本机加密保存失败；请检查用户目录权限。'
                    return result
            result = self.status()
            result["message"] = "Earthdata 登录成功。" if burst_ready else "Earthdata 登录成功；Bursts 授权尚未完成，请在官方站点确认 ASF 应用授权后重新登录。"
            return result
        except requests.RequestException:
            raise TransferError("无法连接 Earthdata，请检查网络后重试。") from None
        finally:
            session.close()
            self.login_lock.release()

    def logout(self):
        with self.lock:
            if self.secrets:
                self.secrets.set('earthdata', None)
            self.close()
        return self.status()

    def close(self):
        """Release memory on shutdown without deleting the remembered login."""
        with self.lock:
            self.token = None
            self.cookies.clear()
            self.username = ""
            self.burst_ready = False
            self.generation += 1

    def session(self, *, burst=False):
        with self.lock:
            if not self.token:
                raise AuthError("请先在个人中心登录 NASA Earthdata。")
            if burst and not self.burst_ready:
                raise AuthError("Bursts 需要 ASF 授权 Cookie，请在官方站点授权后重新登录。")
            generation = self.generation

            def guard():
                with self.lock:
                    if generation != self.generation or not self.token:
                        raise AuthError("登录会话已变更，请重新开始下载。")

            def expired():
                with self.lock:
                    if generation != self.generation:
                        return
                    if self.secrets:
                        try:
                            self.secrets.set('earthdata', None)
                        except OSError:
                            pass  # The UI must still allow a fresh login.
                    self.close()

            return ScopedSession(self.token, self.cookies.copy(), guard=guard, on_auth_failure=expired)
