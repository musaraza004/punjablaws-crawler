import base64
from contextlib import redirect_stdout
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import crawl_punjablaws as crawl
import run_crawl


def token(value, mask):
    data = bytes(b for char in value.encode() for b in (mask, mask ^ char))
    return base64.b64encode(data).decode().translate(str.maketrans('=/+', '-~.'))


class Response(io.BytesIO):
    def __init__(self, url, body, content_type='text/html'):
        super().__init__(body.encode())
        self.url, self.status = url, 200
        self.headers = Message()
        self.headers['Content-Type'] = content_type + '; charset=utf-8'


class CrawlerTests(unittest.TestCase):
    def test_unreachable_network_stops_early(self):
        def no_network_preflight():
            crawl.DELAY = 0
            crawl.ROBOTS = None
        with tempfile.TemporaryDirectory() as folder, patch.object(run_crawl, 'preflight', side_effect=no_network_preflight), patch('urllib.request.urlopen', side_effect=urllib.error.URLError('timed out')), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'No HTML pages'):
                run_crawl.main(['--output',folder,'--workers','2','--retries','0'])
            s=json.loads((Path(folder)/'punjablaws_crawl/summary.json').read_text())
            self.assertFalse(s['complete'])
            self.assertGreater(s['pending'],0)
            self.assertLessEqual(s['fetched_pages'],3)
            self.assertIn('halt_reason',s)

    def test_scope_and_redirects(self):
        self.assertEqual(crawl.canonical('http://punjabcode.punjab.gov.pk/en/page/faqs#top'), crawl.BASE+'en/page/faqs')
        self.assertIsNone(crawl.canonical('https://example.org/'))
        self.assertIsNone(crawl.canonical('/admin/delete'))
        self.assertIsNone(crawl.canonical('/assets/logo.png'))
        request = run_crawl.urllib.request.Request(crawl.BASE)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            run_crawl.ScopedRedirects().redirect_request(request, None, 302, '', {}, 'https://example.org/')
        caught.exception.close()

    def test_preflight_loads_rules_and_sitemap(self):
        def open_mock(req, **kwargs):
            if req.full_url.endswith('robots.txt'):
                return Response(req.full_url, 'User-agent: *\nDisallow: /private\nCrawl-delay: 2', 'text/plain')
            return Response(req.full_url, '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'+crawl.BASE+'</loc></url></urlset>', 'application/xml')
        with tempfile.TemporaryDirectory() as folder, patch.object(crawl, 'ROOT', Path(folder)), patch.object(crawl, 'DELAY', 0.3), patch.object(crawl, 'ROBOTS', None), patch('urllib.request.urlopen', side_effect=open_mock):
            run_crawl.preflight()
            self.assertFalse(crawl.ROBOTS.can_fetch(crawl.UA, crawl.BASE+'private'))
            self.assertEqual(crawl.DELAY, 2)
            self.assertTrue((Path(folder)/'probes/sitemap.xml').exists())

    def test_capped_resume_retry_aliases_and_pdf_inventory(self):
        a, alias, b, child = token('42', 3), token('42', 7), token('43', 4), token('44', 8)
        path_a = 'en/show_article/'+a
        requested = []
        attempts = 0

        def open_mock(req, **kwargs):
            nonlocal attempts
            url = req.full_url
            requested.append(url)
            self.assertFalse(url.endswith('.pdf'), 'PDF binaries must never be fetched')
            if url == crawl.BASE:
                return Response(url, f'<title>Home</title><a href="/{path_a}">Shared title</a><a href="/en/show_article/{alias}">Shared title</a><a href="/en/show_article/{b}">Shared title</a>')
            if url.endswith(path_a):
                attempts += 1
                if attempts == 1:
                    raise urllib.error.HTTPError(url, 503, 'Temporary error', {}, None)
                return Response(url, f'<title>Law A</title><script>var article_id = "42";</script><object data="/uploads/articles/law-a.pdf"></object><a href="#" onclick="get_article_file(this)" id="{child}">Child rule</a>')
            if '/show_article/' in url:
                identity = '43' if url.endswith(b) else '44'
                return Response(url, f'<title>Law {identity}</title><script>var article_id = "{identity}";</script>')
            return Response(url, '<title>Index</title>')

        def no_network_preflight():
            crawl.DELAY = 0
            crawl.ROBOTS = None
            crawl.ROBOTS_STATUS = 'Mocked preflight for offline tests'

        with tempfile.TemporaryDirectory() as folder, patch('urllib.request.urlopen', side_effect=open_mock), patch.object(run_crawl, 'preflight', side_effect=no_network_preflight), patch.object(crawl.time, 'sleep'), redirect_stdout(io.StringIO()):
            common = ['--output', folder, '--workers', '1']
            run_crawl.main(common + ['--max-pages', '1'])
            root = Path(folder)/'punjablaws_crawl'
            initial = json.loads((root/'summary.json').read_text())
            self.assertFalse(initial['complete'])
            self.assertEqual(initial['fetched_pages'], 1)
            run_crawl.main(common)
            summary = json.loads((root/'summary.json').read_text())
            self.assertTrue(summary['complete'])
            self.assertEqual(summary['pending'], 0)
            self.assertEqual(summary['documents'], 1)
            self.assertEqual(summary['unique_law_records_by_language'], 3)
            self.assertEqual(summary['archive_hash_mismatches'], [])
            self.assertEqual(attempts, 2)
            self.assertNotIn(crawl.BASE+'en/show_article/'+alias, requested)
            self.assertEqual(requested.count(crawl.BASE), 1, 'Resume must not re-fetch completed pages')
            self.assertIn('law-a.pdf', (root/'documents.csv').read_text(encoding='utf-8-sig'))
            self.assertFalse((root/'.crawl.lock').exists())
            with zipfile.ZipFile(Path(folder)/'punjablaws_crawl.zip') as archive:
                self.assertIsNone(archive.testzip())
                self.assertNotIn('punjablaws_crawl/.crawl.lock', archive.namelist())


if __name__ == '__main__':
    unittest.main()
