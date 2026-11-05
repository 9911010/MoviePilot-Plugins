"""MoviePilot V3 media image repair plugin."""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from app.sdk.plugin import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk import scheduler as scheduler_sdk

from .core import MediaEmby, MediaEngine, HTTP, TYPES, SyncError


class MediaImageRepair(_PluginBase):
    plugin_name = '影视图片修复'
    plugin_desc = '定期检查 Emby 影视图片，从 TMDB 补齐缺图，修复无效图片并保留手工封面。'
    plugin_icon = 'Emby_A.png'
    plugin_version = '1.0.2'
    plugin_author = 'Windz'
    author_url = ''
    plugin_config_prefix = 'mediaimagerepair_'
    plugin_order = 50
    auth_level = 1

    DEFAULTS = dict(enabled=False, onlyonce=False, cron='43 */6 * * *',
                    emby_url='', emby_key='', parent_id='', types=list(TYPES), batch_size=500, backup_healthy=False,
                    proxy='', language='zh-CN', repair=True, dry_run=False)

    def __init__(self):
        super().__init__()
        self._config = dict(self.DEFAULTS)
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._config_error = ''

    def init_plugin(self, config=None):
        self.stop_service()
        self._stop = threading.Event()
        self._config = dict(self.DEFAULTS, **(config or {}))
        self._config_error = ''
        try:
            CronTrigger.from_crontab(self._config['cron'], timezone=settings.TZ)
            if self._config['enabled'] or self._config['onlyonce']:
                MediaEmby(self._config['emby_url'], self._config['emby_key'])
                HTTP(proxy=self._config['proxy'] or None)
                if not self._config['types'] or not set(self._config['types']).issubset(TYPES) or not 1 <= int(self._config['batch_size']) <= 2000:
                    raise ValueError('invalid scope')
        except Exception:
            self._config_error = '配置无效：检查 Emby 地址、API Key、Cron 表达式及代理地址。'
            logger.warning(self._config_error)
        if self._config['onlyonce']:
            self._config['onlyonce'] = False
            self.update_config(dict(self._config))
            add_job = getattr(scheduler_sdk, 'add_plugin_once_job', None)
            if not self._config_error and add_job:
                ok = add_job(self.__class__.__name__, 'media_image_once', self.run_once,
                             '影视图片立即同步', delay_seconds=3)
                if ok is False:
                    self._config_error = '宿主调度器尚未启动，请稍后再次勾选立即运行。'
            elif not add_job:
                self._config_error = '当前主程序缺少一次性任务接口，请升级 V3 或从服务页面手动运行定时服务。'

    def get_state(self):
        return bool(self._config['enabled']) and not self._config_error

    @staticmethod
    def get_command():
        return []

    def get_api(self):
        return []

    def get_service(self):
        if not self.get_state():
            return []
        return [{'id': self.__class__.__name__ + '.check', 'name': '影视图片检查与补齐',
                 'trigger': CronTrigger.from_crontab(self._config['cron'], timezone=settings.TZ),
                 'func': self.run_scheduled, 'kwargs': {}}]

    def run_scheduled(self):
        if self.get_state():
            return self._run()

    def run_once(self):
        if not self._config_error:
            return self._run()

    def _run(self):
        if self._stop.is_set() or not self._run_lock.acquire(blocking=False):
            logger.info('影视图片同步：上一次任务仍在运行或已停用，本次跳过')
            return None
        stop, config = self._stop, deepcopy(self._config)
        try:
            engine = MediaEngine(MediaEmby(config['emby_url'], config['emby_key']),
                                 self.get_data_path(), stop, HTTP(proxy=config['proxy'] or None),
                                 log=lambda message: logger.info('影视图片修复：' + message))
            result = engine.run(types=config['types'], parent=config['parent_id'].strip(),
                                batch_size=int(config['batch_size']), repair=config['repair'],
                                dry_run=config['dry_run'], backup_healthy=config['backup_healthy'],
                                language=config['language'])
            # Keep reports bounded. Do not persist endpoint credentials or raw exception strings.
            history = self.get_data('history') or []
            if not isinstance(history, list):
                history = []
            history.append(result)
            self.save_data('history', history[-20:])
            # The engine logs a Chinese summary, including partial failures.
            if result.get('error'):
                logger.warning('影视图片同步：' + result['error'])
            return result
        except SyncError as exc:
            logger.warning('影视图片同步：' + str(exc))
        except Exception:
            logger.warning('影视图片同步失败，请检查插件数据目录及服务配置')
        finally:
            self._run_lock.release()

    def stop_service(self):
        self._stop.set()
        self._config['enabled'] = False
        remove_job = getattr(scheduler_sdk, 'remove_plugin_once_job', None)
        if remove_job:
            remove_job(self.__class__.__name__, 'media_image_once')
        # In-flight I/O has bounded timeouts; the run checks cancellation before every write.

    def get_form(self):
        def field(component, model, label, **props):
            return {'component': component, 'props': dict(model=model, label=label, **{k.replace('_', '-'): v for k, v in props.items()})}
        content = [
            {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
             'text': '仅管理电影、电视剧、季和单集的主图片，不处理合集、背景图、Logo。每 6 小时检查一批，默认 500 项，检查位置自动接续。'}},
            field('VSwitch', 'enabled', '启用定期检查'),
            field('VSwitch', 'onlyonce', '保存后检查一次（执行后自动关闭）'),
            field('VSwitch', 'dry_run', '只检查问题，不补图（关闭后才会修复）'),
            field('VSwitch', 'repair', '同时修复损坏图片（缺图始终会检查）'),
            field('VTextField', 'emby_url', 'Emby 服务地址', placeholder='http://192.168.1.10:8096', hint='优先填写 NAS 内网地址；不要填写 /web 网页地址。', persistent_hint=True),
            field('VTextField', 'emby_key', 'Emby API 密钥', type='password', autocomplete='off', hint='在 Emby 后台 → 高级 → API 密钥中获取。', persistent_hint=True),
            field('VTextField', 'parent_id', '限定媒体库 ID（留空检查全部媒体库）'),
            field('VSelect', 'types', '检查类型', multiple=True, chips=True, items=[{'title':n,'value':t} for n,t in zip(['电影','电视剧','季','单集'],TYPES)]),
            field('VTextField', 'batch_size', '每次最多检查多少项（1–2000）', type='number', hint='默认 500；大媒体库分多次检查，降低 NAS 和网络负担。', persistent_hint=True),
            field('VSwitch', 'backup_healthy', '备份正常图片（大型媒体库会占用较多磁盘）'),
            field('VTextField', 'cron', '检查周期（Cron）', hint='默认每天 00:43、06:43、12:43、18:43；按 MoviePilot 时区执行。每批续查，整库完成后从头循环。', persistent_hint=True),
            field('VTextField', 'proxy', 'TMDB 图片下载代理（可选）', placeholder='http://192.168.1.2:7890', hint='留空直接连接；此设置不代理 Emby 请求。', persistent_hint=True),
            field('VSelect', 'language', '海报语言', items=[{'title':'简体中文','value':'zh-CN'},
                {'title':'繁体中文','value':'zh-TW'}, {'title':'English','value':'en-US'}]),
            {'component':'VAlert','props':{'type':'warning','variant':'tonal',
             'text':'使用 Emby 已匹配条目的 TheMovieDb 图片候选；没有来源时记录跳过，不猜测片名。已有图片接口报错不覆盖。每批串行处理，避免全库并发。'}}
        ]
        # Give each control its own grid cell; host themes may hide field details.
        rows = [{'component': 'VCol', 'props': {'cols': 12, 'class': 'py-3', 'style': 'padding-top: 14px; padding-bottom: 14px;'},
                 'content': [control]} for control in content]
        return [{'component': 'VForm', 'content': [
            {'component': 'VRow', 'content': rows}]}], dict(self.DEFAULTS)

    def get_page(self):
        history = self.get_data('history') or []
        if self._config_error:
            return [{'component':'VAlert','props':{'type':'error','text':self._config_error}}]
        if not history:
            return [{'component':'VAlert','props':{'type':'info','text':'暂无运行记录。配置完成后勾选立即运行一次并保存。'}}]
        last = history[-1]
        labels = {'healthy':'正常／保留','locked':'元数据已锁定','no_source':'TMDB 暂无对应图片',
                  'corrupt_skipped':'损坏／未启用修复','would_repair':'待修复','would_fill':'待补图',
                  'restored':'从备份恢复','synced':'从 TMDB 同步','cancelled':'已取消','error':'错误'}
        counts = last.get('counts', {})
        summary = last.get('summary') or '；'.join(f'{labels.get(k,k)} {v} 项' for k,v in counts.items()) or '未检查到条目'
        if not last.get('summary') and counts.get('error'):
            summary = '检查结束，部分失败：' + summary
        try:
            moment = datetime.fromisoformat(last['finished']).astimezone(ZoneInfo(str(settings.TZ)))
            when = moment.strftime('%Y-%m-%d %H:%M:%S') + '（' + str(settings.TZ) + '）'
        except (ValueError, KeyError, TypeError):
            when = str(last.get('finished', '时间未知'))
        severity = 'error' if last.get('error') else ('warning' if counts.get('error') else 'info')
        content = [{'component':'VAlert', 'props':{'type':severity, 'variant':'tonal',
                    'text':f"最近检查：{when}。{summary} {last.get('error', '')}", 'class':'mb-4'}}]
        # Explicit cards avoid the host's data-table slot rendering dependency.
        # Errors first, with every item still visible and no English pagination.
        rows = sorted(last.get('items', []), key=lambda x: x.get('status') != 'error')
        for row in rows:
            status = labels.get(row.get('status'), '未识别状态')
            detail = row.get('message') or row.get('error') or status
            category = row.get('category') or ('电影' if row.get('type') == 'Movie' else '剧集')
            library = row.get('library') or row.get('library_error') or '媒体库未记录'
            try:
                checked = datetime.fromisoformat(row['finished']).astimezone(ZoneInfo(str(settings.TZ))).strftime('%Y-%m-%d %H:%M:%S')
                timing = f'执行完成时间：{checked}（{settings.TZ}）'
            except (ValueError, KeyError, TypeError):
                timing = '执行时间：旧记录未记录逐项时间，下次检查后显示'
            content.append({'component':'VCard', 'props':{
                'variant':'outlined', 'class':'mb-3',
                'title':f'【{category} · {library}】' + str(row.get('name') or row.get('id', '未命名条目')),
                'subtitle':status, 'text':detail + '\n' + timing,
                'style':'white-space: pre-line; overflow-wrap: anywhere;'}})
        if not rows:
            content.append({'component':'VAlert','props':{'variant':'tonal','type':'info','text':'本次没有条目明细，请查看上方错误或检查范围。'}})
        content.append({'component':'VAlert','props':{'variant':'tonal','type':'info',
                        'text':f"下次从位置 {last.get('next_offset',0)} 继续；媒体总数 {last.get('total',0)}。正常图片备份需在设置中开启。"}})
        content.append({'component':'VAlert','props':{'variant':'tonal','type':'info',
                        'text':'保留最近 20 次检查记录；修复前优先使用有效备份。缺少来源或网络失败的条目会明确记录原因。'}})
        return [{'component':'VContainer','props':{'fluid':True,
                 'style':'padding: 16px 16px 96px;'},'content':content}]
