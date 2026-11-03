"""Resumable public HTML crawl; stdlib only. PDFs are inventoried, not downloaded."""
import concurrent.futures as cf
import base64
import collections
import csv
import gzip
import hashlib
import html
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import re
import sys
import threading
import time
import urllib.error
import urllib.parse as up
import urllib.request as ur
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')
BASE = 'https://punjablaws.punjab.gov.pk/'
ROOT = Path(__file__).resolve().parent / 'output' / 'punjablaws_crawl'
ROOT.mkdir(parents=True, exist_ok=True)
(ROOT / 'pages').mkdir(exist_ok=True)
UA = 'PunjabLawsPublicCrawler/1.0 (public page inventory)'
HOSTS = {'punjablaws.punjab.gov.pk', 'punjabcode.punjab.gov.pk'}
SKIP = {'.jpg','.jpeg','.png','.gif','.svg','.css','.js','.ico','.woff','.woff2','.ttf','.eot','.mp4','.mp3'}
DOCS = {'.pdf','.doc','.docx','.xls','.xlsx','.zip','.rtf','.txt'}
gate = threading.Lock()
last_request = 0.0
DELAY = 0.3
WORKERS = 4
MAX_PAGES = 0
RETRIES = 2
INCLUDE_LEGACY = False
ROBOTS = None
ROBOTS_STATUS = 'Not checked; use run_crawl.py for preflight checks.'

def canonical(url, source=BASE):
    url = up.urljoin(source, html.unescape(url.strip()))
    p = up.urlsplit(url)
    if p.scheme not in ('http','https') or p.hostname not in HOSTS:
        return None
    path = re.sub('/+', '/', p.path or '/')
    if Path(path).suffix.lower() in SKIP or any(x in path.lower().split('/') for x in ('admin','logout','login')):
        return None
    path = up.quote(path, safe='/%:@!$&\'()*+,;=-._~')
    return up.urlunsplit(('https','punjablaws.punjab.gov.pk',path.rstrip('/') or '/',p.query,''))

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links=[]; self.title=[]; self.text=[]; self.depth=0; self.in_title=False; self.anchor=None
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag in ('script','style'): self.depth += 1
        if tag=='title': self.in_title=True
        if tag=='a':
            target=a.get('href','')
            if 'get_article_file' in a.get('onclick','') and a.get('id'):
                target='/en/show_article/'+a['id']
            self.anchor=[target,[]]
        if tag in ('iframe','embed','object'):
            target=a.get('src') or a.get('data')
            if target: self.links.append((target,''))
        if tag in ('br','p','div','li','h1','h2','tr'): self.text.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script','style'): self.depth=max(0,self.depth-1)
        if tag=='title': self.in_title=False
        if tag=='a' and self.anchor:
            self.links.append((self.anchor[0], ' '.join(self.anchor[1]).strip()))
            self.anchor=None
    def handle_data(self,data):
        if self.in_title: self.title.append(data)
        if not self.depth:
            self.text.append(data)
            if self.anchor: self.anchor[1].append(data.strip())

def _fetch_once(url):
    global last_request
    row={'url':url,'fetched_at':datetime.now(timezone.utc).isoformat(),'status':0,'title':'','content_type':'','bytes':0,'file':'','text_file':'','article_id':'','sha256':'','error':''}
    try:
        if ROBOTS is not None and not ROBOTS.can_fetch(UA, url):
            row['error']='Blocked by robots.txt'
            return row, []
        with gate:
            delay = DELAY - (time.monotonic()-last_request)
            if delay>0: time.sleep(delay)
            last_request=time.monotonic()
        req=ur.Request(url,headers={'User-Agent':UA,'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5'})
        with ur.urlopen(req,timeout=25) as response:
            final=response.url
            if up.urlsplit(final).hostname not in HOSTS:
                row['error']='Redirect outside crawl scope: '+final
                return row, []
            row.update(status=response.status,content_type=response.headers.get_content_type(),final_url=final)
            if 'html' not in row['content_type']:
                row['is_document']=True
                return row, []
            try: body=response.read()
            except http.client.IncompleteRead as e:
                body=e.partial
                row['error']='Incomplete response body'
            encoding=response.headers.get_content_charset() or 'utf-8'
        row['bytes']=len(body)
        if not body: row['error']='Empty response body'
        row['sha256']=hashlib.sha256(body).hexdigest()
        key=hashlib.sha256(url.encode()).hexdigest()[:24]
        row['file']='pages/'+key+'.html.gz'
        (ROOT/row['file']).write_bytes(gzip.compress(body))
        if 'html' not in row['content_type']: return row, []
        text=body.decode(encoding,errors='replace')
        parser=Parser(); parser.feed(text)
        row['title']=' '.join(parser.title).strip()
        match=re.search(r'var\s+article_id\s*=\s*[\"\x27](\d+)',text)
        if match: row['article_id']=match.group(1)
        clean=re.sub(r'[ \t\r\f\v]+',' ', ''.join(parser.text))
        clean=re.sub(r'\n\s*\n+', '\n\n',clean).strip()
        row['text_file']='pages/'+key+'.txt.gz'
        (ROOT/row['text_file']).write_bytes(gzip.compress(clean.encode('utf-8')))
        links=[]
        for href,title in parser.links:
            if not href or href.startswith(('#','javascript:','mailto:','tel:')): continue
            target=canonical(href,url)
            if target: links.append((target,title))
        return row,links
    except urllib.error.HTTPError as e:
        row.update(status=e.code,error=str(e))
        row['retry_after']=e.headers.get('Retry-After','') if e.headers else ''
        e.close()
    except Exception as e: row['error']=str(e)
    return row,[]

def fetch(url):
    for attempt in range(RETRIES+1):
        row, links = _fetch_once(url)
        row['attempts'] = attempt+1
        transient = row['status'] in (0,408,429) or row['status']>=500 or row['error'] in ('Incomplete response body','Empty response body')
        if row['error']=='Blocked by robots.txt' or not transient or attempt==RETRIES:
            return row,links
        print(f"Retry {attempt+1}/{RETRIES}: {url}: {row['error']}", flush=True)
        try: pause=float(row.get('retry_after',''))
        except ValueError: pause=2**attempt
        time.sleep(min(max(pause,1),30))

def main():
    records={}; inventory={}; aliases={}; title_urls={}; article_urls={}; token_urls={}
    log=ROOT/'results.jsonl'
    if log.exists():
        for line in log.read_text(encoding='utf-8').splitlines():
            try: row=json.loads(line)
            except json.JSONDecodeError: continue
            previous=records.get(row['url'])
            if previous and previous.get('status')==200 and row.get('status')!=200: continue
            records[row['url']]=row
    old=ROOT/'inventory.json'
    if old.exists(): inventory=json.loads(old.read_text(encoding='utf-8'))
    pending=collections.deque(); queued=set()
    def token_key(url):
        path=up.urlsplit(url).path
        if '/show_article/' not in path: return None
        token=path.rsplit('/',1)[-1]
        if len(token)>16: return None
        try:
            raw=base64.b64decode(token.translate(str.maketrans('-~.','=/+')),validate=True)
            if not raw or len(raw)%2: return None
            return (path.split('/')[1], bytes(raw[i]^raw[i+1] for i in range(0,len(raw),2)).hex())
        except Exception: return None
    for u,r in records.items():
        key=token_key(u)
        if key: token_urls[key]=u
    def enqueue(url,title='',source='link'):
        url=canonical(url)
        if not url: return
        item=inventory.setdefault(url,{'title':title,'source':source,'kind':'document' if Path(up.urlsplit(url).path).suffix.lower() in DOCS else 'page'})
        if title and not item.get('title'): item['title']=title
        item.pop('alias_of',None)
        if INCLUDE_LEGACY: item.pop('fetch_policy',None)
        if item['kind']=='document' or url in records or url in queued: return
        if not INCLUDE_LEGACY and '/show_article/' in url and len(up.urlsplit(url).path.rsplit('/',1)[-1])>=64:
            item['fetch_policy']='Historical long encoded sitemap link; sampled link returns HTTP 500. Retained in inventory, live indexes used instead.'
            return
        key=token_key(url)
        if key:
            if key in token_urls:
                item['alias_of']=token_urls[key]; return
            token_urls[key]=url
        pending.append(url); queued.add(url)
    enqueue(BASE,source='seed')
    # Live indexes run first; sitemap retains all historical URLs as additional seeds.
    for lang in ('en','urdu'):
        for path in ('index','get_laws','get_rules','alphabetical/all/-1','chronological','departmental','categorical','list_repealed_chrono','list_amending_chrono','proposed_bills','service_rules','general_rules','list_law_gazette_chrono','list_rule_gazette_chrono','recently_updated'):
            enqueue(BASE+lang+'/'+path,source='seed')
    sm=ROOT/'probes/sitemap.xml'
    raw_sitemap=[]
    if sm.exists():
        raw_sitemap=[e.text for e in ET.parse(sm).iter() if e.tag.endswith('loc') and e.text]
        # Non-article sitemap URLs first, to learn current article names and links.
        for u in sorted(raw_sitemap,key=lambda x:'/show_article/' in x): enqueue(u,source='sitemap')
    for u,item in list(inventory.items()): enqueue(u,item.get('title',''),item.get('source','resume'))
    # Reconstruct links from saved responses, including the last partial checkpoint.
    for u,r in records.items():
        if r.get('file') and 'html' in r.get('content_type',''):
            parser=Parser();parser.feed(gzip.decompress((ROOT/r['file']).read_bytes()).decode('utf-8',errors='replace'))
            for href,title in parser.links:
                if href and not href.startswith(('#','javascript:','mailto:','tel:')):
                    enqueue(up.urljoin(u,href),title)
    started=time.monotonic(); completed=0; network_unreachable=False
    fields=['url','status','title','article_id','content_type','bytes','file','text_file','error']
    def save(final=False):
        (ROOT/'inventory.json').write_text(json.dumps(inventory,ensure_ascii=False),encoding='utf-8')
        with (ROOT/'pages.csv').open('w',newline='',encoding='utf-8-sig') as f:
            w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader(); w.writerows(records.values())
        with (ROOT/'urls.csv').open('w',newline='',encoding='utf-8-sig') as f:
            cols=['url','kind','title','source','status','alias_of','fetch_policy']
            w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader()
            for u,item in sorted(inventory.items()): w.writerow(dict(item,url=u,status=records.get(u,{}).get('status','')))
        (ROOT/'urls.txt').write_text('\n'.join(sorted(inventory))+'\n',encoding='utf-8')
        summary={'updated_at':datetime.now(timezone.utc).isoformat(),'complete':final,'discovered_urls':len(inventory),'fetched_pages':len(records),'successful_pages':sum(200<=r['status']<300 and not r['error'] and 'html' in r['content_type'] for r in records.values()),'documents':sum(i['kind']=='document' for i in inventory.values()),'aliases_skipped':sum('alias_of' in i for i in inventory.values()),'historical_links_not_queued':sum('fetch_policy' in i for i in inventory.values()),'pending':len(pending)+len(active),'errors':sum(bool(r['error']) for r in records.values()),'status_counts':dict(collections.Counter(r['status'] for r in records.values())),'sitemap_urls':len(raw_sitemap),'robots':ROBOTS_STATUS,'scope':'Public same-host HTML pages, pagination, and subordinate law links. PDFs are inventoried and never downloaded. Legacy sitemap host mapped to requested host; historical long encoded article links are inventoried unless --include-legacy is selected.','settings':{'workers':WORKERS,'delay':DELAY,'max_pages_per_run':MAX_PAGES,'retries':RETRIES,'include_legacy':INCLUDE_LEGACY}}
        (ROOT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        if network_unreachable:
            summary['halt_reason']='All initial requests failed to connect; pending URLs are preserved for a later run.'
            (ROOT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        print(json.dumps(summary),flush=True)
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool, log.open('a',encoding='utf-8') as out:
        active={}
        save()
        while pending or active:
            while pending and not network_unreachable and len(active)<WORKERS and (not MAX_PAGES or completed+len(active)<MAX_PAGES):
                u=pending.popleft();active[pool.submit(fetch,u)]=u
            if not active: break
            done,_=cf.wait(active,timeout=5,return_when=cf.FIRST_COMPLETED)
            for task in done:
                u=active.pop(task); row,links=task.result()
                records[u]=row;out.write(json.dumps(row,ensure_ascii=False)+'\n');out.flush();completed+=1
                inventory[u]['status']=row['status']
                if row.get('is_document'): inventory[u]['kind']='document'
                if row['title']: inventory[u]['title']=row['title']
                if row['article_id']:
                    key=(up.urlsplit(u).path.split('/')[1],row['article_id'])
                    if key in article_urls: inventory[u]['duplicate_article_of']=article_urls[key]
                    else: article_urls[key]=u
                for target,title in links: enqueue(target,title)
                if completed>=WORKERS and records and all(r['status']==0 for r in records.values()):
                    network_unreachable=True
                if completed%100==0: save()
        save(final=not pending)
    (ROOT/'README.md').write_text('Punjab Laws crawl\n\nSee summary.json for coverage and errors. urls.csv contains all discovered page and document URLs. pages.csv contains fetch results. results.jsonl is the resumable fetch log. pages/ contains gzip-compressed HTML and extracted visible text. PDFs and other documents are inventoried by URL and are not downloaded. The sitemap uses punjabcode.punjab.gov.pk; its paths were requested on punjablaws.punjab.gov.pk. Encoded article links can change between responses; named aliases are recorded to prevent unbounded duplicate discovery. No forms were submitted.\n',encoding='utf-8')

if __name__=='__main__': main()
