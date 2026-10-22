import hashlib,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from import_ozon_catalog import normalize,save,load

class ImportTests(unittest.TestCase):
 def fixture(self,root):
  export=root/'export';public=root/'public';export.mkdir();public.mkdir()
  media=export/'media';media.mkdir();image=media/'one.jpg';image.write_bytes(b'\xff\xd8\xfffixture')
  url='https://ir.ozone.ru/image.jpg'
  save(export/'media-files.json',{url:{'file':'media/one.jpg','content_type':'image/jpeg','sha256':hashlib.sha256(image.read_bytes()).hexdigest()}})
  save(export/'manifest.json',{'status':'complete','errors':[],'products':2,'finished_at':'2026-10-22T22:10:00+00:00'})
  save(export/'dictionaries.json',{'attribute_names':{'1:2':{'result':[{'id':4604,'name':'Состав материала'},{'id':4295,'name':'Российский размер'}]}}})
  records=[]
  for pid,sku in [(1,'same'),(2,'new')]:
   records.append({'product_id':str(pid),'offer_id':sku,'info':{'name':'Носки '+sku,'primary_image':[url],'images':[],'currency_code':'RUB'},'attributes':{'attributes':[{'id':4604,'values':[{'value':'80% хлопок'}]},{'id':4295,'values':[{'value':'38'},{'value':'37'}]}]},'description':{'description':'<p>Полное описание</p><script>bad()</script>'},'prices':{'price':{'price':1000,'old_price':1200,'currency_code':'RUB','net_price':99},'commissions':[{'secret':1}]},'stocks':{'stocks':[{'type':'fbo','present':3,'reserved':1}]},'media_urls':[url]})
  save(export/'products.json',records)
  base=[{'id':'ym-same','sku':'same','name':'Старое название','price':1150,'oldPrice':1400,'description':'Исходное описание Яндекс','sourceDescriptionHtml':'<p>Исходное описание Яндекс</p>','images':['/images/base.png'],'sourceImages':[],'sizes':[],'attributes':{},'source':'Яндекс Маркет','sourceDate':'2026-10-22'}]
  return export,public,base
 def test_exact_sku_merge_preserves_base_price_and_both_descriptions(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);export,public,base=self.fixture(root);dest=root/'catalog.json';report=normalize(export,dest,base,public)
   p=load(dest);self.assertEqual(len(p),2);self.assertEqual(p[0]['id'],'ym-same');self.assertEqual(p[0]['price'],1150)
   self.assertEqual(p[0]['sourcePrices'][0]['price'],1000);self.assertEqual(p[0]['sourceDescriptions'][0]['text'],'Исходное описание Яндекс')
   self.assertEqual(p[0]['sourceDescriptions'][1]['text'],'Полное описание');self.assertNotIn('bad()',p[0]['sourceDescriptions'][1]['text'])
   self.assertEqual(p[0]['sizes'],['37 / 38']);self.assertEqual(p[0]['marketplaceStock']['stocks'][0]['present'],3)
   self.assertEqual(report['sourceDate'],'2026-10-23');self.assertEqual(report['mergedByExactSellerSku'],['same'])
   self.assertEqual(report['addedSkus'],['new']);self.assertEqual(report['missingOzonSizes'],[])
   self.assertNotIn('net_price',dest.read_text());self.assertNotIn('commissions',dest.read_text())
 def test_reimport_is_repeatable_and_keeps_ids_stable(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);export,public,base=self.fixture(root);dest=root/'catalog.json'
   one=normalize(export,dest,base,public);before=dest.read_bytes();files=sorted(p.name for p in (public/'images').iterdir())
   two=normalize(export,dest,base,public);self.assertEqual(before,dest.read_bytes());self.assertEqual(one,two);self.assertEqual(files,sorted(p.name for p in (public/'images').iterdir()))
 def test_partial_export_does_not_overwrite_catalog(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);export,public,base=self.fixture(root);dest=root/'catalog.json';dest.write_text('keep')
   save(export/'manifest.json',{'status':'partial','errors':[{'error':'missing'}]})
   with self.assertRaisesRegex(ValueError,'complete'):normalize(export,dest,base,public)
   self.assertEqual(dest.read_text(),'keep')
 def test_corrupt_media_rejected_before_catalog_write(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);export,public,base=self.fixture(root);(export/'media/one.jpg').write_bytes(b'changed');dest=root/'catalog.json'
   with self.assertRaisesRegex(ValueError,'checksum'):normalize(export,dest,base,public)
   self.assertFalse(dest.exists())
 def test_unknown_size_not_inferred_from_sku(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);export,public,base=self.fixture(root);p=load(export/'products.json')
   for x in p:x['attributes']['attributes']=[a for a in x['attributes']['attributes'] if a['id']!=4295]
   save(export/'products.json',p);dest=root/'catalog.json';report=normalize(export,dest,base,public)
   self.assertEqual(report['missingOzonSizes'],['same','new']);self.assertEqual(load(dest)[1]['sizes'],[])

if __name__=='__main__':unittest.main()
