# Punjab Laws Python crawler

[Run on GitHub](https://github.com/musaraza004/punjablaws-crawler/actions/workflows/crawl.yml) · [Download results](https://github.com/musaraza004/punjablaws-crawler/releases)

Crawl <https://punjablaws.punjab.gov.pk/> in English and Urdu. Save original HTML and visible page text, collect PDF URLs and titles, follow pagination and subordinate legislation, and export CSV/JSON results. **PDF binaries are never downloaded.** Python 3.11+; no packages or API keys are required.

**Current hosting limitation (3 November 2026):** the local live check saved 5/5 pages, but the GitHub-hosted live check could not connect to the website (20/20 requests timed out). Separate Linux, ARM Linux, and Windows runner checks also timed out at the TCP connection stage. The code and tests run on GitHub, but a successful cloud crawl depends on the website becoming reachable from that runner. See [connectivity checks](https://github.com/musaraza004/punjablaws-crawler/actions/workflows/connectivity.yml). Run locally for free from a network that can reach the website; no proxy or paid hosting is required. The exact cause of the hosted network timeouts has not been established.

## Run free on GitHub

1. Open **Actions → Run crawler → Run workflow**.
2. Leave `max_pages` at **0** for all discoverable pages, or enter **20** for a quick check.
3. When the run finishes, open **Releases** and download `punjablaws_crawl.zip`, `urls.csv`, `documents.csv`, or `summary.json`.

This repository must stay **public** to use the free standard GitHub-hosted runner. It uses `ubuntu-latest`, no larger runners, no paid services, no Actions artifact uploads, and no Actions caches. Downloads are attached to GitHub Releases. GitHub documents [free standard runners in public repositories](https://docs.github.com/en/actions/concepts/billing-and-usage) and [release storage/bandwidth limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas). GitHub's service limits and terms still apply. The workflow runs only when manually requested; it has a 180-minute job limit.

## Run on your computer

```sh
python run_crawl.py --max-pages 20
python run_crawl.py
```

The first command runs a capped check. The second resumes the same output folder and finishes pending pages. Use a new output directory for a fresh crawl:

```sh
python run_crawl.py --output next-crawl
python run_crawl.py --workers 4 --delay 0.5 --retries 2
python run_crawl.py --export-only
```

Defaults: four workers, at least 0.3 seconds between request starts across all workers, and two retries for connection failures, HTTP 408/429/5xx, and incomplete bodies. Readable robots.txt directives and crawl delays are enforced. If robots.txt is unavailable (the site returned HTTP 403 during the initial crawl), its status is recorded and public GET crawling continues. Off-site redirects are blocked. No login areas or forms are submitted.

If all initial page requests fail to connect, the crawler stops early and preserves pending URLs. A run with no saved HTML exits with an error instead of publishing an empty successful release.

## Results

`output/punjablaws_crawl.zip` contains:

| File | Contents |
| --- | --- |
| `urls.csv`, `urls.txt` | All discovered page/document URLs, aliases and historical URLs |
| `pages.csv` | Actual fetch results, titles, stable article IDs and archive paths |
| `documents.csv` | PDF/document URLs, titles and referring pages |
| `failed_pages.csv` | Requests still unsuccessful or incomplete after retries |
| `links.csv` | Source-to-target links |
| `summary.json` | Completion, pending count, request settings and integrity checks |
| `results.jsonl`, `inventory.json` | Resume state |
| `pages/*.html.gz`, `pages/*.txt.gz` | Gzip-compressed original HTML and visible text |
| `probes/` | robots.txt, sitemap and preflight diagnostics when available |

Download and extract the ZIP. Decompress individual `.html.gz` or `.txt.gz` files to inspect them. Archived HTML retains online links; it is not a self-contained offline mirror. Law text embedded inside PDFs is not extracted.

To resume a GitHub result locally, extract its `punjablaws_crawl/` directory into `output/` in your checkout, then run `python run_crawl.py`. Each new GitHub run starts fresh. A `.crawl.lock` prevents simultaneous local writers; after a forced shutdown, remove that file only after confirming the original process has stopped.

## Coverage

The source sitemap uses the older `punjabcode.punjab.gov.pk` hostname; paths are requested on the current host. Obsolete long encoded article links returned server errors during the initial crawl. They are retained in the inventory and excluded by default; use `--include-legacy` if you want to request them too. Current short encoded links are deduplicated using their stable byte-pair XOR identifier, separately for each language. Different records with the same title are preserved.

An unlimited run is complete when no discovered non-document, non-alias, non-excluded URL remains pending. Failed requests are reported separately. This does not establish that unlinked pages, arbitrary searches, or every historical URL were retrieved. Capped runs are explicitly marked incomplete and can be resumed.

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests use mocked website responses, exercise retry discovery, deduplication, page limits, resume, and packaging, and make no network calls.
