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
                          Fields='ProviderIds,Path,ParentId,LockedFields,IsLocked,SeriesName',
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

    def library_name(self, item):
        # Virtual-folder paths are Emby's actual library configuration, not genres.
        if not hasattr(self, '_libraries'):
            folders = json.loads(self.call('/Library/VirtualFolders'))
            if not isinstance(folders, list):
                raise SyncError('Emby 媒体库列表格式异常')
            self._libraries = folders
            self._library_cache = {}
        path = str(item.get('Path') or '').replace('\\', '/').rstrip('/')
        matches = []
        for folder in self._libraries:
            for location in folder.get('Locations') or []:
                root = str(location).replace('\\', '/').rstrip('/')
                if root and (path == root or path.startswith(root + '/')):
                    matches.append((len(root), folder.get('Name', '')))
        if matches:
            longest = max(length for length, name in matches)
            return '、'.join(sorted({name for length, name in matches if length == longest and name}))
        key = str(item.get('ParentId') or item['Id'])
        if key not in self._library_cache:
            ancestors = json.loads(self.call('/Items/' + quote(str(item['Id']), safe='') + '/Ancestors'))
            if not isinstance(ancestors, list):
                raise SyncError('Emby 上级目录格式异常')
            self._library_cache[key] = next((x.get('Name', '') for x in ancestors
                                            if x.get('Type') == 'CollectionFolder'), '')
        return self._library_cache[key]

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
    def __init__(self, emby, data_dir, stop=None, http=None, log=None):
        self.emby, self.stop = emby, stop or threading.Event()
        self.http = http or HTTP()
        self.log = log or (lambda message: None)
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
        self._stage = '检查现有图片'
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
        self.log('发现图片缺失' if state == 'missing' else '发现图片损坏')
        self._stage = '读取图片备份'
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
            self._stage = '查询 TMDB 图片候选'
            self.log('正在通过 Emby 查询已匹配条目的 TMDB 图片')
            candidates = self.emby.candidates(item['Id'])
            candidates.sort(key=lambda x: x.get('Language') not in (language, language.split('-')[0]))
            if not candidates:
                return dict(row, status='no_source')
            self.log(f'搜索成功，找到 {len(candidates)} 张候选图片；开始下载（最多尝试 4 张）')
            self._stage = '下载 TMDB 图片'
            failures = []
            for index, candidate in enumerate(candidates[:4], 1):
                if self.stop.is_set():
                    return dict(row, status='cancelled')
                try:
                    source = poster_url(candidate.get('Url'))
                    data = self.http.get(source, public=True)
                    validate_image(data, minimum=200)
                    break
                except SyncError as exc:
                    failures.append(str(exc))
                    self.log(f'第 {index} 张下载或校验失败：{exc}')
                    data = None
            if data is None:
                raise SyncError('候选图片均下载或校验失败：' + '；'.join(dict.fromkeys(failures)))
        if restored:
            self.log('找到有效备份，准备恢复原图')
        self._stage = '导入前复查现有图片'
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
        self._stage = '导入图片到 Emby'
        self.log('图片已准备好，正在导入 Emby')
        self.emby.upload(item['Id'], data)
        self._stage = '验证导入结果'
        self.log('导入请求已完成，正在重新读取图片验证')
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
        self.log(f'开始检查，从第 {offset + 1} 项继续；本批最多 {batch_size} 项' + ('（仅检查，不上传）' if dry_run else ''))
        try:
            items, total = self.emby.batch(types, parent, offset, int(batch_size))
            report['total'] = total
            for item in items:
                if self.stop.is_set():
                    break
                name = str(item.get('SeriesName', '') + ' ' + item.get('Name', '')).replace('\n', ' ').strip()
                started = datetime.now(timezone.utc).isoformat()
                category = '电影' if item.get('Type') == 'Movie' else '剧集'
                library, library_error = '', ''
                try:
                    library = self.emby.library_name(item)
                except Exception:
                    library_error = '媒体库分类读取失败'
                label = f'{category} · {library or library_error or "未识别媒体库"}'
                self.log(f'【{label}】【{name}】开始检查')
                self._stage = '检查条目'
                try:
                    row = self.check(item, repair, dry_run, backup_healthy, language)
                except SyncError as exc:
                    row = dict(id=item['Id'], name=item.get('Name', ''), type=item.get('Type'), status='error', error=f'{self._stage}失败：{exc}')
                except Exception:
                    row = dict(id=item['Id'], name=item.get('Name', ''), type=item.get('Type'), status='error', error='处理失败，未记录认证信息')
                messages = {'healthy':'图片正常，保留现有图片', 'locked':'已锁定图片或元数据，跳过',
                    'no_source':'Emby 未返回 TMDB 图片候选；请检查该条目的 TMDB 编号及图片提供器，不能据此判断片名错误',
                    'corrupt_skipped':'图片损坏，但未启用损坏修复', 'would_repair':'发现损坏图片；仅检查模式未执行修复',
                    'would_fill':'发现缺图；仅检查模式未下载或导入', 'restored':'从备份恢复完成，重新读取验证成功',
                    'synced':'TMDB 图片导入完成，重新读取验证成功', 'cancelled':'任务已取消', 'skipped':'类型不在检查范围'}
                row.update(name=name, category=category, library=library, library_error=library_error,
                           started=started, finished=datetime.now(timezone.utc).isoformat())
                row['message'] = row.get('error') or messages.get(row['status'], '未知状态')
                self.log(f'【{label}】【{name}】{row["message"]}')
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
        counts = report['counts']
        success = counts.get('synced', 0) + counts.get('restored', 0)
        title = '任务已取消' if report['cancelled'] else ('任务失败' if report.get('error') else ('检查结束，部分失败' if counts.get('error') else '检查完成'))
        report['summary'] = f'{title}：检查 {len(report["items"])} 项，补图成功 {success} 项，正常保留 {counts.get("healthy", 0)} 项，失败 {counts.get("error", 0)} 项，其余为跳过或仅检查。'
        self.log(report['summary'] + report.get('error', ''))
        return report
