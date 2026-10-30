"""MoviePilot V3 collection poster synchronization plugin."""
from __future__ import annotations

import threading
from copy import deepcopy

from apscheduler.triggers.cron import CronTrigger
from app.sdk.plugin import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk import scheduler as scheduler_sdk

from .core import Emby, TMDB, SyncEngine, SyncError


class CollectionCoverSync(_PluginBase):
    plugin_name = '合集封面同步'
    plugin_desc = '定期检查 Emby 合集封面，从 TMDB 补齐缺图，修复无效图片并保留手工封面。'
    plugin_icon = 'Emby_A.png'
    plugin_version = '1.0.1'
    plugin_author = 'Windz'
    author_url = ''
    plugin_config_prefix = 'collectioncoversync_'
    plugin_order = 50
    auth_level = 1

    DEFAULTS = dict(enabled=False, onlyonce=False, cron='17 */6 * * *',
                    emby_url='', emby_key='', parent_id='', tmdb_key='',
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
                Emby(self._config['emby_url'], self._config['emby_key'])
                TMDB(self._config['tmdb_key'], self._config['proxy'], self._config['language'])
        except Exception:
            self._config_error = '配置无效：检查 Emby 地址、API Key、Cron 表达式及代理地址。'
            logger.warning(self._config_error)
        if self._config['onlyonce']:
            self._config['onlyonce'] = False
            self.update_config(dict(self._config))
            add_job = getattr(scheduler_sdk, 'add_plugin_once_job', None)
            if not self._config_error and add_job:
                ok = add_job(self.__class__.__name__, 'cover_sync_once', self.run_once,
                             '合集封面立即同步', delay_seconds=3)
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
        return [{'id': self.__class__.__name__ + '.check', 'name': '合集封面检查与补齐',
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
            logger.info('合集封面同步：上一次任务仍在运行或已停用，本次跳过')
            return None
        stop, config = self._stop, deepcopy(self._config)
        try:
            engine = SyncEngine(Emby(config['emby_url'], config['emby_key']),
                                TMDB(config['tmdb_key'], config['proxy'], config['language']),
                                self.get_data_path(), stop)
            result = engine.run(parent=config['parent_id'].strip(), repair=config['repair'],
                                dry_run=config['dry_run'])
            # Keep reports bounded. Do not persist endpoint credentials or raw exception strings.
            history = self.get_data('history') or []
            if not isinstance(history, list):
                history = []
            history.append(result)
            self.save_data('history', history[-20:])
            logger.info('合集封面同步完成：' + str(result['counts']))
            if result.get('error'):
                logger.warning('合集封面同步：' + result['error'])
            return result
        except SyncError as exc:
            logger.warning('合集封面同步：' + str(exc))
        except Exception:
            logger.warning('合集封面同步失败，请检查插件数据目录及服务配置')
        finally:
            self._run_lock.release()

    def stop_service(self):
        self._stop.set()
        self._config['enabled'] = False
        remove_job = getattr(scheduler_sdk, 'remove_plugin_once_job', None)
        if remove_job:
            remove_job(self.__class__.__name__, 'cover_sync_once')
        # In-flight I/O has bounded timeouts; the run checks cancellation before every write.

    def get_form(self):
        def field(component, model, label, **props):
            return {'component': component, 'props': dict(model=model, label=label, **{k.replace('_', '-'): v for k, v in props.items()})}
        content = [
            {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
             'text': '仅管理 Emby 合集主海报。有效封面（包括手工封面）不覆盖；网络错误不视为缺图。默认每 6 小时检查。'}},
            field('VSwitch', 'enabled', '启用定期检查'),
            field('VSwitch', 'onlyonce', '保存后立即运行一次'),
            field('VSwitch', 'dry_run', '仅检查，不上传图片'),
            field('VSwitch', 'repair', '修复无法解码的损坏图片'),
            field('VTextField', 'emby_url', 'Emby 服务地址', placeholder='http://192.168.1.10:8096'),
            field('VTextField', 'emby_key', 'Emby API Key', type='password', autocomplete='off'),
            field('VTextField', 'parent_id', '限定合集父目录 ID（留空检查全部合集）'),
            field('VTextField', 'cron', '检查周期（Cron）', hint='默认 17 */6 * * *；使用 MoviePilot 时区', persistent_hint=True),
            field('VTextField', 'tmdb_key', 'TMDB API Key / Read Access Token（可选）', type='password',
                  hint='留空读取 TMDB 公共合集页面；填写后优先使用官方 API', persistent_hint=True),
            field('VTextField', 'proxy', 'TMDB 专用 HTTP 代理（可选）', placeholder='http://192.168.1.2:7890'),
            field('VSelect', 'language', '海报语言', items=[{'title':'简体中文','value':'zh-CN'},
                {'title':'繁体中文','value':'zh-TW'}, {'title':'English','value':'en-US'}]),
            {'component':'VAlert','props':{'type':'warning','variant':'tonal',
             'text':'缺少 TMDB 合集编号时跳过并记录，请先在 Emby 元数据中填写编号。不会猜测匹配、修改影片元数据或触发全库重新刮削。'}}
        ]
        # Give each control its own grid cell; host themes may hide field details.
        rows = [{'component': 'VCol', 'props': {'cols': 12, 'class': 'py-3'},
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
        labels = {'healthy':'正常／保留','locked':'元数据已锁定','missing_tmdb':'缺少 TMDB 编号',
                  'corrupt_skipped':'损坏／未启用修复','would_repair':'待修复','would_fill':'待补图',
                  'restored':'从备份恢复','synced':'从 TMDB 同步','cancelled':'已取消','error':'错误'}
        rows = [dict(x, status=labels.get(x['status'],x['status'])) for x in last['items']]
        summary = '；'.join(f'{labels.get(k,k)} {v}' for k,v in last['counts'].items()) or '未检查到合集'
        return [
            {'component':'VAlert','props':{'type':'error' if last.get('error') else 'info',
             'text':f"最近检查（UTC）：{last['finished']}。{summary}。{last.get('error','')}"}},
            {'component':'VDataTable','props':{'headers':[{'title':'合集','key':'name'},
              {'title':'状态','key':'status'},{'title':'说明','key':'error'}], 'items':rows, 'itemsPerPage':25}},
            {'component':'VAlert','props':{'type':'info','text':'正常封面会备份到插件数据目录；缺失时优先恢复原图。保留最近 20 次检查记录。'}}
        ]
