"""Run and package a Punjab Laws HTML crawl using Python's standard library."""
import argparse
import http.client
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET

import crawl_punjablaws as crawl


class ScopedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if target.scheme not in ('http', 'https') or target.hostname not in crawl.HOSTS:
            raise urllib.error.HTTPError(req.full_url, 403, 'Redirect outside crawl scope', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def preflight():
    probes = crawl.ROOT / 'probes'
    probes.mkdir(parents=True, exist_ok=True)
    diagnostics = {}
    for name in ('robots.txt', 'sitemap.xml'):
        url = crawl.BASE + name
        try:
            req = urllib.request.Request(url, headers={'User-Agent': crawl.UA})
            with urllib.request.urlopen(req, timeout=30) as response:
                try:
                    body = response.read(10 * 1024 * 1024 + 1)
                except http.client.IncompleteRead as error:
                    body = error.partial
                if len(body) > 10 * 1024 * 1024:
                    raise ValueError('Preflight response exceeds 10 MiB')
                diagnostics[name] = {'status': response.status, 'bytes': len(body)}
            text = body.decode('utf-8', errors='replace')
            if name == 'robots.txt':
                if '<html' in text.lower():
                    raise ValueError('robots.txt returned HTML instead of crawl rules')
                crawl.ROBOTS = urllib.robotparser.RobotFileParser(url)
                crawl.ROBOTS.parse(text.splitlines())
                crawl.ROBOTS_STATUS = 'Loaded robots.txt; directives enforced.'
                specified_delay = crawl.ROBOTS.crawl_delay(crawl.UA)
                if specified_delay:
                    crawl.DELAY = max(crawl.DELAY, specified_delay)
            else:
                root = ET.fromstring(body)
                if root.tag.split('}')[-1] != 'urlset':
                    raise ValueError('Expected a URL sitemap; received an unsupported sitemap format')
            (probes / name).write_bytes(body)
        except (urllib.error.URLError, OSError, ValueError, ET.ParseError) as error:
            diagnostics[name] = {'error': str(error)}
            if name == 'robots.txt':
                crawl.ROBOTS_STATUS = f'Unavailable: {error}; proceeding with public GET links and rate limits.'
            print(f'Preflight {name}: {error}', flush=True)
            if isinstance(error, urllib.error.HTTPError):
                error.close()
    (probes / 'preflight.json').write_text(json.dumps(diagnostics, indent=2), encoding='utf-8')


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('output'), help='Output directory; reuse it to resume')
    parser.add_argument('--max-pages', type=int, default=0, help='New requests in this run; 0 walks all discovered pages')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--delay', type=float, default=0.3, help='Minimum seconds between request starts, shared by workers')
    parser.add_argument('--retries', type=int, default=2)
    parser.add_argument('--include-legacy', action='store_true', help='Also request obsolete long encoded sitemap article URLs')
    parser.add_argument('--export-only', action='store_true', help='Package saved results without requesting website pages')
    args = parser.parse_args(argv)
    if args.max_pages < 0 or not 1 <= args.workers <= 8 or args.delay < 0.1 or not 0 <= args.retries <= 5:
        parser.error('Use max-pages >= 0, workers 1..8, delay >= 0.1, and retries 0..5')
    return args


def main(argv=None):
    args = arguments(argv)
    crawl.ROOT = args.output.resolve() / 'punjablaws_crawl'
    (crawl.ROOT / 'pages').mkdir(parents=True, exist_ok=True)
    crawl.MAX_PAGES, crawl.WORKERS, crawl.DELAY = args.max_pages, args.workers, args.delay
    crawl.RETRIES, crawl.INCLUDE_LEGACY = args.retries, args.include_legacy
    urllib.request.install_opener(urllib.request.build_opener(ScopedRedirects()))
    # One writer per output folder prevents conflicting checkpoints and files.
    lock = crawl.ROOT / '.crawl.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SystemExit(f'Output is already locked: {lock}. Remove the lock only after confirming no crawler is running.')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(str(os.getpid()))
        if not args.export_only:
            preflight()
            crawl.main()
        # Import after configuring ROOT; both modules operate on the same directory.
        import finalize_crawl
        finalize_crawl.ROOT = crawl.ROOT
        finalize_crawl.main(['--allow-partial'])
        summary = json.loads((crawl.ROOT / 'summary.json').read_text(encoding='utf-8'))
        if not summary['successful_pages']:
            raise RuntimeError('No HTML pages were saved. Check the website availability and failed_pages.csv.')
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
