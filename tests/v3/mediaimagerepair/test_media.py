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
PACKAGE = ROOT / 'plugins.v3' / 'mediaimagerepair'
for name in ('app','app.plugins','app.sdk','app.sdk.plugin','app.sdk.config','app.sdk.logging','app.sdk.scheduler','apscheduler','apscheduler.triggers','apscheduler.triggers.cron'):
    sys.modules.setdefault(name, types.ModuleType(name))
class Base:
    def __init__(self): self.store={}
    def update_config(self,c): self.saved=c
    def get_data(self,k): return self.store.get(k)
    def save_data(self,k,v): self.store[k]=v
sys.modules['app.sdk.plugin']._PluginBase=Base
sys.modules['app.sdk.config'].settings=types.SimpleNamespace(TZ='Asia/Shanghai')
sys.modules['app.sdk.logging'].logger=Mock()
sys.modules['app.sdk.scheduler'].add_plugin_once_job=Mock(return_value=True)
sys.modules['app.sdk.scheduler'].remove_plugin_once_job=Mock()
sys.modules['apscheduler.triggers.cron'].CronTrigger=types.SimpleNamespace(from_crontab=lambda *a,**kw: a[0])
spec=importlib.util.spec_from_file_location('app.plugins.mediaimagerepair',PACKAGE/'__init__.py',submodule_search_locations=[str(PACKAGE)])
plugin=importlib.util.module_from_spec(spec);sys.modules[spec.name]=plugin;spec.loader.exec_module(plugin)
c=sys.modules['app.plugins.mediaimagerepair.core']
def photo():
    out=io.BytesIO();Image.new('RGB',(320,240)).save(out,'JPEG');return out.getvalue()
def item(i='1',typ='Movie'):
    return dict(Id=i,Name='Media',Type=typ,Path='/media/'+i,ProviderIds={'Tmdb':i})
class Fake:
    base='http://emby/emby'
    def __init__(self,data=None):
        self.data=data;self.rows=[item()];self.uploads=[];self.present=data is not None;self.candidate_calls=0;self.info_error=None
    def batch(self,types,parent,offset,limit):return self.rows[offset:offset+limit],len(self.rows)
    def image_info(self,i):
        if self.info_error:raise self.info_error
        return [{'ImageType':'Primary'}] if self.present else []
    def image(self,i):
        if isinstance(self.data,Exception):raise self.data
        return self.data
    def candidates(self,i):
        self.candidate_calls+=1
        return [{'Url':'https://image.tmdb.org/t/p/original/a.jpg','Language':'zh'}]
    def upload(self,i,data):self.uploads.append(data);self.data=data;self.present=True
class Tests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.http=Mock();self.http.get.return_value=photo()
    def tearDown(self):self.tmp.cleanup()
    def run_engine(self,e,**kw):return c.MediaEngine(e,self.tmp.name,http=self.http).run(pause=0,**kw)
    def test_valid_manual_preserved(self):
        e=Fake(photo());self.assertEqual(self.run_engine(e)['counts'],{'healthy':1});self.assertFalse(e.uploads);self.assertEqual(e.candidate_calls,0)
    def test_html_is_repaired_and_backed_up(self):
        e=Fake(b'<html>404</html>');self.assertEqual(self.run_engine(e)['counts'],{'synced':1});self.assertTrue(list(pathlib.Path(self.tmp.name).rglob('*.bin')))
    def test_absent_record_is_missing_without_image_get(self):
        e=Fake(c.HttpError(500));e.present=False;self.assertEqual(self.run_engine(e)['counts'],{'synced':1})
    def test_500_with_record_never_overwritten(self):
        e=Fake(c.HttpError(500));self.assertEqual(self.run_engine(e)['counts'],{'error':1});self.assertFalse(e.uploads)
    def test_metadata_error_is_not_missing(self):
        e=Fake();e.info_error=c.HttpError(401);self.assertEqual(self.run_engine(e)['counts'],{'error':1});self.assertFalse(e.uploads)
    def test_locked_skipped(self):
        e=Fake();e.rows[0]['IsLocked']=True;self.assertEqual(self.run_engine(e)['counts'],{'locked':1});self.assertFalse(e.uploads)
    def test_collections_excluded(self):
        e=Fake();e.rows=[item(typ='BoxSet')];self.assertEqual(self.run_engine(e)['counts'],{'skipped':1});self.assertFalse(e.uploads)
    def test_only_check_never_downloads_or_uploads(self):
        e=Fake();self.assertEqual(self.run_engine(e,dry_run=True)['counts'],{'would_fill':1});self.http.get.assert_not_called();self.assertFalse(e.uploads)
    def test_missing_source_skipped(self):
        e=Fake();e.candidates=lambda i:[];self.assertEqual(self.run_engine(e)['counts'],{'no_source':1});self.assertFalse(e.uploads)
    def test_bad_download_preserves_original(self):
        self.http.get.return_value=b'bad';e=Fake(b'old');self.assertEqual(self.run_engine(e)['counts'],{'error':1});self.assertFalse(e.uploads)
    def test_manual_upload_during_download_wins(self):
        e=Fake()
        def download(*a,**kw):e.data=photo();e.present=True;return photo()
        self.http.get.side_effect=download;self.assertEqual(self.run_engine(e)['counts'],{'healthy':1});self.assertFalse(e.uploads)
    def test_stop_during_download_prevents_write(self):
        e=Fake();stop=threading.Event()
        def download(*a,**kw):stop.set();return photo()
        self.http.get.side_effect=download
        r=c.MediaEngine(e,self.tmp.name,stop,self.http).run(pause=0);self.assertFalse(e.uploads);self.assertEqual(r['next_offset'],0)
    def test_batch_resume_and_wrap(self):
        e=Fake(photo());e.rows=[item(str(i)) for i in range(5)]
        self.assertEqual(self.run_engine(e,batch_size=2)['next_offset'],2)
        self.assertEqual(self.run_engine(e,batch_size=2)['next_offset'],4)
        r=self.run_engine(e,batch_size=2);self.assertEqual(r['next_offset'],0);self.assertEqual(len(r['items']),1)
    def test_identity_change_does_not_reuse_backup(self):
        e=Fake(photo());self.run_engine(e,backup_healthy=True);e.data=None;e.present=False;e.rows[0]['ProviderIds']={'Tmdb':'999'}
        self.assertEqual(self.run_engine(e)['counts'],{'synced':1})
    def test_backup_restores_original(self):
        e=Fake(photo());self.run_engine(e,backup_healthy=True);e.data=None;e.present=False
        self.assertEqual(self.run_engine(e)['counts'],{'restored':1});self.assertEqual(e.candidate_calls,0)
    def test_candidates_only_tmdb(self):
        e=c.MediaEmby('http://emby','secret');e.call=Mock(return_value=json.dumps({'Images':[{'ProviderName':'Other','Type':'Primary'},{'ProviderName':'TheMovieDb','Type':'Backdrop'},{'ProviderName':'TheMovieDb','Type':'Primary'}]}))
        self.assertEqual(len(e.candidates('1')),1)
    def test_plugin_scope_and_independent_service(self):
        p=plugin.MediaImageRepair();p.init_plugin({'enabled':True,'emby_url':'http://emby','emby_key':'test'})
        self.assertEqual(p.get_service()[0]['id'],'MediaImageRepair.check');p.stop_service();self.assertFalse(p.get_service())
    def test_invalid_type_disables_plugin(self):
        p=plugin.MediaImageRepair();p.init_plugin({'enabled':True,'emby_url':'http://emby','emby_key':'test','types':['BoxSet']});self.assertFalse(p.get_state())

if __name__=='__main__':unittest.main()
