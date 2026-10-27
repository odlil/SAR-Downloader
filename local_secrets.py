"""Persist sessions with DPAPI on Windows or a private local key on Linux."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import stat
import sys
import threading
from storage import atomic_write


class SettingsReadError(OSError):
    pass


def protect(data, *, decrypt=False):
    if sys.platform != 'win32':
        raise OSError('本机加密保存需要 Windows。')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError('无法使用当前 Windows 用户加密保存设置。')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class LocalSecrets:
    def __init__(self, directory):
        self.path = Path(directory) / 'private-settings.bin'
        self.lock = threading.RLock()
        self.storage_error = ''

    def _crypt(self, data, *, decrypt=False):
        if sys.platform == 'win32':
            return protect(data, decrypt=decrypt)
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError as error:
            raise OSError('本地加密组件缺失，请安装项目依赖或使用完整免安装包。') from error
        keypath = self.path.with_suffix('.key')
        try:
            descriptor = os.open(keypath, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if decrypt or self.path.exists():
                raise OSError('加密密钥缺失，已保留原设置。') from None
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            key = Fernet.generate_key()
            descriptor = os.open(keypath, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise OSError('本地加密密钥必须仅当前用户可读写（chmod 600）。')
                key = stream.read(45)
        try:
            cipher = Fernet(key)
            return cipher.decrypt(data) if decrypt else cipher.encrypt(data)
        except (InvalidToken, ValueError) as error:
            raise OSError('加密设置或密钥无效，已保留原文件。') from error

    def _read(self, *, strict=False):
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise ValueError()
            result = json.loads(self._crypt(self.path.read_bytes(), decrypt=True))
            if not isinstance(result, dict):
                raise ValueError()
            self.storage_error = ''
            return result
        except FileNotFoundError:
            self.storage_error = ''
            return {}
        except (OSError, ValueError, UnicodeError):
            self.storage_error = '原加密设置暂时无法读取，原文件已保留；请检查用户权限或恢复备份后重试。'
            if strict:
                raise SettingsReadError(self.storage_error) from None
            return {}

    def get(self, name):
        with self.lock:
            return self._read().get(name)

    def set(self, name, value):
        return self.update(name, lambda previous: value)

    def update(self, name, transform):
        """Read, transform and commit a setting under the same lock."""
        with self.lock:
            data = self._read(strict=True)
            value = transform(data.get(name))
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
            encrypted = self._crypt(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, encrypted)
            if sys.platform != 'win32':
                self.path.chmod(0o600)
            return value


class BasemapSettings:
    def __init__(self, secrets):
        self.secrets = secrets

    def get(self):
        result = self.normalize(self.secrets.get('basemap'))
        if self.secrets.storage_error:
            result['warning'] = self.secrets.storage_error
        return result

    @staticmethod
    def normalize(saved):
        if not isinstance(saved, dict):
            return {'provider': 'tianditu', 'kind': 'osm', 'keys': {}}
        keys = saved.get('keys', {})
        keys = {provider: key for provider, key in keys.items()
                if provider in ('tianditu', 'esri') and isinstance(key, str) and key.strip()} if isinstance(keys, dict) else {}
        provider = saved.get('provider') if saved.get('provider') in ('tianditu', 'esri') else 'tianditu'
        kind = 'satellite' if saved.get('kind') == 'satellite' and keys.get(provider) else 'osm'
        return {'provider': provider, 'kind': kind, 'keys': keys}

    def save(self, payload):
        from earthdata_auth import TransferError
        provider, kind = payload.get('provider'), payload.get('kind')
        key = payload.get('key')
        if provider not in ('tianditu', 'esri') or kind not in ('osm', 'satellite'):
            raise TransferError('底图设置无效。')
        if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > 8192 or any(c in key for c in '\r\n\x00')):
            raise TransferError('请填写有效的底图 Key / Token。')
        def transform(previous):
            saved = self.normalize(previous)
            if key is not None:
                saved['keys'][provider] = key.strip()
            if kind == 'satellite' and not saved['keys'].get(provider):
                raise TransferError('请先填写所选服务的 API Key / Token。')
            saved.update(provider=provider, kind=kind)
            return saved
        return self.secrets.update('basemap', transform)
