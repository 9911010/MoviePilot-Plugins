"""Emby box-set artwork repair; independent of MoviePilot for offline tests."""
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
        super().__init__({401: '认证失败（HTTP 401），请检查 API Key', 403: '访问被拒绝（HTTP 403），请检查权限或网络限制', 404: '资源不存在（HTTP 404），请核对编号或服务地址', 429: '请求过于频繁（HTTP 429），请稍后重试'}.get(status, f'服务返回异常（HTTP {status}）'))


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
        h = {'User-Agent': 'CollectionCoverSync/1.0'}
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

    def image_info(self, item_id):
        result = json.loads(self.call('/Items/' + parse.quote(str(item_id), safe='') + '/Images'))
        if not isinstance(result, list):
            raise SyncError('Emby 图片记录格式异常，未修改封面')
        return result

    def image(self, item_id):
        return self.call('/Items/' + parse.quote(str(item_id), safe='') + '/Images/Primary')

    def upload(self, item_id, data):
        return self.call('/Items/' + parse.quote(str(item_id), safe='') + '/Images/Primary', base64.b64encode(data))


class TMDB:
    def __init__(self, key='', proxy='', language='zh-CN', http=None):
        self.key = key.strip()
        self.language = language
        self.http = http or HTTP(proxy=proxy or None)

    def poster(self, collection_id, progress=None):
        progress = progress or (lambda message: None)
        cid = numeric(collection_id)
        if not cid:
            raise SyncError('缺少有效的 TMDB 合集编号')
        page_url = 'https://www.themoviedb.org/collection/' + cid + '?' + parse.urlencode({'language': self.language})
        if self.key:
            api = 'https://api.themoviedb.org/3/collection/' + cid
            params = {'language': self.language}
            headers = {}
            if len(self.key) > 40:
                headers['Authorization'] = 'Bearer ' + self.key
            else:
                params['api_key'] = self.key
            result = json.loads(self.http.get(api + '?' + parse.urlencode(params), headers=headers))
            path = result.get('poster_path')
            if not path:
                raise SyncError('TMDB 合集尚无封面')
            src = poster_url('https://image.tmdb.org/t/p/original/' + str(path).lstrip('/'))
        else:
            raw = self.http.get(page_url, public=True).decode('utf-8')
            parser = PosterParser()
            parser.feed(raw)
            if not parser.poster:
                raise SyncError('TMDB 合集尚无封面或页面结构变化')
            src = poster_url(parser.poster)
        progress('搜索成功，已找到封面；正在下载图片')
        return jpeg(self.http.get(src, public=True)), {'page': page_url, 'image': src, 'tmdb': cid}


class SyncEngine:
    def __init__(self, emby, source, data_dir, stop=None, log=None):
        self.log = log or (lambda message: None)
        self.emby, self.source = emby, source
        # Isolate caches between media servers (including instances using the same directory in tests).
        scope = hashlib.sha256(emby.base.encode()).hexdigest()[:16]
        self.folder = Path(data_dir) / scope
        self.folder.mkdir(parents=True, exist_ok=True)
        self.stop = stop or threading.Event()

    def _paths(self, item):
        key = hashlib.sha256(str(item['Id']).encode()).hexdigest()
        return self.folder / (key + '.jpg'), self.folder / (key + '.json')

    def _cache(self, item, data, meta):
        path, manifest = self._paths(item)
        tmp = path.with_suffix('.tmp')
        tmp.write_bytes(data)
        tmp.replace(path)
        meta = dict(meta, sha256=hashlib.sha256(data).hexdigest())
        tmp = manifest.with_suffix('.tmp')
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        tmp.replace(manifest)

    def _cached(self, item, cid):
        path, manifest = self._paths(item)
        try:
            meta = json.loads(manifest.read_text(encoding='utf-8'))
            if meta.get('tmdb') != cid:
                return None
            b = path.read_bytes()
            if hashlib.sha256(b).hexdigest() != meta.get('sha256'):
                return None
            validate_image(b)
            return b, meta
        except (OSError, ValueError, SyncError):
            return None

    def _state(self, item):
        info = self.emby.image_info(item['Id'])
        if not any(x.get('ImageType') == 'Primary' for x in info):
            return 'missing', None
        try:
            data = self.emby.image(item['Id'])
        except HttpError as exc:
            if exc.status == 404:
                return 'missing', None
            raise
        try:
            validate_image(data)
            return 'healthy', data
        except SyncError:
            return 'corrupt', data

    def run(self, parent='', repair=True, dry_run=False, pause=1.0):
        self.log('开始检查合集封面' + ('（仅检查模式，不导入图片）' if dry_run else ''))
        report = {'started': datetime.now(timezone.utc).isoformat(), 'dry_run': dry_run, 'items': []}
        try:
            for item in self.emby.collections(parent, self.stop):
                if self.stop.is_set():
                    break
                row = {'id': item['Id'], 'name': item.get('Name', ''), 'status': 'skipped'}
                stage = '检查现有封面'
                name = str(item.get('Name') or item['Id']).replace('\n', ' ').replace('\r', ' ')
                def progress(message):
                    self.log(f'【{name}】{message}')
                progress(stage)
                try:
                    cid = numeric(item.get('ProviderIds', {}).get('Tmdb'))
                    if item.get('IsLocked') or 'Images' in item.get('LockedFields', []):
                        row['status'] = 'locked'
                    else:
                        state, old = self._state(item)
                        if state != 'healthy':
                            progress('发现缺少封面' if state == 'missing' else '发现封面损坏，无法解码')
                        if state == 'healthy':
                            row['status'] = 'healthy'
                            # Back up existing manual posters as well, never replace them with TMDB artwork.
                            self._cache(item, old, {'tmdb': cid, 'source': 'existing'})
                        elif state == 'corrupt' and not repair:
                            row['status'] = 'corrupt_skipped'
                        elif not cid:
                            row['status'] = 'missing_tmdb'
                        elif dry_run:
                            row['status'] = 'would_repair' if state == 'corrupt' else 'would_fill'
                        else:
                            cached = self._cached(item, cid)
                            stage = '从备份读取封面' if cached else '查询 TMDB 并下载封面'
                            progress('找到本地备份，准备恢复' if cached else f'正在查询 TMDB 合集编号 {cid}')
                            def source_progress(message):
                                nonlocal stage
                                stage = '下载 TMDB 封面'
                                progress(message)
                            if cached:
                                data, meta = cached
                            elif isinstance(self.source, TMDB):
                                data, meta = self.source.poster(cid, progress=source_progress)
                            else:
                                data, meta = self.source.poster(cid)
                            progress('封面已读取，正在校验图片')
                            validate_image(data)
                            # Recheck directly before mutation: user may have added a poster during download.
                            stage = '导入前复查 Emby 封面'
                            fresh, latest = self._state(item)
                            if fresh == 'healthy':
                                row['status'] = 'healthy'
                                self._cache(item, latest, {'tmdb': cid, 'source': 'existing'})
                            elif self.stop.is_set():
                                row['status'] = 'cancelled'
                            else:
                                if old:
                                    backup = self.folder / 'damaged'
                                    backup.mkdir(exist_ok=True)
                                    digest = hashlib.sha256(old).hexdigest()
                                    (backup / (digest + '.bin')).write_bytes(old)
                                # Cache before uploading so a timeout can be recovered next time.
                                stage = '保存本地封面备份'
                                self._cache(item, data, meta)
                                stage = '导入封面到 Emby'
                                progress('正在导入封面到 Emby')
                                self.emby.upload(item['Id'], jpeg(data))
                                stage = '导入后验证封面'
                                progress('上传请求完成，正在验证 Emby 图片能否正常读取')
                                validate_image(self.emby.image(item['Id']))
                                row['status'] = 'restored' if cached else 'synced'
                                row['source'] = meta.get('page', meta.get('source', 'cache'))
                except SyncError as exc:
                    row.update(status='error', error=f'{stage}失败：{exc}')
                except Exception:
                    row.update(status='error', error=f'{stage}失败：本地读写或服务返回格式异常；请检查数据目录权限及服务日志')
                messages = {
                    'healthy': '封面正常，保留现有图片', 'locked': '跳过：合集图片或元数据已锁定',
                    'missing_tmdb': '跳过：缺少有效 TMDB 合集编号，请在 Emby 元数据中填写；本插件不按名称猜测匹配',
                    'corrupt_skipped': '跳过：封面损坏，但未开启损坏图片修复',
                    'would_repair': '检查完成：需要修复，当前仅检查模式，未导入',
                    'would_fill': '检查完成：需要补图，当前仅检查模式，未导入',
                    'restored': '成功：已从备份恢复封面，并验证可正常读取',
                    'synced': '成功：导入封面完成，并验证可正常读取', 'cancelled': '已取消：未导入封面'}
                row['message'] = row.get('error') or messages.get(row['status'], row['status'])
                progress(row['message'])
                report['items'].append(row)
                if self.stop.wait(pause):
                    break
        except SyncError as exc:
            report['error'] = str(exc)
        except Exception:
            report['error'] = '无法读取合集列表，请检查 Emby 连接与 API 权限'
        report['cancelled'] = self.stop.is_set()
        report['finished'] = datetime.now(timezone.utc).isoformat()
        report['counts'] = {s: sum(i['status'] == s for i in report['items']) for s in sorted({i['status'] for i in report['items']})}
        counts = report['counts']
        success = counts.get('synced', 0) + counts.get('restored', 0)
        failed = counts.get('error', 0)
        healthy = counts.get('healthy', 0)
        skipped = len(report['items']) - success - failed - healthy
        outcome = '任务已取消' if report['cancelled'] else ('任务未完整完成' if report.get('error') else ('检查结束，存在失败项' if failed else '检查完成'))
        report['summary'] = f'{outcome}：检查 {len(report["items"])} 个合集，成功补图/恢复 {success} 个，封面正常 {healthy} 个，跳过/待处理 {skipped} 个，失败 {failed} 个。'
        if report.get('error'):
            report['summary'] += '读取合集列表失败：' + report['error']
        self.log(report['summary'])
        return report
