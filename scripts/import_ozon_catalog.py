#!/usr/bin/env python3
"""Publish only explicitly selected catalog fields from a completed private export."""
import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]

class Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.hidden+=1
        if not self.hidden and tag in ('p','br','div','li'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script','style'): self.hidden=max(0,self.hidden-1)
        if not self.hidden and tag in ('p','div','li'): self.parts.append('\n')
    def handle_data(self, value):
        if not self.hidden: self.parts.append(value)

def plain(value):
    p=Text();p.feed(value);return re.sub(r'\n[ \t]*\n+', '\n\n',''.join(p.parts)).strip()

def load(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def save(path, value):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    temp=Path(str(path)+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');temp.replace(path)

def normalize(export_root, destination, base, public_root, source_date=None):
    export_root,public_root=Path(export_root).resolve(),Path(public_root).resolve()
    manifest=load(export_root/'manifest.json')
    if manifest.get('status')!='complete' or manifest.get('errors'):
        raise ValueError('Require a complete Ozon export; inspect manifest.json')
    source=load(export_root/'products.json');dictionaries=load(export_root/'dictionaries.json');media=load(export_root/'media-files.json')
    if len(source)!=manifest['products']:raise ValueError('Export count mismatch')
    attribute_names={}
    for value in dictionaries.get('attribute_names',{}).values():
        entries=value.get('result',value)
        if isinstance(entries,list):
            for entry in entries: attribute_names[str(entry['id'])]=entry.get('name',str(entry['id']))
    products=json.loads(json.dumps(base));by_sku={p['sku']:p for p in products}
    if len(by_sku)!=len(products):raise ValueError('Base catalog has duplicate SKUs; resolve explicitly')
    image_dir=public_root/'images';video_dir=public_root/'videos';merged=[];added=[];source_ids=set();seen_skus=set()
    imported_date=source_date or datetime.fromisoformat(manifest['finished_at']).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
    datetime.strptime(imported_date,'%Y-%m-%d')
    def publish(url):
        entry=media.get(url)
        if not entry:return None
        src=(export_root/entry['file']).resolve()
        if export_root not in src.parents or not src.is_file():raise ValueError('Invalid media file location')
        digest=hashlib.sha256(src.read_bytes()).hexdigest()
        if digest!=entry['sha256']:raise ValueError('Media checksum mismatch')
        mime=entry['content_type']
        if mime not in ('image/jpeg','image/png','image/webp','image/gif','image/avif','video/mp4','video/webm','video/quicktime'):return None
        folder=video_dir if mime.startswith('video/') else image_dir
        folder.mkdir(parents=True,exist_ok=True)
        dest=folder/(digest+src.suffix)
        if not dest.exists():shutil.copyfile(src,dest)
        elif hashlib.sha256(dest.read_bytes()).hexdigest()!=digest:raise ValueError('Published media checksum mismatch')
        return {'src':'/'+str(dest.relative_to(public_root)),'type':mime}
    published={url:publish(url) for url in media}
    for raw in source:
        info=raw.get('info');attrs=raw.get('attributes');desc=raw.get('description');price=raw.get('prices')
        if not all(isinstance(v,dict) for v in (info,attrs,desc,price)):raise ValueError('Required product data missing')
        pid=str(raw['product_id']);sku=str(raw['offer_id'])
        if pid in source_ids or sku in seen_skus:raise ValueError('Duplicate Ozon product/SKU')
        source_ids.add(pid);seen_skus.add(sku)
        values={str(a['id']):[str(v['value']) for v in a.get('values',[]) if v.get('value') is not None] for a in attrs.get('attributes',[])}
        av=lambda key:', '.join(values.get(str(key),[]))
        socks='носк' in info.get('name','').lower()
        composition=av(4604)
        category=('Хлопок' if 'хлоп' in (info.get('name','')+' '+composition).lower() and 'крапив' not in composition.lower() else 'Крапива и лён') if socks else 'Рукоделие'
        public_attrs={attribute_names.get(key,'Характеристика '+key):', '.join(value) for key,value in values.items() if key not in ('4191','23171','4180','9024') and value}
        # Ozon's product size array describes one seller SKU; it is not a stock breakdown by shoe size.
        shoe_sizes=values.get('4295',[]) if socks else []
        sizes=[' / '.join(sorted(shoe_sizes,key=lambda s:(float(s.replace(',','.')) if re.fullmatch(r'\d+(?:[.,]\d+)?',s) else float('inf'),s)))] if shoe_sizes else []
        images=info.get('primary_image',[])+info.get('images',[])+info.get('images360',[])
        if not isinstance(images,list):raise ValueError('Unexpected Ozon image schema')
        urls=list(dict.fromkeys(images+[u for u in raw.get('media_urls',[]) if published.get(u,{}).get('type','').startswith('image/')]))
        local_images=[published[u]['src'] for u in urls if published.get(u)]
        videos=[published[u] for u in raw.get('media_urls',[]) if published.get(u,{}).get('type','').startswith('video/')]
        price_fields=price.get('price',{})
        currency=price_fields.get('currency_code',info.get('currency_code'))
        if currency!='RUB':raise ValueError('Unsupported product currency')
        amount=Decimal(str(price_fields.get('price',info.get('price','0'))))
        if amount<=0:raise ValueError('Missing positive seller price')
        old=Decimal(str(price_fields.get('old_price') or info.get('old_price') or '0'))
        source_description={'source':'Ozon','date':imported_date,'text':plain(desc.get('description','')),'sourceHtml':desc.get('description','')}
        source_price={'source':'Ozon','date':imported_date,'price':float(amount),'oldPrice':float(old) if old>amount else None,'currency':'RUB'}
        source_ref={'source':'Ozon','sku':sku,'productId':pid,'date':imported_date}
        if sku in by_sku:
            p=by_sku[sku]
            p.setdefault('sourceDescriptions',[{'source':'Яндекс Маркет','date':p['sourceDate'],'text':p['description'],'sourceHtml':p.get('sourceDescriptionHtml','')}])
            p['sourceDescriptions']=[d for d in p['sourceDescriptions'] if d['source']!='Ozon']+[source_description]
            p['images']=list(dict.fromkeys(p['images']+local_images));p['sourceImages']=list(dict.fromkeys(p.get('sourceImages',[])+urls))
            p.setdefault('sources',[{'source':'Яндекс Маркет','sku':sku,'date':p['sourceDate']}])
            p['sources']=[s for s in p['sources'] if s['source']!='Ozon']+[source_ref]
            p['sourcePrices']=[s for s in p.get('sourcePrices',[]) if s['source']!='Ozon']+[source_price]
            p['attributes'].update(public_attrs);p['sizes']=sizes or p.get('sizes',[]);merged.append(sku)
        else:
            p={'id':'ozon-'+pid,'sku':sku,'name':info['name'],'category':category,'price':float(amount),'oldPrice':float(old) if old>amount else None,'description':source_description['text'],'sourceDescriptionHtml':desc.get('description',''),'brand':av(31),'country':av(4389),'attributes':public_attrs,'sourceImages':urls,'images':local_images,'sizes':sizes,'stock':None,'tag':av(9662).upper() if av(9662) else '', 'source':'Ozon','sourceDate':imported_date,'sourceDescriptions':[source_description],'sources':[source_ref],'sourcePrices':[source_price]}
            products.append(p);by_sku[sku]=p;added.append(sku)
        p['videos']=videos;p['composition']=composition;p['manufacturerSize']=av(9533);p['requiresSize']=socks
        p['marketplaceStock']={'source':'Ozon','date':imported_date,'stocks':(raw.get('stocks') or {}).get('stocks',[]),'note':'Остатки на Ozon, не наличие для прямой продажи на сайте'}
        p['ozonProductId']=pid
        p['sizeNote']=('Вариант размера из карточки Ozon; размер производителя: '+av(9533)) if sizes else ('Размер уточняется по артикулу' if socks else 'Размер для этого товара не выбирается')
    report={'source':'Ozon Seller API','sourceDate':imported_date,'exportedProducts':len(source),'baseProducts':len(base),'mergedByExactSellerSku':merged,'addedSkus':added,'catalogProducts':len(products),'photosPublished':sum(bool(p and p['type'].startswith('image/')) for p in published.values()),'videosPublished':sum(bool(p and p['type'].startswith('video/')) for p in published.values()),'missingOzonSizes':[p['sku'] for p in products if p.get('ozonProductId') and p.get('requiresSize') and not p['sizes']],'note':'Only exact seller SKU matches merged; marketplace prices/stock preserved separately. Original direct-shop prices retained for existing products.'}
    save(destination,products)
    return report

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--export',type=Path,default=ROOT/'.data/ozon-export-live');parser.add_argument('--source-date',help='ISO date for the user-facing snapshot label');args=parser.parse_args()
    base_path=ROOT/'data/yandex-catalog.json'
    if not base_path.exists():
        base=load(ROOT/'data/catalog.json')
        if any(p.get('ozonProductId') for p in base):raise ValueError('Cannot infer original Yandex base; restore explicit base snapshot')
        save(base_path,base)
    base=load(base_path)
    report=normalize(args.export,ROOT/'data/catalog.json',base,ROOT/'public',args.source_date)
    save(ROOT/'data/ozon-import-report.json',report);print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
