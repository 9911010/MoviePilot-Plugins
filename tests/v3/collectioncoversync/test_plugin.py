import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = ROOT / 'plugins.v3' / 'collectioncoversync'
# Use production import namespace, with explicit host shims only in this offline test process.
for name in ('app','app.plugins','app.sdk','app.sdk.plugin','app.sdk.config','app.sdk.logging','app.sdk.scheduler','apscheduler','apscheduler.triggers','apscheduler.triggers.cron'):
    sys.modules.setdefault(name, types.ModuleType(name))
class Base:
    def __init__(self): self.store = {}
    def update_config(self, c): self.saved = dict(c)
    def get_data(self, k): return self.store.get(k)
    def save_data(self, k, v): self.store[k] = v
class Cron:
    @staticmethod
    def from_crontab(value, **kw):
        if len(value.split()) != 5: raise ValueError()
        return value
sys.modules['app.sdk.plugin']._PluginBase = Base
sys.modules['app.sdk.config'].settings = types.SimpleNamespace(TZ='Asia/Shanghai')
sys.modules['app.sdk.logging'].logger = Mock()
sys.modules['app.sdk.scheduler'].add_plugin_once_job = Mock(return_value=True)
sys.modules['app.sdk.scheduler'].remove_plugin_once_job = Mock()
sys.modules['apscheduler.triggers.cron'].CronTrigger = Cron
spec = importlib.util.spec_from_file_location('app.plugins.collectioncoversync', PACKAGE/'__init__.py', submodule_search_locations=[str(PACKAGE)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
c = sys.modules['app.plugins.collectioncoversync.core']

def photo(color='red'):
    b=io.BytesIO(); Image.new('RGB',(300,450),color).save(b,'JPEG'); return b.getvalue()

def item(cid='87096'):
    return dict(Id='1',Name='合集',ProviderIds={'Tmdb':cid},ImageTags={})

class FakeEmby:
    base='http://emby/emby'
    def __init__(self,data=None,items=None):
        self.data=data;self.items=items or [item()];self.uploads=[];self.calls=0;self.before_second=None
    def collections(self,parent,stop):return iter(self.items)
    def image(self,ident):
        self.calls+=1
        if self.calls==2 and self.before_second:self.before_second()
        if isinstance(self.data,Exception):raise self.data
        if self.data is None:raise c.HttpError(404)
        return self.data
    def upload(self,ident,data):
        import base64
        self.uploads.append(data);self.data=data
class Source:
    def __init__(self):self.calls=0
    def poster(self,cid):
        self.calls+=1;return photo('blue'),{'tmdb':cid,'page':'https://www.themoviedb.org/collection/'+cid}

class EngineTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.src=Source()
    def tearDown(self):self.temp.cleanup()
    def run_engine(self,emby,**kw):return c.SyncEngine(emby,self.src,self.temp.name).run(pause=0,**kw)
    def test_valid_manual_image_preserved_even_without_tag(self):
        e=FakeEmby(photo());r=self.run_engine(e)
        self.assertEqual(r['counts'],{'healthy':1});self.assertFalse(e.uploads);self.assertEqual(self.src.calls,0)
    def test_missing_synced_and_verified(self):
        e=FakeEmby();r=self.run_engine(e)
        self.assertEqual(r['counts'],{'synced':1});self.assertEqual(len(e.uploads),1);self.assertGreaterEqual(e.calls,3)
    def test_html_200_repaired_and_bad_bytes_backed_up(self):
        e=FakeEmby(b'<html>404 Not Found</html>');r=self.run_engine(e)
        self.assertEqual(r['counts'],{'synced':1});self.assertTrue(list(pathlib.Path(self.temp.name).rglob('*.bin')))
    def test_transient_errors_do_not_overwrite(self):
        for err in (c.HttpError(500),c.HttpError(401),c.HttpError(403),c.SyncError('timeout')):
            e=FakeEmby(err);r=self.run_engine(e)
            self.assertEqual(r['counts'],{'error':1});self.assertFalse(e.uploads)
        self.assertEqual(self.src.calls,0)
    def test_repair_disabled(self):
        e=FakeEmby(b'html');self.assertEqual(self.run_engine(e,repair=False)['counts'],{'corrupt_skipped':1});self.assertFalse(e.uploads)
    def test_dry_run_no_download_no_write(self):
        e=FakeEmby();self.assertEqual(self.run_engine(e,dry_run=True)['counts'],{'would_fill':1});self.assertFalse(e.uploads);self.assertEqual(self.src.calls,0)
    def test_missing_id_not_guessed(self):
        e=FakeEmby(items=[item('')]);self.assertEqual(self.run_engine(e)['counts'],{'missing_tmdb':1});self.assertFalse(e.uploads)
    def test_locked_collection_preserved(self):
        i=item();i['IsLocked']=True;e=FakeEmby(items=[i]);self.assertEqual(self.run_engine(e)['counts'],{'locked':1});self.assertEqual(e.calls,0)
    def test_manual_poster_restored_after_disappearing(self):
        original=photo('green');e=FakeEmby(original);self.run_engine(e);e.data=None
        self.assertEqual(self.run_engine(e)['counts'],{'restored':1});self.assertEqual(self.src.calls,0)
        im=Image.open(io.BytesIO(e.data));self.assertGreater(im.getpixel((0,0))[1],100)
    def test_changed_tmdb_id_does_not_restore_old_series(self):
        e=FakeEmby(photo());self.run_engine(e);e.data=None;e.items=[item('656')]
        self.assertEqual(self.run_engine(e)['counts'],{'synced':1});self.assertEqual(self.src.calls,1)
    def test_user_upload_during_download_wins(self):
        e=FakeEmby();e.before_second=lambda:setattr(e,'data',photo('green'))
        self.assertEqual(self.run_engine(e)['counts'],{'healthy':1});self.assertFalse(e.uploads)
    def test_bad_download_never_uploaded(self):
        self.src.poster=lambda cid:(b'<html>404</html>',{'tmdb':cid})
        e=FakeEmby();self.assertEqual(self.run_engine(e)['counts'],{'error':1});self.assertFalse(e.uploads)
    def test_cancel_before_write(self):
        stop=threading.Event();e=FakeEmby();e.before_second=stop.set
        r=c.SyncEngine(e,self.src,self.temp.name,stop).run(pause=0)
        self.assertTrue(r['cancelled']);self.assertFalse(e.uploads)
    def test_server_cache_isolation(self):
        a=FakeEmby(photo());self.run_engine(a);b=FakeEmby();b.base='http://another/emby'
        self.assertEqual(self.run_engine(b)['counts'],{'synced':1})
    def test_failed_item_does_not_stop_remaining(self):
        e=FakeEmby(items=[item(''),dict(item(),Id='2')]);r=self.run_engine(e)
        self.assertEqual(r['counts'],{'missing_tmdb':1,'synced':1})

class ParsingTests(unittest.TestCase):
    def test_only_collection_poster_selected(self):
        p=c.PosterParser();p.feed('<img class="profile" src="actor.jpg"><img class="poster w-full" src="https://media.themoviedb.org/t/p/w300/a.jpg"><img class="poster" src="movie.jpg">')
        self.assertEqual(c.poster_url(p.poster),'https://image.tmdb.org/t/p/original/a.jpg')
    def test_remote_image_hosts_restricted(self):
        for u in ['https://evil.test/t/p/original/a.jpg','http://image.tmdb.org/t/p/original/a.jpg','https://image.tmdb.org/t/p/original/../secret']:
            with self.assertRaises(c.SyncError):c.poster_url(u)
    def test_html_and_truncated_images_rejected(self):
        for b in [b'<html>404</html>',photo()[:100]]:
            with self.assertRaises(c.SyncError):c.validate_image(b)
    def test_emby_auth_only_header_and_base_normalized(self):
        http=Mock();http.get.return_value=b'{}';e=c.Emby('http://nas:8096/emby/','secret',http);e.call('/Items')
        args,kwargs=http.get.call_args;self.assertEqual(args[0],'http://nas:8096/emby/Items');self.assertNotIn('secret',args[0]);self.assertEqual(kwargs['headers']['X-Emby-Token'],'secret')
    def test_pagination_more_than_100(self):
        http=Mock();http.get.side_effect=[json.dumps({'Items':[dict(item(),Id=str(i)) for i in range(100)],'TotalRecordCount':101}).encode(),json.dumps({'Items':[dict(item(),Id='100')],'TotalRecordCount':101}).encode()]
        self.assertEqual(len(list(c.Emby('http://nas','key',http).collections())),101)
    def test_tmdb_api_returns_collection_poster(self):
        http=Mock();http.get.side_effect=[b'{"poster_path":"/cover.jpg"}',photo()]
        b,meta=c.TMDB('key',http=http).poster('123');c.validate_image(b);self.assertEqual(meta['tmdb'],'123');self.assertIn('/collection/123?',http.get.call_args_list[0].args[0])

class LifecycleTests(unittest.TestCase):
    def test_disabled_has_no_jobs(self):
        p=plugin.CollectionCoverSync();p.init_plugin({});self.assertEqual(p.get_service(),[])
    def test_enabled_registers_cron_and_manual_once_resets(self):
        p=plugin.CollectionCoverSync();p.init_plugin({'enabled':True,'onlyonce':True,'emby_url':'http://nas','emby_key':'key'})
        self.assertEqual(len(p.get_service()),1);self.assertFalse(p.saved['onlyonce']);p.stop_service();self.assertFalse(p.get_state());self.assertTrue(p._stop.is_set())
    def test_invalid_config_no_schedule(self):
        p=plugin.CollectionCoverSync();p.init_plugin({'enabled':True,'cron':'bad'});self.assertEqual(p.get_service(),[])
    def test_no_overlap(self):
        p=plugin.CollectionCoverSync();p._run_lock.acquire();self.assertIsNone(p._run());p._run_lock.release()
    def test_version_matches_market(self):
        data=json.loads((ROOT/'package.v3.json').read_text());self.assertEqual(data['CollectionCoverSync']['version'],plugin.CollectionCoverSync.plugin_version)

if __name__=='__main__':unittest.main()
