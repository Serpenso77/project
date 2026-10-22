#!/usr/bin/env python3
"""Read-only Ozon Seller API catalog export. Python standard library only."""
from __future__ import annotations
import argparse
import csv
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = 'https://api-seller.ozon.ru'
ENDPOINTS = {
    'list': '/v3/product/list',
    'info': '/v3/product/info/list',
    'attributes': '/v4/product/info/attributes',
    'description': '/v1/product/info/description',
    'prices': '/v5/product/info/prices',
    'stocks': '/v4/product/info/stocks',
    'category_tree': '/v1/description-category/tree',
    'attribute_names': '/v1/description-category/attribute',
}
MEDIA_HOSTS = ('ozon.ru', 'ozone.ru', 'ozonusercontent.com')
RETRY_CODES = {429, 500, 502, 503, 504}

class ExportError(Exception):
    pass

class AuthenticationError(ExportError):
    pass

class TransientMediaError(ExportError):
    pass

def now():
    return datetime.now(timezone.utc).isoformat()

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)

def fingerprint(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def unwrap(response):
    result = response.get('result', response)
    return result

def records(response):
    value = unwrap(response)
    if isinstance(value, list) and all(isinstance(x, dict) for x in value):
        return value
    if isinstance(value, dict) and isinstance(value.get('items'), list) and all(isinstance(x, dict) for x in value['items']):
        return value['items']
    raise ExportError('Unexpected response schema: expected result array or items array')

def product_id(item):
    value = item.get('product_id', item.get('id'))
    if value is None:
        raise ExportError('Product record has no product_id/id')
    return str(value)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Seller API credentials to another URL/host.
        return None

class API:
    def __init__(self, client_id, api_key, root, refresh=False, attempts=4, delay=.25,
                 opener=None, sleeper=time.sleep):
        self.client_id = client_id
        self.api_key = api_key
        self.cache = Path(root) / 'raw'
        self.refresh = refresh
        self.attempts = attempts
        self.delay = delay
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sleep = sleeper
        self.events = []

    def call(self, endpoint, payload):
        if endpoint not in ENDPOINTS.values():
            raise ExportError('Only allowlisted read-only API methods may be called')
        request_key = fingerprint(json.dumps([endpoint, payload], sort_keys=True))
        path = self.cache / (endpoint.strip('/').replace('/', '_') + '-' + request_key[:24] + '.json')
        if path.exists() and not self.refresh:
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(saved, dict) or not isinstance(saved.get('response'), dict):
                    raise ValueError('Invalid cached response')
            except (ValueError, OSError):
                raise ExportError('Invalid cache file; rerun with --refresh') from None
            if saved.get('endpoint') != endpoint or saved.get('request') != payload:
                raise ExportError('Cache request does not match; use --refresh')
            self.events.append({'endpoint': endpoint, 'file': str(path.relative_to(self.cache.parent)), 'cached': True, 'retrieved_at': saved.get('retrieved_at')})
            return saved['response']
        data = json.dumps(payload).encode('utf-8')
        for attempt in range(self.attempts):
            self.sleep(self.delay if attempt == 0 else min(2 ** attempt, 8))
            req = urllib.request.Request(API_BASE + endpoint, data=data, method='POST', headers={
                'Client-Id': self.client_id, 'Api-Key': self.api_key,
                'Content-Type': 'application/json', 'Accept': 'application/json',
                'User-Agent': 'LemashkiCatalogExporter/1.0',
            })
            try:
                with self.opener.open(req, timeout=45) as response:
                    raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise ExportError('API response exceeds 32 MiB')
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ExportError('API returned a non-object response')
                if value.get('error') or (value.get('code') and value.get('message')):
                    raise ExportError('API returned an error object; check endpoint version and permissions')
                retrieved = now()
                atomic_json(path, {'endpoint': endpoint, 'request': payload, 'retrieved_at': retrieved, 'response': value})
                self.events.append({'endpoint': endpoint, 'file': str(path.relative_to(self.cache.parent)), 'cached': False, 'retrieved_at': retrieved})
                return value
            except urllib.error.HTTPError as exc:
                # Do not expose response bodies, request headers, keys or arbitrary server text.
                if exc.code in (401, 403):
                    raise AuthenticationError(f'{endpoint}: HTTP {exc.code}; check Seller API key permissions and client ID') from None
                if exc.code not in RETRY_CODES or attempt == self.attempts - 1:
                    raise ExportError(f'{endpoint}: HTTP {exc.code}; check API method version and request compatibility') from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == self.attempts - 1:
                    raise ExportError(f'{endpoint}: network request failed after {self.attempts} attempts') from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ExportError(f'{endpoint}: invalid JSON response') from None
        raise ExportError('Request did not complete')

def paged(api, endpoint, payload, cursor_field, limit, max_pages=100000):
    """Follow server cursors until exhausted; reject loops and unproven truncation."""
    output = []
    cursor = ''
    seen = set()
    expected_total = None
    for _ in range(max_pages):
        request = {**payload, cursor_field: cursor, 'limit': limit}
        response = api.call(endpoint, request)
        batch = records(response)
        root = unwrap(response)
        meta = root if isinstance(root, dict) else response
        total = meta.get('total')
        if isinstance(total, int):
            expected_total = total
        output.extend(batch)
        next_cursor = meta.get(cursor_field, response.get(cursor_field, ''))
        if not batch:
            if expected_total is not None and len(output) < expected_total:
                raise ExportError(f'{endpoint}: pagination stopped before total ({len(output)}/{expected_total})')
            return output
        if expected_total is not None and len(output) >= expected_total:
            return output
        if next_cursor:
            next_cursor = str(next_cursor)
            if next_cursor == cursor or next_cursor in seen:
                raise ExportError(f'{endpoint}: repeated pagination cursor; export would be incomplete')
            seen.add(next_cursor)
            cursor = next_cursor
            continue
        if len(batch) >= limit or (expected_total is not None and len(output) < expected_total):
            raise ExportError(f'{endpoint}: full/incomplete page without cursor; cannot prove completeness')
        return output
    raise ExportError(f'{endpoint}: maximum page count reached')

def chunks(values, size):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]

def safe_media_url(url, check_dns=True):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ExportError('Media URL must be credential-free HTTPS on port 443')
    host = (parsed.hostname or '').lower()
    if not any(host == base or host.endswith('.' + base) for base in MEDIA_HOSTS):
        raise ExportError('Media hostname is outside the Ozon CDN allowlist')
    if check_dns:
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError:
            raise TransientMediaError('Media hostname could not be resolved') from None
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ExportError('Media hostname resolved to a non-public address')
    return url

class SafeMediaRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_media_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def collect_media(value, found=None):
    """Find CDN assets including URLs embedded in rich-content JSON. Preserve other links in raw data."""
    if found is None:
        found = set()
    if isinstance(value, dict):
        for child in value.values():
            collect_media(child, found)
    elif isinstance(value, list):
        for child in value:
            collect_media(child, found)
    elif isinstance(value, str):
        if value.lstrip().startswith(('{', '[')):
            try:
                collect_media(json.loads(value), found)
            except (ValueError, RecursionError):
                pass
        for url in re.findall(r'https://[^\s<>"\x27\\]+', value):
            try:
                safe_media_url(url, check_dns=False)
                found.add(url)
            except (ExportError, ValueError):
                pass
    return found

def collect_external_links(value):
    urls = set()
    def visit(v):
        if isinstance(v, dict):
            for x in v.values(): visit(x)
        elif isinstance(v, list):
            for x in v: visit(x)
        elif isinstance(v, str):
            if v.lstrip().startswith(('{', '[')):
                try: visit(json.loads(v))
                except (ValueError, RecursionError): pass
            for url in re.findall(r'https?://[^\s<>"\x27\\]+', v):
                try: safe_media_url(url, check_dns=False)
                except (ExportError, ValueError): urls.add(url)
    visit(value)
    return sorted(urls)

class Media:
    def __init__(self, root, refresh=False, max_bytes=100 * 1024 * 1024, opener=None, sleeper=time.sleep, attempts=3):
        self.root = Path(root)
        self.sleep = sleeper
        self.attempts = attempts
        self.refresh = refresh
        self.max_bytes = max_bytes
        self.opener = opener or urllib.request.build_opener(SafeMediaRedirect())
        self.directory = self.root / 'media'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / 'media-index.json'
        self.index = json.loads(self.index_path.read_text()) if self.index_path.exists() else {}

    def download(self, url):
        for attempt in range(self.attempts):
            try:
                return self._download_once(url)
            except TransientMediaError:
                if attempt == self.attempts - 1:
                    raise
                self.sleep(min(2 ** attempt, 8))
        raise ExportError('Media attempts exhausted')

    def _download_once(self, url):
        safe_media_url(url, check_dns=False)
        old = self.index.get(url)
        if old and not self.refresh:
            path = self.root / old['file']
            # Verify retained content before reusing a checkpoint.
            if path.is_file() and fingerprint_bytes(path) == old.get('sha256'):
                return old
        safe_media_url(url)
        req = urllib.request.Request(url, headers={'User-Agent': 'LemashkiCatalogExporter/1.0'})
        temp = self.directory / (fingerprint(url) + '.part')
        try:
            with self.opener.open(req, timeout=60) as response, temp.open('wb') as output:
                mime = response.headers.get_content_type()
                allowed = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif', 'image/avif': '.avif', 'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov', 'application/pdf': '.pdf'}
                if mime not in allowed:
                    raise ExportError('Unsupported media content type')
                size = 0
                digest = hashlib.sha256()
                head = b''
                while True:
                    block = response.read(128 * 1024)
                    if not block:
                        break
                    if not head:
                        head = block[:32]
                    size += len(block)
                    if size > self.max_bytes:
                        raise ExportError('Media exceeds configured size limit')
                    digest.update(block)
                    output.write(block)
            if not size:
                raise ExportError('Empty media response')
            signatures = {'image/jpeg': head.startswith(b'\xff\xd8\xff'), 'image/png': head.startswith(b'\x89PNG\r\n\x1a\n'), 'image/webp': head[:4] == b'RIFF' and head[8:12] == b'WEBP', 'image/gif': head.startswith((b'GIF87a', b'GIF89a')), 'application/pdf': head.startswith(b'%PDF-'), 'image/avif': head[4:8] == b'ftyp', 'video/mp4': head[4:8] == b'ftyp', 'video/quicktime': head[4:8] in (b'ftyp', b'moov', b'wide'), 'video/webm': head.startswith(b'\x1aE\xdf\xa3')}
            if not signatures[mime]:
                raise ExportError('Media signature does not match content type')
            dest = self.directory / (fingerprint(url) + allowed[mime])
            temp.replace(dest)
            result = {'file': str(dest.relative_to(self.root)), 'content_type': mime, 'bytes': size, 'sha256': digest.hexdigest()}
            self.index[url] = result
            atomic_json(self.index_path, self.index)
            return result
        except urllib.error.HTTPError as exc:
            if exc.code in RETRY_CODES:
                raise TransientMediaError(f'Media HTTP {exc.code}') from None
            raise ExportError(f'Media HTTP {exc.code}; check CDN access and URL validity') from None
        except (urllib.error.URLError, TimeoutError):
            raise TransientMediaError('Media network request failed') from None
        except (OSError, ValueError):
            raise ExportError('Media file could not be saved; check filesystem access and URL validity') from None
        finally:
            temp.unlink(missing_ok=True)

def fingerprint_bytes(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(128 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

class Exporter:
    def __init__(self, api, root, media=None, progress=print):
        self.api, self.root, self.media, self.progress = api, Path(root), media, progress
        self.errors = []
        self.warnings = []

    def problem(self, scope, error):
        self.errors.append({'scope': scope, 'error': str(error)})
        self.progress(f'Incomplete stage: {scope} (see manifest.json)')

    def run(self):
        started = now()
        self.root.mkdir(parents=True, exist_ok=True)
        items = []
        try:
            items = paged(self.api, ENDPOINTS['list'], {'filter': {'visibility': 'ALL'}}, 'last_id', 1000)
        except AuthenticationError:
            raise
        except ExportError as exc:
            self.problem('list', exc)
        by_id = {}
        for item in items:
            pid = product_id(item)
            if pid in by_id:
                self.problem('list', ExportError('Duplicate product ID across pages; catalog may have changed'))
                continue
            by_id[pid] = {'product_id': pid, 'offer_id': item.get('offer_id'), 'list': item, 'info': None, 'attributes': None, 'description': None, 'prices': None, 'stocks': None}
        ids = list(by_id)
        self.progress(f'Found {len(ids)} products (visibility ALL, including archived where returned by API)')
        for group in chunks(ids, 100):
            numeric_ids = [int(x) for x in group]
            jobs = {
                'info': lambda: records(self.api.call(ENDPOINTS['info'], {'product_id': numeric_ids})),
                'attributes': lambda: records(self.api.call(ENDPOINTS['attributes'], {'filter': {'product_id': [str(x) for x in group], 'visibility': 'ALL'}, 'limit': 1000, 'sort_dir': 'ASC'})),
                'prices': lambda: paged(self.api, ENDPOINTS['prices'], {'filter': {'product_id': [str(x) for x in group], 'visibility': 'ALL'}}, 'cursor', 1000),
                'stocks': lambda: paged(self.api, ENDPOINTS['stocks'], {'filter': {'product_id': [str(x) for x in group], 'visibility': 'ALL'}}, 'cursor', 1000),
            }
            for field, call in jobs.items():
                try:
                    for item in call():
                        pid = product_id(item)
                        if pid in by_id:
                            by_id[pid][field] = item
                    if field in ('info', 'attributes'):
                        missing = [pid for pid in group if by_id[pid][field] is None]
                        if missing:
                            self.problem(field, ExportError('Missing records for product IDs: ' + ','.join(missing)))
                except AuthenticationError:
                    raise
                except (ExportError, ValueError, TypeError) as exc:
                    self.problem(field, exc)
        for offset, pid in enumerate(ids, 1):
            try:
                response = self.api.call(ENDPOINTS['description'], {'product_id': int(pid)})
                value = unwrap(response)
                if not isinstance(value, dict) or 'description' not in value:
                    raise ExportError('Description response does not contain description field')
                by_id[pid]['description'] = value
            except AuthenticationError:
                raise
            except ExportError as exc:
                self.problem('description:' + pid, exc)
            if offset % 20 == 0 or offset == len(ids):
                self.progress(f'Descriptions {offset}/{len(ids)}')
        dictionaries = {'category_tree': None, 'attribute_names': {}}
        if ids:
            try:
                dictionaries['category_tree'] = self.api.call(ENDPOINTS['category_tree'], {'language': 'RU'})
            except AuthenticationError:
                raise
            except ExportError as exc:
                self.problem('category_tree', exc)
            pairs = set()
            for p in by_id.values():
                attrs, info = p['attributes'] or {}, p['info'] or {}
                category = attrs.get('description_category_id') or info.get('description_category_id')
                type_id = attrs.get('type_id') or info.get('type_id')
                if category and type_id:
                    pairs.add((int(category), int(type_id)))
            for category, type_id in sorted(pairs):
                try:
                    dictionaries['attribute_names'][f'{category}:{type_id}'] = self.api.call(ENDPOINTS['attribute_names'], {'description_category_id': category, 'type_id': type_id, 'language': 'RU'})
                except AuthenticationError:
                    raise
                except ExportError as exc:
                    self.problem(f'attribute_names:{category}:{type_id}', exc)
        products = list(by_id.values())
        for p in products:
            p['media_urls'] = sorted(collect_media(p))
            p['external_links_not_downloaded'] = collect_external_links(p)
        media_urls = sorted({url for p in products for url in p['media_urls']})
        media_results = {}
        external_links = sorted({url for p in products for url in p['external_links_not_downloaded']})
        if external_links:
            self.warnings.append(f'{len(external_links)} external/non-CDN link(s) retained but not downloaded; see external-links.json')
        atomic_json(self.root / 'external-links.json', external_links)
        if self.media:
            for offset, url in enumerate(media_urls, 1):
                try:
                    media_results[url] = self.media.download(url)
                except ExportError as exc:
                    self.problem('media:' + fingerprint(url)[:16], exc)
                if offset % 20 == 0 or offset == len(media_urls):
                    self.progress(f'Media {offset}/{len(media_urls)}')
        elif media_urls:
            self.warnings.append('Media files intentionally skipped; URLs retained. Run without --skip-media to download files.')
        coverage = {field: [p['product_id'] for p in products if p[field] is None] for field in ('info', 'attributes', 'description', 'prices', 'stocks')}
        # Some archived/non-sale products have no price/stock records; mark explicitly rather than inventing zero values.
        for field in ('prices', 'stocks'):
            if coverage[field]:
                self.warnings.append(f'{field}: API returned no records for {len(coverage[field])} product(s); see missing_records')
        atomic_json(self.root / 'products.json', products)
        atomic_json(self.root / 'dictionaries.json', dictionaries)
        atomic_json(self.root / 'media-files.json', media_results)
        with (self.root / 'products.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['product_id', 'offer_id', 'name', 'description', 'images', 'price', 'stocks'])
            writer.writeheader()
            for p in products:
                row = {'product_id': p['product_id'], 'offer_id': p['offer_id'] or '', 'name': (p['info'] or {}).get('name', ''), 'description': (p['description'] or {}).get('description', ''), 'images': '\n'.join(p['media_urls']), 'price': json.dumps(p['prices'], ensure_ascii=False), 'stocks': json.dumps(p['stocks'], ensure_ascii=False)}
                # Keep formula-like seller text literal in spreadsheet programs; JSON retains exact original.
                writer.writerow({k: ("'" + str(v) if str(v).startswith(('=', '+', '-', '@', '\t', '\r')) else v) for k, v in row.items()})
        manifest = {'started_at': started, 'finished_at': now(), 'status': 'partial' if self.errors else ('metadata_only' if self.media is None else 'complete'), 'source': 'Ozon Seller API', 'scope': 'Owned product catalog; not orders, buyers, reviews, marketplace-only content or full warehouse/report APIs', 'products': len(products), 'media_urls': len(media_urls), 'media_downloaded': len(media_results), 'missing_records': coverage, 'errors': self.errors, 'warnings': self.warnings, 'api_requests': getattr(self.api, 'events', []), 'endpoint_versions': ENDPOINTS}
        atomic_json(self.root / 'manifest.json', manifest)
        return manifest

def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only full product-data export through official Ozon Seller API. No third-party packages.')
    parser.add_argument('--output', type=Path, default=Path('.data/ozon-export'), help='Private output directory; default .data/ozon-export')
    parser.add_argument('--refresh', action='store_true', help='Refetch all API responses and media; default resumes from retained checkpoints')
    parser.add_argument('--skip-media', action='store_true', help='Save all returned media URLs without downloading binaries; reports metadata_only')
    parser.add_argument('--max-media-mb', type=int, default=100, help='Per-file size cap (default 100 MiB); oversized assets are reported as errors')
    args = parser.parse_args(argv)
    client_id, api_key = os.environ.get('OZON_CLIENT_ID', '').strip(), os.environ.get('OZON_API_KEY', '').strip()
    if not client_id or not api_key:
        parser.error('Set OZON_CLIENT_ID and OZON_API_KEY securely in environment settings. Never pass keys as command-line arguments.')
    if args.max_media_mb < 1 or args.max_media_mb > 2048:
        parser.error('--max-media-mb must be between 1 and 2048')
    # Data belongs outside the public web root and credentials are never written.
    public = Path(__file__).resolve().parents[1] / 'public'
    output = args.output.resolve()
    if output == public or public in output.parents:
        parser.error('Output directory must not be inside public/')
    output.mkdir(parents=True, exist_ok=True)
    account_path = output / 'account.json'
    account = {'client_id_sha256': fingerprint(client_id)}
    if account_path.exists() and json.loads(account_path.read_text()) != account:
        parser.error('This directory belongs to another Seller account. Choose a separate --output directory.')
    atomic_json(account_path, account)
    api = API(client_id, api_key, output, refresh=args.refresh)
    media = None if args.skip_media else Media(output, refresh=args.refresh, max_bytes=args.max_media_mb * 1024 * 1024)
    try:
        manifest = Exporter(api, output, media).run()
    except (ExportError, ValueError, OSError) as exc:
        atomic_json(output / 'manifest.json', {'status': 'failed', 'finished_at': now(), 'error': str(exc), 'api_requests': api.events})
        print('Export failed; see manifest.json. Existing cached responses were retained.', file=sys.stderr)
        return 1
    print(f"Export status: {manifest['status']}; products: {manifest['products']}; downloaded media: {manifest['media_downloaded']}")
    print('Output: ' + str(output))
    if manifest['warnings']:
        print('Read warnings and missing_records in manifest.json before using the export.')
    return 1 if manifest['errors'] else 0

if __name__ == '__main__':
    sys.exit(main())
