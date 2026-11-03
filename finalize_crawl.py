"""Verify a finished crawl, retry transient failures, and package its artifacts."""
import argparse
import concurrent.futures as cf
import csv
import gzip
import hashlib
import json
from pathlib import Path
import time
import zipfile
from crawl_punjablaws import ROOT, Parser, canonical, fetch, DOCS
from urllib.parse import urlsplit

def write_csv(name, rows, fields):
    with (ROOT/name).open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--retry',action='store_true');parser.add_argument('--allow-partial',action='store_true');args=parser.parse_args(argv)
    summary=json.loads((ROOT/'summary.json').read_text())
    if not summary['complete'] and not args.allow_partial: raise SystemExit('Crawl is incomplete; use --allow-partial to package a checkpoint.')
    records={};bad=[]
    for n,line in enumerate((ROOT/'results.jsonl').read_text(encoding='utf-8').splitlines(),1):
        try: row=json.loads(line)
        except json.JSONDecodeError:
            bad.append(n);continue
        old=records.get(row['url'])
        if old and old['status']==200 and row['status']!=200: continue
        records[row['url']]=row
    for row in records.values():
        if row['status']==200 and not row['bytes'] and not row.get('is_document'): row['error']='Empty response body'
    inventory=json.loads((ROOT/'inventory.json').read_text(encoding='utf-8'))
    for u,row in records.items():
        inventory.setdefault(u,{'kind':'page','title':row['title'],'source':'fetch_log'})
    if args.retry:
        for attempt in range(2):
            failed=[u for u,r in records.items() if (r['status']==0 or r['status']>=500 or r['error'] in ('Incomplete response body','Empty response body')) and not inventory.get(u,{}).get('fetch_policy')]
            if not failed: break
            print('Retry pass',attempt+1,':',len(failed),'URLs',flush=True)
            with cf.ThreadPoolExecutor(max_workers=4) as pool:
                for row,links in pool.map(fetch,failed):
                    records[row['url']]=row
                    for target,title in links:
                        inventory.setdefault(target,{'kind':'document' if Path(urlsplit(target).path).suffix.lower() in DOCS else 'page','title':title,'source':'retry'})
    with (ROOT/'results.jsonl').open('w',encoding='utf-8') as f:
        for row in records.values(): f.write(json.dumps(row,ensure_ascii=False)+'\n')
    fields=['url','status','title','article_id','content_type','bytes','file','text_file','error']
    write_csv('pages.csv',records.values(),fields)
    errors=[r for r in records.values() if r['error'] or not 200<=r['status']<300]
    write_csv('failed_pages.csv',errors,fields)
    docs={u:dict(v,url=u,referring_pages=set()) for u,v in inventory.items() if v['kind']=='document'}
    broken_files=[];hash_mismatches=[];article_ids=set();language_counts={}
    with (ROOT/'links.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.writer(f);writer.writerow(['source_url','target_url','link_text'])
        for u,row in records.items():
            if row['status']!=200 or not row.get('file'): continue
            file=ROOT/row['file']
            if not file.exists(): broken_files.append(str(file)); continue
            if row.get('article_id'):
                lang=u.split('/')[3];article_ids.add((lang,row['article_id']))
                language_counts[lang]=language_counts.get(lang,0)+1
            raw=gzip.decompress(file.read_bytes())
            actual_hash=hashlib.sha256(raw).hexdigest()
            if row.get('sha256') and actual_hash!=row['sha256']: hash_mismatches.append(u)
            p=Parser();p.feed(raw.decode('utf-8',errors='replace'))
            for href,title in set(p.links):
                if not href or href.startswith(('#','javascript:','mailto:','tel:')): continue
                target=canonical(href,u)
                if not target: continue
                writer.writerow([u,target,title])
                if target in docs:
                    docs[target]['referring_pages'].add(u)
                    if title and not docs[target].get('title'): docs[target]['title']=title
                    if docs[target].get('title','').strip().lower() in ('','download','view','full screen') and row['title'] not in ('','The Punjab Code','پنجاب کوڈ'):
                        docs[target]['title']=row['title']
    doc_rows=[]
    for u,row in sorted(docs.items()):
        refs=sorted(row['referring_pages'])
        inventory[u]['title']=row.get('title','')
        doc_rows.append(dict(row,referring_pages=' | '.join(refs),referring_page_count=len(refs),verification='linked from fetched page' if refs else 'sitemap only; not verified'))
    write_csv('documents.csv',doc_rows,['url','title','verification','referring_page_count','referring_pages'])
    for u,row in inventory.items():
        result=records.get(u)
        if result:
            row['status']=result['status']
            if result['title']: row['title']=result['title']
        if row.get('alias_of'):
            result=records.get(row['alias_of'],{})
            row['resolved_status']=result.get('status','')
            if not row.get('title'): row['title']=result.get('title','')
    (ROOT/'inventory.json').write_text(json.dumps(inventory,ensure_ascii=False),encoding='utf-8')
    (ROOT/'urls.txt').write_text('\n'.join(sorted(inventory))+'\n',encoding='utf-8')
    write_csv('urls.csv',[dict(row,url=u) for u,row in sorted(inventory.items())],['url','kind','title','source','status','alias_of','resolved_status','fetch_policy'])
    summary.update(fetched_pages=len(records),successful_pages=sum(r['status']==200 for r in records.values()),errors=len(errors),documents=len(docs),documents_linked_from_fetched_pages=sum(bool(d['referring_page_count']) for d in doc_rows),unique_law_records_by_language=len(article_ids),law_page_language_counts=language_counts,damaged_log_lines_removed=bad,missing_archive_files=broken_files,pdf_binaries_downloaded=False)
    unresolved=[u for u,r in inventory.items() if r['kind']=='page' and u not in records and not r.get('alias_of') and not r.get('fetch_policy')]
    summary['unresolved_discovered_page_urls']=len(unresolved)
    summary['discovered_urls']=len(inventory)
    summary['complete']=not unresolved
    summary['pending']=len(unresolved)
    summary['aliases_skipped']=sum(bool(v.get('alias_of')) for v in inventory.values())
    (ROOT/'unresolved_urls.txt').write_text('\n'.join(unresolved),encoding='utf-8')
    summary['status_counts']={str(k):sum(r['status']==k for r in records.values()) for k in sorted({r['status'] for r in records.values()})}
    summary['successful_pages']=sum(r['status']==200 and not r['error'] and 'html' in r['content_type'] for r in records.values())
    summary['historical_links_not_fetched']=sum(bool(r.get('fetch_policy')) and u not in records for u,r in inventory.items())
    summary['archive_hash_mismatches']=hash_mismatches
    (ROOT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    (ROOT/'README.md').write_text('''# Punjab Laws crawl

Source: https://punjablaws.punjab.gov.pk/

See summary.json for the run timestamp, request settings, counts, and coverage. A capped run is explicitly marked incomplete.

## Files

- urls.csv / urls.txt: every discovered same-site page or document URL, including aliases and historical sitemap links.
- pages.csv: actual HTTP fetch results, page titles, stable article IDs when exposed, and local archive paths.
- documents.csv: linked PDF and other document URLs with their referring pages.
- failed_pages.csv: unsuccessful or incomplete fetches remaining after retries.
- links.csv: links extracted from successful page responses.
- results.jsonl / inventory.json: machine-readable results and discovery state.
- pages/: original HTML and visible page text, compressed with gzip. Open .html.gz or .txt.gz after decompression.
- probes/sitemap.xml: the source sitemap, preserved unchanged.

## Coverage and limits

The crawl follows public GET links, index pagination, English and Urdu navigation, and subordinate law links exposed by JavaScript click handlers. It does not submit forms, search arbitrary terms, or access login areas. Images, stylesheets, and scripts are excluded from the page archive.

The sitemap lists an older hostname, punjabcode.punjab.gov.pk. Its paths were mapped to the requested host. Historical long encoded article links use an older format; a sample returned HTTP 500. Those URLs remain in the inventory with an explicit fetch policy while current law links are followed from live indexes. Consequently, the inventory does not imply that every historical URL was fetched.

Current article URLs contain randomized encoded identifiers. Equivalent short identifiers are grouped using their stable byte-pair XOR value, preserving separate language routes and separate records with identical titles. Aliases point to the representative fetched URL. HTML content, article IDs, and PDF links remain available for review.

PDF and other document binaries are inventoried by URL and are not downloaded. Many law pages embed PDFs, so extracted HTML text does not include the full text of those laws. Archived HTML retains its original online links and is not an offline mirror.

The crawler checks robots.txt at startup, follows readable directives, and records unavailable crawl rules in summary.json. Request rate, concurrency, and retries are configurable. Defaults are four workers, a 0.3-second minimum interval between request starts, and two retries for transient failures. A finite link crawl cannot establish the existence of unlinked or dynamically generated pages.
''',encoding='utf-8')
    archive=ROOT.parent/'punjablaws_crawl.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=3) as z:
        for file in ROOT.rglob('*'):
            if file.is_file() and file.name!='.crawl.lock': z.write(file,Path('punjablaws_crawl')/file.relative_to(ROOT))
        for name in ('crawl_punjablaws.py','finalize_crawl.py','run_crawl.py'):
            z.write(Path(__file__).resolve().parent/name,name)
    with zipfile.ZipFile(archive) as z:
        corrupt=z.testzip()
        if corrupt: raise RuntimeError('Archive integrity failure: '+corrupt)
    print(json.dumps(summary,indent=2),flush=True)
    print('Verified archive:',archive,'bytes:',archive.stat().st_size,flush=True)
    if broken_files or hash_mismatches: raise RuntimeError('Saved page integrity checks failed; see summary.json.')

if __name__=='__main__': main()
