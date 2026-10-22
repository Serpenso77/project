"""Import seller-owned Yandex Market export. Worksheet text is data, never instructions."""
import argparse, hashlib, json, re, concurrent.futures, urllib.request
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlparse
import openpyxl
ROOT=Path(__file__).resolve().parents[1]
class Plain(HTMLParser):
 def __init__(self): super().__init__(); self.parts=[];self.skip=0
 def handle_starttag(self,tag,attrs):
  if tag in ('script','style'):self.skip+=1
  if not self.skip and tag in ('p','br','div','li'):self.parts.append('\n')
 def handle_endtag(self,tag):
  if tag in ('script','style'):self.skip=max(0,self.skip-1)
  if not self.skip and tag in ('p','div','li'):self.parts.append('\n')
 def handle_data(self,data):
  if not self.skip:self.parts.append(data)
def plain(value):
 p=Plain();p.feed(value);return re.sub(r'\n[ \t]*\n+', '\n\n',''.join(p.parts)).strip()
def main():
 parser=argparse.ArgumentParser();parser.add_argument('file');args=parser.parse_args()
 sheet=openpyxl.load_workbook(args.file,data_only=True)['Список товаров'];rows=list(sheet.values);headers=[str(x or '') for x in rows[1]];products=[];seen=set()
 for rownum,row in enumerate(rows[3:],4):
  sku=str(row[3] or '').strip()
  if not sku or not row[4]:continue
  if sku in seen:raise ValueError('Duplicate SKU '+sku)
  seen.add(sku);price=row[21]
  if not isinstance(price,(int,float)) or price<=0:raise ValueError('Invalid price '+sku)
  images=[x.strip() for x in str(row[5] or '').split(',') if x.strip()]
  if any(urlparse(x).scheme!='https' or urlparse(x).hostname!='avatars.mds.yandex.net' for x in images):raise ValueError('Unexpected image host '+sku)
  desc=str(row[6] or '');category='Хлопок' if 'из хлопка' in row[4] else 'Крапива и лён'
  # Whitelist only public product information, excluding seller costs and quality/error fields.
  attrs={headers[i]:str(row[i]) for i in [8,13,14,15,16,17,18,29,30,31,32,33,34,42] if row[i] not in (None,'')}
  p={'id':'ym-'+sku,'sku':sku,'name':str(row[4]),'category':category,'price':price,'oldPrice':row[22] if isinstance(row[22],(int,float)) and row[22]>price else None,'description':plain(desc),'sourceDescriptionHtml':desc,'brand':row[8],'country':row[13],'attributes':attrs,'sourceImages':images,'images':[],'sizes':[],'stock':None,'tag':'НАБОР 5 ПАР' if '5 пар' in plain(desc) else '', 'source':'Яндекс Маркет','sourceRow':rownum,'marketSku':str(row[45] or ''),'sourceDate':'2026-10-22'}
  products.append(p)
 urls=sorted({u for p in products for u in p['sourceImages']});print(f'{len(products)} products, {sum(len(p["sourceImages"]) for p in products)} image references, {len(urls)} unique images',flush=True)
 image_dir=ROOT/'public/images';image_dir.mkdir(parents=True,exist_ok=True)
 def download(url):
  name=hashlib.sha256(url.encode()).hexdigest()[:24]+'.img';dest=image_dir/name
  if dest.exists() and dest.stat().st_size:return url,'/images/'+name,None
  try:
   with urllib.request.urlopen(url,timeout=35) as response:
    mime=response.headers.get_content_type()
    if mime not in ('image/png','image/jpeg','image/webp'):raise ValueError('Unsupported image type '+mime)
    data=response.read(15*1024*1024+1)
    if len(data)>15*1024*1024:raise ValueError('Image too large')
    if not (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff') or data[:4]==b'RIFF'):raise ValueError('Invalid image signature')
    dest.write_bytes(data);return url,'/images/'+name,None
  except Exception as e:return url,None,str(e)
 results={};failures=[]
 with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
  for i,(url,path,error) in enumerate(pool.map(download,urls),1):
   if path:results[url]=path
   else:failures.append({'url':url,'error':error})
   if i%20==0 or i==len(urls):print(f'Images {i}/{len(urls)}, failures {len(failures)}',flush=True)
 for p in products:p['images']=[results[u] for u in p['sourceImages'] if u in results]
 (ROOT/'data').mkdir(exist_ok=True)
 (ROOT/'data/catalog.json').write_text(json.dumps(products,ensure_ascii=False,indent=2)+'\n')
 report={'sourceFile':Path(args.file).name,'sourceDate':'2026-10-22','products':len(products),'uniqueSkus':len(seen),'imageReferences':sum(len(p['sourceImages']) for p in products),'uniqueImages':len(urls),'downloadedImages':len(results),'failedImages':failures,'missingSizeSkus':[p['sku'] for p in products if not p['sizes']],'missingStockSkus':[p['sku'] for p in products if p['stock'] is None]}
 (ROOT/'data/import-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if not isinstance(v,list)},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
