"""Standalone transport and image validation for media artwork repair."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import threading
import warnings
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib import error, parse, request

from PIL import Image

MAX_BYTES = 20 * 1024 * 1024


class SyncError(Exception):
    """Sanitized error safe to put in the plugin log."""


class HttpError(SyncError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'HTTP {status}')


class SameOriginRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if parse.urlsplit(newurl)[:2] != parse.urlsplit(req.full_url)[:2]:
            raise SyncError('拒绝跨域重定向，避免发送认证信息')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HTTP:
    def __init__(self, proxy=None, timeout=20):
        self.timeout = timeout
        handlers = [SameOriginRedirect()]
        if proxy:
            if parse.urlsplit(proxy).scheme not in ('http', 'https'):
                raise SyncError('代理必须是 HTTP/HTTPS 地址')
            handlers.append(request.ProxyHandler({'http': proxy, 'https': proxy}))
        elif proxy == '':
            handlers.append(request.ProxyHandler({}))
        self.opener = request.build_opener(*handlers)
        self.proxy = proxy

    def get(self, url, headers=None, data=None, public=False):
        h = {'User-Agent': 'MediaImageRepair/1.0'}
        h.update(headers or {})
        try:
            with self.opener.open(request.Request(url, headers=h, data=data), timeout=self.timeout) as res:
                b = res.read(MAX_BYTES + 1)
                if len(b) > MAX_BYTES:
                    raise SyncError('响应超过 20MB 上限')
                return b
        except error.HTTPError as exc:
            if public and exc.code == 403 and shutil.which('curl'):
                return self._public_curl(url)
            raise HttpError(exc.code) from None
        except (error.URLError, TimeoutError, OSError):
            if public and shutil.which('curl'):
                return self._public_curl(url)
            raise SyncError('连接失败或超时，保留现有图片') from None

    def _public_curl(self, url):
        # Only public, fixed TMDB hosts; never pass Emby credentials to curl.
        if parse.urlsplit(url).hostname not in ('www.themoviedb.org', 'media.themoviedb.org', 'image.tmdb.org'):
            raise SyncError('不支持的图片来源')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'response'
            args = ['curl', '--fail', '--silent', '--show-error', '--max-time', str(self.timeout),
                    '--max-filesize', str(MAX_BYTES), '--output', str(target)]
            if self.proxy:
                args += ['--proxy', self.proxy]
            elif self.proxy == '':
                args += ['--noproxy', '*']
            try:
                p = subprocess.run(args + [url], capture_output=True, timeout=self.timeout + 3)
                if p.returncode or not target.exists():
                    raise SyncError('TMDB 公共页面下载失败')
                b = target.read_bytes()
                if len(b) > MAX_BYTES:
                    raise SyncError('响应超过 20MB 上限')
                return b
            except (OSError, subprocess.TimeoutExpired):
                raise SyncError('TMDB 公共页面下载超时') from None


def validate_image(data, minimum=1):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                if im.format not in ('JPEG', 'PNG', 'WEBP') or min(im.size) < minimum:
                    raise ValueError()
                im.verify()
            with Image.open(io.BytesIO(data)) as im:
                im.load()
                return im.size
    except Exception:
        raise SyncError('图片内容无效或无法解码') from None


def jpeg(data):
    validate_image(data, minimum=200)
    with Image.open(io.BytesIO(data)) as im:
        out = io.BytesIO()
        im.convert('RGB').save(out, 'JPEG', quality=95)
        return out.getvalue()


class PosterParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.poster = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'img' and 'poster' in a.get('class', '').split() and self.poster is None:
            self.poster = a.get('src')


def poster_url(src):
    u = parse.urlsplit(src or '')
    if u.scheme != 'https' or u.hostname not in ('image.tmdb.org', 'media.themoviedb.org'):
        raise SyncError('TMDB 图片地址无效')
    parts = u.path.split('/')
    if len(parts) != 5 or parts[1:3] != ['t', 'p'] or not parts[-1].lower().endswith(('.jpg', '.png', '.webp')):
        raise SyncError('TMDB 图片路径无效')
    return 'https://image.tmdb.org/t/p/original/' + parts[-1]


def numeric(value):
    s = str(value or '').strip()
    return s if s.isascii() and s.isdigit() else None


class Emby:
    def __init__(self, base, token, http=None):
        base = base.strip().rstrip('/')
        u = parse.urlsplit(base)
        if u.scheme not in ('http', 'https') or not u.netloc or u.query or u.fragment or u.username:
            raise SyncError('Emby 地址格式无效，请填写服务器地址')
        if not token:
            raise SyncError('请填写 Emby API Key')
        self.base = base if base.lower().endswith('/emby') else base + '/emby'
        self.token = token
        self.http = http or HTTP(proxy='')

    def call(self, path, data=None):
        headers = {'X-Emby-Token': self.token}
        if data is not None:
            headers['Content-Type'] = 'image/jpeg'
        return self.http.get(self.base + path, headers=headers, data=data)

    def collections(self, parent='', stop=None):
        offset = 0
        seen = set()
        while not stop or not stop.is_set():
            args = dict(IncludeItemTypes='BoxSet', Recursive='true', Fields='ProviderIds,ImageTags,LockedFields,IsLocked',
                        StartIndex=offset, Limit=100, SortBy='SortName', SortOrder='Ascending')
            if parent:
                args['ParentId'] = parent
            page = json.loads(self.call('/Items?' + parse.urlencode(args)))
            items = page.get('Items', [])
            if not items:
                return
            new = [c for c in items if c['Id'] not in seen]
            if not new:
                raise SyncError('Emby 分页重复，已停止')
            for c in new:
                seen.add(c['Id'])
                yield c
            offset += len(items)
            if offset >= page.get('TotalRecordCount', float('inf')):
                return

    def image(self, item_id):
        return self.call('/Items/' + parse.quote(str(item_id), safe='') + '/Images/Primary')

    def upload(self, item_id, data):
        return self.call('/Items/' + parse.quote(str(item_id), safe='') + '/Images/Primary', base64.b64encode(data))


