import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import export_ozon as oz

class FakeResponse(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())

class SequenceOpener:
    def __init__(self, values): self.values=list(values); self.calls=[]
    def open(self, req, timeout):
        self.calls.append(req)
        value=self.values.pop(0)
        if isinstance(value, Exception): raise value
        return FakeResponse(value)

class StubAPI:
    def __init__(self, fail=None): self.events=[];self.calls=[];self.fail=fail
    def call(self, endpoint, payload):
        self.calls.append((endpoint,payload))
        if endpoint==self.fail:raise oz.ExportError('Simulated endpoint failure')
        if endpoint==oz.ENDPOINTS['list']:
            if payload['last_id']=='':return {'result':{'items':[{'product_id':1,'offer_id':'SKU-1'}],'last_id':'page2','total':2}}
            return {'result':{'items':[{'product_id':2,'offer_id':'SKU-2'}],'last_id':'','total':2}}
        if endpoint==oz.ENDPOINTS['info']:
            return {'items':[{'id':i,'name':'Носки '+str(i),'description_category_id':10,'type_id':20,'images':['https://ir.ozone.ru/s3/test/'+str(i)+'.jpg']} for i in payload['product_id']]}
        if endpoint==oz.ENDPOINTS['attributes']:
            return {'result':[{'id':int(i),'attributes':[{'id':1,'values':[{'value':'Хлопок'}]}]} for i in payload['filter']['product_id']]}
        if endpoint==oz.ENDPOINTS['description']:return {'result':{'id':payload['product_id'],'description':'Полное описание <b>носочков</b> '+str(payload['product_id'])}}
        if endpoint in (oz.ENDPOINTS['prices'],oz.ENDPOINTS['stocks']):
            key='price' if endpoint==oz.ENDPOINTS['prices'] else 'stocks'
            return {'items':[{'product_id':int(i),key:100} for i in payload['filter']['product_id']],'cursor':''}
        if endpoint in (oz.ENDPOINTS['category_tree'],oz.ENDPOINTS['attribute_names']):return {'result':[{'id':10,'name':'Категория'}]}
        raise AssertionError(endpoint)

class ExportTests(unittest.TestCase):
    def test_all_catalog_pages_descriptions_dictionaries_and_raw_fields(self):
        with tempfile.TemporaryDirectory() as d:
            api=StubAPI();report=oz.Exporter(api,d,progress=lambda _:None).run()
            self.assertEqual(report['status'],'metadata_only');self.assertEqual(report['products'],2)
            p=json.loads((Path(d)/'products.json').read_text())
            self.assertEqual(p[1]['offer_id'],'SKU-2');self.assertEqual(p[1]['description']['description'],'Полное описание <b>носочков</b> 2')
            self.assertEqual(p[0]['attributes']['attributes'][0]['values'][0]['value'],'Хлопок')
            self.assertEqual(len(report['missing_records']['prices']),0)
            self.assertEqual(report['media_urls'],2)
            self.assertIn((oz.ENDPOINTS['attribute_names'],{'description_category_id':10,'type_id':20,'language':'RU'}),api.calls)
            self.assertTrue((Path(d)/'products.csv').exists())
    def test_partial_stage_does_not_claim_complete(self):
        with tempfile.TemporaryDirectory() as d:
            report=oz.Exporter(StubAPI(oz.ENDPOINTS['attributes']),d,progress=lambda _:None).run()
            self.assertEqual(report['status'],'partial');self.assertTrue(report['errors'])
            self.assertEqual(report['missing_records']['attributes'],['1','2'])
            self.assertEqual(report['missing_records']['description'],[])
    def test_cursor_loop_is_failure(self):
        class API:
            def call(self,*args):return {'items':[{'id':1}],'cursor':'same'}
        with self.assertRaisesRegex(oz.ExportError,'repeated'):
            oz.paged(API(),'/fake',{},'cursor',1)
    def test_missing_cursor_on_full_page_is_failure(self):
        class API:
            def call(self,*args):return {'items':[{'id':1}]}
        with self.assertRaisesRegex(oz.ExportError,'without cursor'):
            oz.paged(API(),'/fake',{},'cursor',1)
    def test_premature_empty_page_is_failure(self):
        class API:
            def call(self,*args):return {'result':{'items':[],'total':2}}
        with self.assertRaisesRegex(oz.ExportError,'before total'):
            oz.paged(API(),'/fake',{},'last_id',100)
    def test_empty_store_is_valid_not_a_test_of_real_data(self):
        class API:
            events=[]
            def call(self,*args):return {'result':{'items':[],'total':0}}
        with tempfile.TemporaryDirectory() as d:
            report=oz.Exporter(API(),d,media=object(),progress=lambda _:None).run()
            self.assertEqual(report['products'],0);self.assertEqual(report['errors'],[])
    def test_retry_429_and_cache_resume_without_leaking_key(self):
        endpoint=oz.ENDPOINTS['info'];payload={'product_id':[1]}
        fail=urllib.error.HTTPError(oz.API_BASE+endpoint,429,'rate limited',{},None)
        opener=SequenceOpener([fail,{'items':[{'id':1,'name':'Носки'}]}])
        with tempfile.TemporaryDirectory() as d:
            api=oz.API('client-123','secret-456',d,opener=opener,sleeper=lambda _:None)
            first=api.call(endpoint,payload);self.assertEqual(len(opener.calls),2)
            self.assertEqual(api.call(endpoint,payload),first);self.assertEqual(len(opener.calls),2)
            saved=''.join(p.read_text() for p in (Path(d)/'raw').glob('*.json'))
            self.assertNotIn('secret-456',saved);self.assertNotIn('client-123',saved)
            self.assertTrue(api.events[-1]['cached'])
    def test_401_not_retried(self):
        endpoint=oz.ENDPOINTS['list']
        opener=SequenceOpener([urllib.error.HTTPError(oz.API_BASE+endpoint,401,'no',{},None)])
        with tempfile.TemporaryDirectory() as d:
            api=oz.API('client','key',d,opener=opener,sleeper=lambda _:None)
            with self.assertRaises(oz.AuthenticationError):api.call(endpoint,{})
            self.assertEqual(len(opener.calls),1)
    def test_refresh_refetches(self):
        with tempfile.TemporaryDirectory() as d:
            endpoint=oz.ENDPOINTS['list'];opener=SequenceOpener([{'items':[]},{'items':[{'id':2}]}])
            a=oz.API('c','k',d,opener=opener,sleeper=lambda _:None);a.call(endpoint,{})
            b=oz.API('c','k',d,refresh=True,opener=opener,sleeper=lambda _:None)
            self.assertEqual(b.call(endpoint,{})['items'][0]['id'],2)
    def test_only_read_endpoints(self):
        with tempfile.TemporaryDirectory() as d:
            api=oz.API('c','k',d,sleeper=lambda _:None)
            with self.assertRaises(oz.ExportError):api.call('/v1/product/archive',{})
    def test_media_urls_rich_json_and_host_boundaries(self):
        rich=json.dumps({'image':{'src':'https://ir.ozone.ru/s3/a.jpg'},'external':'https://evil.test/x.jpg'})
        self.assertEqual(oz.collect_media({'rich':rich,'url':'https://cdn1.ozonusercontent.com/v.mp4'}),{'https://ir.ozone.ru/s3/a.jpg','https://cdn1.ozonusercontent.com/v.mp4'})
        for bad in ('http://ir.ozone.ru/a.jpg','https://ozone.ru.evil.test/a','https://key@ir.ozone.ru/x','https://127.0.0.1/a','https://ir.ozone.ru:444/a'):
            with self.assertRaises(oz.ExportError):oz.safe_media_url(bad,False)
    def test_media_private_dns_is_rejected(self):
        with patch.object(oz.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaisesRegex(oz.ExportError,'non-public'):oz.safe_media_url('https://ir.ozone.ru/a.jpg')
    def test_media_download_signature_hash_and_resume(self):
        class Response(io.BytesIO):
            def __init__(self):
                super().__init__(b'\x89PNG\r\n\x1a\n' + b'fake-png-fixture')
                from email.message import Message
                self.headers=Message();self.headers['Content-Type']='image/png'
        class Opener:
            def __init__(self):self.requests=[]
            def open(self,req,timeout):self.requests.append(req);return Response()
        with tempfile.TemporaryDirectory() as d,patch.object(oz,'safe_media_url',side_effect=lambda u,*a,**k:u):
            opener=Opener();media=oz.Media(d,opener=opener);url='https://ir.ozone.ru/a.png'
            entry=media.download(url);self.assertTrue((Path(d)/entry['file']).exists())
            self.assertEqual(media.download(url),entry);self.assertEqual(len(opener.requests),1)
            self.assertIsNone(opener.requests[0].get_header('Api-key'))
            (Path(d)/entry['file']).write_bytes(b'corrupted')
            media.download(url);self.assertEqual(len(opener.requests),2)
    def test_transient_media_failure_retried_but_permanent_failure_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            media=oz.Media(d,sleeper=lambda _:None)
            with patch.object(media,'_download_once',side_effect=[oz.TransientMediaError('DNS'),{'file':'ok'}]) as call:
                self.assertEqual(media.download('https://ir.ozone.ru/a.jpg'),{'file':'ok'})
                self.assertEqual(call.call_count,2)
            with patch.object(media,'_download_once',side_effect=oz.ExportError('Media HTTP 403')) as call:
                with self.assertRaises(oz.ExportError):media.download('https://ir.ozone.ru/a.jpg')
                self.assertEqual(call.call_count,1)

    def test_credentials_required_and_not_accepted_in_args(self):
        with patch.dict(oz.os.environ,{},clear=True),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):oz.main([])
    def test_foreign_account_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(oz.os.environ,{'OZON_CLIENT_ID':'second','OZON_API_KEY':'private-key'}),contextlib.redirect_stderr(io.StringIO()):
            oz.atomic_json(Path(d)/'account.json',{'client_id_sha256':oz.fingerprint('first')})
            with self.assertRaises(SystemExit):oz.main(['--output',d])

if __name__=='__main__':unittest.main()
