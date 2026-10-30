"""Bounded, resumable Primary-image checks. No collection or metadata writes."""
import hashlib
import json
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, quote

from .transport import Emby, HTTP, SyncError, HttpError, validate_image, jpeg, poster_url

TYPES = ('Movie', 'Series', 'Season', 'Episode')


def atomic(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_bytes(data)
    tmp.replace(path)


class MediaEmby(Emby):
    def batch(self, types, parent, offset, limit):
        rows, total, seen = [], None, set()
        while len(rows) < limit:
            params = dict(IncludeItemTypes=','.join(types), Recursive='true',
                          Fields='ProviderIds,Path,LockedFields,IsLocked,SeriesName',
                          StartIndex=offset + len(rows), Limit=min(100, limit-len(rows)),
                          SortBy='SortName', SortOrder='Ascending')
            if parent:
                params['ParentId'] = parent
            page = json.loads(self.call('/Items?' + urlencode(params)))
            chunk = page['Items']
            total = int(page['TotalRecordCount'])
            if not chunk:
                if offset + len(rows) < total:
                    raise SyncError('Emby 分页提前结束，保留检查位置')
                break
            for item in chunk:
                if item['Id'] in seen:
                    raise SyncError('Emby 分页重复，保留检查位置')
                seen.add(item['Id'])
            rows.extend(chunk)
            if offset + len(rows) >= total:
                break
        return rows, total or 0

    def image_info(self, iid):
        result = json.loads(self.call('/Items/' + quote(str(iid), safe='') + '/Images'))
        if not isinstance(result, list):
            raise SyncError('Emby 图片列表格式异常')
        return result

    def candidates(self, iid):
        result = json.loads(self.call('/Items/' + quote(str(iid), safe='') + '/RemoteImages?Type=Primary'))
        return [x for x in result['Images']
                if x.get('ProviderName') == 'TheMovieDb' and x.get('Type') == 'Primary']


class MediaEngine:
    def __init__(self, emby, data_dir, stop=None, http=None):
        self.emby, self.stop = emby, stop or threading.Event()
        self.http = http or HTTP()
        self.folder = Path(data_dir) / hashlib.sha256(emby.base.encode()).hexdigest()[:16]
        self.folder.mkdir(parents=True, exist_ok=True)

    def state(self, item):
        # Empty successful image metadata is authoritative; 500 alone is not absence.
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

    def path(self, item):
        identity = {k: item.get(k) for k in ('Id', 'Type', 'Path', 'ProviderIds', 'IndexNumber', 'ParentIndexNumber')}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return self.folder / (key + '.jpg')

    def check(self, item, repair, dry_run, backup_healthy, language):
        row = dict(id=item['Id'], name=item.get('SeriesName', '') + ' ' + item.get('Name', ''),
                   type=item.get('Type'), status='skipped')
        if item.get('Type') not in TYPES:
            return row
        if item.get('IsLocked') or 'Images' in (item.get('LockedFields') or []):
            return dict(row, status='locked')
        state, old = self.state(item)
        cache = self.path(item)
        if state == 'healthy':
            if backup_healthy and not dry_run and not self.stop.is_set():
                atomic(cache, old)
            return dict(row, status='healthy')
        if state == 'corrupt' and not repair:
            return dict(row, status='corrupt_skipped')
        if dry_run:
            return dict(row, status='would_repair' if state == 'corrupt' else 'would_fill')
        data = None
        if cache.exists():
            try:
                data = cache.read_bytes()
                validate_image(data, minimum=200)
            except (OSError, SyncError):
                data = None
        restored = data is not None
        source = 'backup'
        if data is None:
            candidates = self.emby.candidates(item['Id'])
            candidates.sort(key=lambda x: x.get('Language') not in (language, language.split('-')[0]))
            if not candidates:
                return dict(row, status='no_source')
            for candidate in candidates[:4]:
                if self.stop.is_set():
                    return dict(row, status='cancelled')
                try:
                    source = poster_url(candidate.get('Url'))
                    data = self.http.get(source, public=True)
                    validate_image(data, minimum=200)
                    break
                except SyncError:
                    data = None
            if data is None:
                raise SyncError('TMDB 候选图片下载失败；保留原图')
        fresh, current = self.state(item)
        if fresh == 'healthy':
            return dict(row, status='healthy')
        if self.stop.is_set():
            return dict(row, status='cancelled')
        if current:
            damaged = self.folder / 'damaged'
            damaged.mkdir(exist_ok=True)
            atomic(damaged / (hashlib.sha256(current).hexdigest() + '.bin'), current)
        data = jpeg(data)
        atomic(cache, data)
        self.emby.upload(item['Id'], data)
        validate_image(self.emby.image(item['Id']))
        return dict(row, status='restored' if restored else 'synced', source=source)

    def run(self, types=TYPES, parent='', batch_size=500, repair=True, dry_run=False,
            backup_healthy=False, language='zh-CN', pause=1):
        types = [t for t in TYPES if t in types]
        if not types or not 1 <= int(batch_size) <= 2000:
            raise SyncError('请选择媒体类型；每批数量应为 1 至 2000')
        scope = hashlib.sha256(json.dumps([types, parent, dry_run]).encode()).hexdigest()[:16]
        cursor = self.folder / ('cursor-' + scope + '.json')
        try:
            offset = max(0, int(json.loads(cursor.read_text())['offset']))
        except (OSError, ValueError, KeyError, TypeError):
            offset = 0
        report = dict(started=datetime.now(timezone.utc).isoformat(), items=[], dry_run=dry_run,
                      offset=offset, next_offset=offset, total=0)
        try:
            items, total = self.emby.batch(types, parent, offset, int(batch_size))
            report['total'] = total
            for item in items:
                if self.stop.is_set():
                    break
                try:
                    row = self.check(item, repair, dry_run, backup_healthy, language)
                except SyncError as exc:
                    row = dict(id=item['Id'], name=item.get('Name', ''), type=item.get('Type'), status='error', error=str(exc))
                except Exception:
                    row = dict(id=item['Id'], name=item.get('Name', ''), type=item.get('Type'), status='error', error='处理失败，未记录认证信息')
                report['items'].append(row)
                if row['status'] == 'cancelled':
                    break
                offset += 1
                atomic(cursor, json.dumps({'offset': offset}).encode())
                if self.stop.wait(pause):
                    break
            if offset >= total:
                offset = 0
                atomic(cursor, json.dumps({'offset': 0}).encode())
            report['next_offset'] = offset
        except SyncError as exc:
            report['error'] = str(exc)
        except Exception:
            report['error'] = '无法读取媒体列表或保存检查位置'
        report.update(finished=datetime.now(timezone.utc).isoformat(), cancelled=self.stop.is_set(),
                      counts=dict(Counter(x['status'] for x in report['items'])))
        return report
