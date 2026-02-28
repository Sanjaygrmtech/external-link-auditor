#!/usr/bin/env python3
"""
External Link Auditor
=====================
Crawls a website to discover all internal pages, then extracts and catalogs
every external link found on each page. Outputs an interactive HTML report.

Usage:
    python crawler.py https://example.com [--max-pages 500] [--delay 0.5] [--output report.html]
"""

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import warnings

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# ─── Configuration ────────────────────────────────────────────────────────────

DEFAULT_MAX_PAGES = 500
DEFAULT_DELAY = 0.3  # seconds between requests
DEFAULT_OUTPUT = "external_link_report.html"
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (compatible; ExternalLinkAuditor/1.0; "
    "+https://github.com/external-link-auditor)"
)

# Known authority domains (gov, edu, major orgs)
AUTHORITY_DOMAINS = {
    ".gov", ".edu", ".mil",
    "who.int", "irs.gov", "consumerfinance.gov", "ftc.gov",
    "sec.gov", "federalreserve.gov", "treasury.gov",
    "ncua.gov", "fdic.gov", "cfpb.gov",
    "cdc.gov", "nih.gov", "fda.gov",
    "wikipedia.org", "britannica.com",
    "reuters.com", "apnews.com",
}


def is_authority_domain(domain: str) -> bool:
    """Check if a domain is a known authority/gov/edu domain."""
    domain = domain.lower()
    for auth in AUTHORITY_DOMAINS:
        if domain.endswith(auth):
            return True
    return False


class ExternalLinkAuditor:
    def __init__(self, start_url: str, max_pages: int = DEFAULT_MAX_PAGES,
                 delay: float = DEFAULT_DELAY):
        parsed = urlparse(start_url)
        if not parsed.scheme:
            start_url = "https://" + start_url
            parsed = urlparse(start_url)

        self.start_url = start_url
        self.base_domain = parsed.netloc.lower().replace("www.", "")
        self.scheme = parsed.scheme
        self.max_pages = max_pages
        self.delay = delay

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        })

        # State
        self.visited = set()
        self.queue = [start_url]
        self.pages_data = {}  # url -> {title, external_links: [{url, anchor, domain, is_authority, status}]}
        self.domain_summary = defaultdict(lambda: {"count": 0, "pages": set()})
        self.errors = []

    def _is_internal(self, url: str) -> bool:
        """Check if URL belongs to the same domain."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        return domain == self.base_domain

    def _normalize_url(self, url: str) -> str:
        """Normalize a URL for deduplication."""
        parsed = urlparse(url)
        # Remove fragment
        normalized = parsed._replace(fragment="")
        # Remove trailing slash for consistency
        path = normalized.path.rstrip("/") if normalized.path != "/" else "/"
        normalized = normalized._replace(path=path)
        return normalized.geturl()

    def _is_crawlable(self, url: str) -> bool:
        """Filter out non-HTML resources."""
        skip_extensions = {
            ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
            ".css", ".js", ".ico", ".xml", ".json", ".zip", ".gz",
            ".mp3", ".mp4", ".avi", ".mov", ".woff", ".woff2", ".ttf",
            ".eot", ".otf",
        }
        parsed = urlparse(url)
        path_lower = parsed.path.lower()
        return not any(path_lower.endswith(ext) for ext in skip_extensions)

    def _try_sitemap(self):
        """Try to discover pages from sitemap.xml."""
        sitemap_urls = [
            f"{self.scheme}://{urlparse(self.start_url).netloc}/sitemap.xml",
            f"{self.scheme}://{urlparse(self.start_url).netloc}/sitemap_index.xml",
        ]
        discovered = set()

        for sitemap_url in sitemap_urls:
            try:
                resp = self.session.get(sitemap_url, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200 and "xml" in resp.headers.get("content-type", ""):
                    soup = BeautifulSoup(resp.text, "html.parser")

                    # Check for sitemap index
                    sitemaps = soup.find_all("sitemap")
                    if sitemaps:
                        for sm in sitemaps:
                            loc = sm.find("loc")
                            if loc and loc.text:
                                try:
                                    sub_resp = self.session.get(loc.text.strip(), timeout=REQUEST_TIMEOUT)
                                    if sub_resp.status_code == 200:
                                        sub_soup = BeautifulSoup(sub_resp.text, "html.parser")
                                        for url_tag in sub_soup.find_all("url"):
                                            loc_tag = url_tag.find("loc")
                                            if loc_tag and loc_tag.text:
                                                discovered.add(loc_tag.text.strip())
                                except Exception:
                                    pass

                    # Regular sitemap URLs
                    for url_tag in soup.find_all("url"):
                        loc = url_tag.find("loc")
                        if loc and loc.text:
                            discovered.add(loc.text.strip())
            except Exception:
                pass

        # Add discovered URLs to queue
        for url in discovered:
            normalized = self._normalize_url(url)
            if self._is_internal(normalized) and self._is_crawlable(normalized):
                if normalized not in self.visited:
                    self.queue.append(normalized)

        if discovered:
            print(f"  📄 Found {len(discovered)} URLs from sitemap")

    def _fetch_page(self, url: str):
        """Fetch and parse a single page."""
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type:
                return None
            return resp.text
        except Exception as e:
            self.errors.append({"url": url, "error": str(e)})
            return None

    def _extract_links(self, html: str, page_url: str):
        """Extract all links from HTML and categorize them."""
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else page_url

        internal_links = set()
        external_links = []
        seen_external = set()

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()

            # Skip anchors, mailto, tel, javascript
            if href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue

            # Resolve relative URLs
            full_url = urljoin(page_url, href)
            parsed = urlparse(full_url)

            # Only http/https
            if parsed.scheme not in ("http", "https"):
                continue

            if self._is_internal(full_url):
                normalized = self._normalize_url(full_url)
                if self._is_crawlable(normalized):
                    internal_links.add(normalized)
            else:
                # External link
                domain = parsed.netloc.lower().replace("www.", "")
                if full_url not in seen_external:
                    seen_external.add(full_url)
                    anchor_text = a_tag.get_text(strip=True) or "[no anchor text]"
                    external_links.append({
                        "url": full_url,
                        "anchor": anchor_text[:200],
                        "domain": domain,
                        "is_authority": is_authority_domain(domain),
                        "rel": a_tag.get("rel", []),
                    })

                    # Update domain summary
                    self.domain_summary[domain]["count"] += 1
                    self.domain_summary[domain]["pages"].add(page_url)

        return title, internal_links, external_links

    def crawl(self):
        """Main crawl loop."""
        print(f"\n{'='*60}")
        print(f"  External Link Auditor")
        print(f"  Target: {self.start_url}")
        print(f"  Max pages: {self.max_pages}")
        print(f"{'='*60}\n")

        # Try sitemap first
        print("🔍 Checking sitemap...")
        self._try_sitemap()
        print(f"📋 Queue size: {len(self.queue)} URLs\n")

        print("🕷️  Crawling pages...\n")

        while self.queue and len(self.visited) < self.max_pages:
            url = self.queue.pop(0)
            normalized = self._normalize_url(url)

            if normalized in self.visited:
                continue

            self.visited.add(normalized)

            # Progress
            count = len(self.visited)
            if count % 10 == 0 or count <= 5:
                print(f"  [{count}/{self.max_pages}] Crawling: {normalized[:80]}...")

            html = self._fetch_page(normalized)
            if html is None:
                continue

            title, internal_links, external_links = self._extract_links(html, normalized)

            self.pages_data[normalized] = {
                "title": title,
                "external_links": external_links,
                "external_count": len(external_links),
            }

            # Add newly discovered internal links to queue
            for link in internal_links:
                if link not in self.visited:
                    self.queue.append(link)

            time.sleep(self.delay)

        print(f"\n✅ Crawl complete!")
        print(f"   Pages crawled: {len(self.visited)}")
        print(f"   Pages with external links: {sum(1 for p in self.pages_data.values() if p['external_count'] > 0)}")
        total_ext = sum(p['external_count'] for p in self.pages_data.values())
        print(f"   Total external links found: {total_ext}")
        print(f"   Unique external domains: {len(self.domain_summary)}")
        if self.errors:
            print(f"   Errors: {len(self.errors)}")

    def generate_report(self, output_path: str):
        """Generate the interactive HTML report."""
        # Prepare data for the report
        pages_list = []
        for url, data in sorted(self.pages_data.items()):
            pages_list.append({
                "url": url,
                "title": data["title"],
                "external_count": data["external_count"],
                "external_links": data["external_links"],
            })

        domain_list = []
        for domain, info in sorted(self.domain_summary.items(), key=lambda x: -x[1]["count"]):
            domain_list.append({
                "domain": domain,
                "count": info["count"],
                "pages_count": len(info["pages"]),
                "is_authority": is_authority_domain(domain),
                "pages": list(info["pages"]),
            })

        report_data = {
            "site": self.start_url,
            "base_domain": self.base_domain,
            "crawl_date": datetime.now().isoformat(),
            "total_pages": len(self.pages_data),
            "total_external_links": sum(p["external_count"] for p in self.pages_data.values()),
            "total_domains": len(self.domain_summary),
            "pages": pages_list,
            "domains": domain_list,
            "errors": self.errors,
        }

        html = generate_html_report(report_data)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        print(f"\n📊 Report saved to: {output_path}")
        return output_path


def generate_html_report(data: dict) -> str:
    """Generate the full interactive HTML report."""
    report_json = json.dumps(data, default=str)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>External Link Audit — {data['base_domain']}</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #0a0e17;
  --surface: #111827;
  --surface-2: #1a2235;
  --surface-3: #243049;
  --border: #2a3654;
  --text: #e2e8f0;
  --text-muted: #8494b0;
  --accent: #38bdf8;
  --accent-dim: rgba(56, 189, 248, 0.12);
  --green: #34d399;
  --green-dim: rgba(52, 211, 153, 0.12);
  --red: #f87171;
  --red-dim: rgba(248, 113, 113, 0.12);
  --orange: #fb923c;
  --orange-dim: rgba(251, 146, 60, 0.12);
  --yellow: #fbbf24;
  --radius: 10px;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  font-family: 'DM Sans', -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  min-height: 100vh;
}}

.container {{
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px 32px;
}}

/* Header */
.header {{
  padding: 40px 0 32px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 32px;
}}
.header h1 {{
  font-size: 28px;
  font-weight: 700;
  margin-bottom: 6px;
  letter-spacing: -0.5px;
}}
.header h1 span {{ color: var(--accent); }}
.header .meta {{
  color: var(--text-muted);
  font-size: 14px;
}}

/* Stats */
.stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 32px;
}}
.stat-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px 24px;
}}
.stat-card .label {{
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--text-muted);
  margin-bottom: 6px;
}}
.stat-card .value {{
  font-size: 32px;
  font-weight: 700;
  font-family: 'JetBrains Mono', monospace;
}}
.stat-card .value.accent {{ color: var(--accent); }}
.stat-card .value.green {{ color: var(--green); }}
.stat-card .value.red {{ color: var(--red); }}
.stat-card .value.orange {{ color: var(--orange); }}

/* Tabs */
.tabs {{
  display: flex;
  gap: 4px;
  margin-bottom: 24px;
  background: var(--surface);
  border-radius: var(--radius);
  padding: 4px;
  border: 1px solid var(--border);
  width: fit-content;
}}
.tab {{
  padding: 10px 24px;
  font-size: 14px;
  font-weight: 500;
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: 7px;
  transition: all 0.2s;
}}
.tab:hover {{ color: var(--text); }}
.tab.active {{
  background: var(--accent);
  color: #0a0e17;
  font-weight: 600;
}}

/* Search & Filter */
.toolbar {{
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
  flex-wrap: wrap;
  align-items: center;
}}
.search-box {{
  flex: 1;
  min-width: 260px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 16px;
  color: var(--text);
  font-size: 14px;
  font-family: inherit;
  outline: none;
  transition: border-color 0.2s;
}}
.search-box:focus {{ border-color: var(--accent); }}
.search-box::placeholder {{ color: var(--text-muted); }}

.filter-btn {{
  padding: 10px 18px;
  font-size: 13px;
  font-weight: 500;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-muted);
  border-radius: var(--radius);
  cursor: pointer;
  transition: all 0.2s;
  font-family: inherit;
}}
.filter-btn:hover {{ border-color: var(--accent); color: var(--text); }}
.filter-btn.active {{
  background: var(--accent-dim);
  border-color: var(--accent);
  color: var(--accent);
}}

/* Tables */
.table-wrap {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}}
table {{
  width: 100%;
  border-collapse: collapse;
}}
th {{
  text-align: left;
  padding: 14px 20px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--text-muted);
  background: var(--surface-2);
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}}
th:hover {{ color: var(--accent); }}
th.sorted {{ color: var(--accent); }}
th .arrow {{ margin-left: 4px; font-size: 10px; }}
td {{
  padding: 12px 20px;
  font-size: 14px;
  border-bottom: 1px solid rgba(42, 54, 84, 0.5);
  vertical-align: top;
}}
tr:hover td {{ background: rgba(56, 189, 248, 0.03); }}
tr:last-child td {{ border-bottom: none; }}

.url-cell {{
  max-width: 400px;
  word-break: break-all;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--accent);
}}
.url-cell a {{
  color: inherit;
  text-decoration: none;
}}
.url-cell a:hover {{ text-decoration: underline; }}

.title-cell {{
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}

.badge {{
  display: inline-block;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 600;
  font-family: 'JetBrains Mono', monospace;
}}
.badge.authority {{
  background: var(--green-dim);
  color: var(--green);
}}
.badge.non-authority {{
  background: var(--orange-dim);
  color: var(--orange);
}}
.badge.count-high {{
  background: var(--red-dim);
  color: var(--red);
}}
.badge.count-med {{
  background: var(--orange-dim);
  color: var(--orange);
}}
.badge.count-low {{
  background: var(--green-dim);
  color: var(--green);
}}

/* Expandable rows */
.expand-btn {{
  background: none;
  border: 1px solid var(--border);
  color: var(--text-muted);
  width: 28px;
  height: 28px;
  border-radius: 6px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  transition: all 0.2s;
}}
.expand-btn:hover {{
  border-color: var(--accent);
  color: var(--accent);
}}
.expand-btn.open {{
  background: var(--accent-dim);
  border-color: var(--accent);
  color: var(--accent);
  transform: rotate(90deg);
}}
.detail-row {{ display: none; }}
.detail-row.open {{ display: table-row; }}
.detail-row td {{
  padding: 0;
  background: var(--surface-2);
}}
.detail-content {{
  padding: 16px 20px;
}}
.detail-content table {{ margin: 0; }}
.detail-content th {{
  background: var(--surface-3);
  font-size: 10px;
  padding: 10px 16px;
}}
.detail-content td {{
  padding: 8px 16px;
  font-size: 13px;
}}

/* Export */
.export-bar {{
  display: flex;
  gap: 8px;
  margin-bottom: 24px;
  justify-content: flex-end;
}}
.export-btn {{
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 500;
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: var(--radius);
  cursor: pointer;
  font-family: inherit;
  transition: all 0.2s;
}}
.export-btn:hover {{
  background: var(--accent);
  color: var(--bg);
  border-color: var(--accent);
}}

/* Panel hidden */
.panel {{ display: none; }}
.panel.active {{ display: block; }}

.no-results {{
  text-align: center;
  padding: 48px;
  color: var(--text-muted);
  font-size: 15px;
}}

.anchor-text {{
  color: var(--text-muted);
  font-size: 12px;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}

.rel-tag {{
  display: inline-block;
  padding: 2px 6px;
  font-size: 10px;
  background: var(--surface-3);
  border-radius: 4px;
  color: var(--text-muted);
  margin-right: 4px;
}}

/* Scrollbar */
::-webkit-scrollbar {{ width: 8px; height: 8px; }}
::-webkit-scrollbar-track {{ background: var(--surface); }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: var(--text-muted); }}

@media (max-width: 768px) {{
  .container {{ padding: 16px; }}
  .header h1 {{ font-size: 22px; }}
  .stats {{ grid-template-columns: repeat(2, 1fr); }}
  .toolbar {{ flex-direction: column; }}
  .search-box {{ min-width: unset; width: 100%; }}
}}
</style>
</head>
<body>

<div class="container">
  <div class="header">
    <h1>External Link Audit — <span id="siteName"></span></h1>
    <div class="meta" id="crawlMeta"></div>
  </div>

  <div class="stats" id="statsGrid"></div>

  <div class="tabs">
    <button class="tab active" data-panel="pages">Pages View</button>
    <button class="tab" data-panel="domains">Domains View</button>
    <button class="tab" data-panel="all-links">All External Links</button>
  </div>

  <!-- Pages Panel -->
  <div class="panel active" id="panel-pages">
    <div class="toolbar">
      <input type="text" class="search-box" id="pageSearch"
             placeholder="Search pages by URL or title...">
      <button class="filter-btn" id="filterHasExternal">Has External Links</button>
      <button class="filter-btn" id="filterHighCount">High Count (10+)</button>
    </div>
    <div class="export-bar">
      <button class="export-btn" onclick="exportCSV('pages')">Export CSV</button>
    </div>
    <div class="table-wrap">
      <table id="pagesTable">
        <thead>
          <tr>
            <th style="width:40px"></th>
            <th data-sort="url">Page URL <span class="arrow"></span></th>
            <th data-sort="title">Title <span class="arrow"></span></th>
            <th data-sort="count" class="sorted">External Links <span class="arrow">▼</span></th>
          </tr>
        </thead>
        <tbody id="pagesBody"></tbody>
      </table>
    </div>
  </div>

  <!-- Domains Panel -->
  <div class="panel" id="panel-domains">
    <div class="toolbar">
      <input type="text" class="search-box" id="domainSearch"
             placeholder="Search domains...">
      <button class="filter-btn active" id="filterNonAuth">Non-Authority Only</button>
      <button class="filter-btn" id="filterAuth">Authority Only</button>
    </div>
    <div class="export-bar">
      <button class="export-btn" onclick="exportCSV('domains')">Export CSV</button>
    </div>
    <div class="table-wrap">
      <table id="domainsTable">
        <thead>
          <tr>
            <th style="width:40px"></th>
            <th data-sort="domain">Domain <span class="arrow"></span></th>
            <th data-sort="count" class="sorted">Total Links <span class="arrow">▼</span></th>
            <th data-sort="pages">Found On Pages <span class="arrow"></span></th>
            <th data-sort="type">Type <span class="arrow"></span></th>
          </tr>
        </thead>
        <tbody id="domainsBody"></tbody>
      </table>
    </div>
  </div>

  <!-- All Links Panel -->
  <div class="panel" id="panel-all-links">
    <div class="toolbar">
      <input type="text" class="search-box" id="linkSearch"
             placeholder="Search external links by URL, domain, or anchor text...">
    </div>
    <div class="export-bar">
      <button class="export-btn" onclick="exportCSV('all-links')">Export CSV</button>
    </div>
    <div class="table-wrap">
      <table id="linksTable">
        <thead>
          <tr>
            <th data-sort="source">Source Page <span class="arrow"></span></th>
            <th data-sort="url">External URL <span class="arrow"></span></th>
            <th data-sort="domain">Domain <span class="arrow"></span></th>
            <th data-sort="anchor">Anchor Text <span class="arrow"></span></th>
            <th data-sort="type">Type <span class="arrow"></span></th>
          </tr>
        </thead>
        <tbody id="linksBody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const DATA = {report_json};

// ─── Initialize ───────────────────────────────────────────────────────────
document.getElementById('siteName').textContent = DATA.base_domain;
document.getElementById('crawlMeta').textContent =
  `Crawled on ${{new Date(DATA.crawl_date).toLocaleDateString('en-US', {{
    weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
  }})}} · ${{DATA.total_pages}} pages analyzed`;

// Stats
const nonAuthDomains = DATA.domains.filter(d => !d.is_authority).length;
const nonAuthLinks = DATA.domains.filter(d => !d.is_authority)
  .reduce((s, d) => s + d.count, 0);

document.getElementById('statsGrid').innerHTML = `
  <div class="stat-card">
    <div class="label">Pages Crawled</div>
    <div class="value accent">${{DATA.total_pages}}</div>
  </div>
  <div class="stat-card">
    <div class="label">Total External Links</div>
    <div class="value">${{DATA.total_external_links}}</div>
  </div>
  <div class="stat-card">
    <div class="label">Unique External Domains</div>
    <div class="value orange">${{DATA.total_domains}}</div>
  </div>
  <div class="stat-card">
    <div class="label">Non-Authority Domains</div>
    <div class="value red">${{nonAuthDomains}}</div>
  </div>
  <div class="stat-card">
    <div class="label">Authority Links</div>
    <div class="value green">${{DATA.total_external_links - nonAuthLinks}}</div>
  </div>
`;

// ─── Tabs ─────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {{
  tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('panel-' + tab.dataset.panel).classList.add('active');
  }});
}});

// ─── Pages Table ──────────────────────────────────────────────────────────
let pagesSort = {{ col: 'count', dir: 'desc' }};
let pageFilter = {{ search: '', hasExternal: false, highCount: false }};

function renderPages() {{
  let pages = [...DATA.pages];

  // Filter
  if (pageFilter.search) {{
    const q = pageFilter.search.toLowerCase();
    pages = pages.filter(p =>
      p.url.toLowerCase().includes(q) || p.title.toLowerCase().includes(q)
    );
  }}
  if (pageFilter.hasExternal) pages = pages.filter(p => p.external_count > 0);
  if (pageFilter.highCount) pages = pages.filter(p => p.external_count >= 10);

  // Sort
  pages.sort((a, b) => {{
    let va, vb;
    if (pagesSort.col === 'count') {{ va = a.external_count; vb = b.external_count; }}
    else if (pagesSort.col === 'title') {{ va = a.title; vb = b.title; }}
    else {{ va = a.url; vb = b.url; }}
    if (typeof va === 'string') {{
      return pagesSort.dir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
    }}
    return pagesSort.dir === 'asc' ? va - vb : vb - va;
  }});

  const tbody = document.getElementById('pagesBody');
  if (!pages.length) {{
    tbody.innerHTML = '<tr><td colspan="4" class="no-results">No pages match your filters</td></tr>';
    return;
  }}

  tbody.innerHTML = pages.map((p, i) => {{
    const countClass = p.external_count >= 10 ? 'count-high' :
                       p.external_count >= 5 ? 'count-med' : 'count-low';
    const details = p.external_links.map(l => `
      <tr>
        <td class="url-cell"><a href="${{l.url}}" target="_blank" rel="noopener">${{l.url}}</a></td>
        <td>${{l.domain}}</td>
        <td class="anchor-text" title="${{l.anchor}}">${{l.anchor}}</td>
        <td>${{l.is_authority
          ? '<span class="badge authority">Authority</span>'
          : '<span class="badge non-authority">Non-Authority</span>'}}</td>
        <td>${{(l.rel || []).map(r => `<span class="rel-tag">${{r}}</span>`).join('')}}</td>
      </tr>
    `).join('');

    return `
      <tr>
        <td><button class="expand-btn" data-idx="${{i}}" onclick="toggleDetail(this, 'page-detail-${{i}}')"
          ${{p.external_count === 0 ? 'disabled style="opacity:0.3"' : ''}}>▸</button></td>
        <td class="url-cell"><a href="${{p.url}}" target="_blank" rel="noopener">${{p.url}}</a></td>
        <td class="title-cell" title="${{p.title}}">${{p.title}}</td>
        <td><span class="badge ${{countClass}}">${{p.external_count}}</span></td>
      </tr>
      <tr class="detail-row" id="page-detail-${{i}}">
        <td colspan="4">
          <div class="detail-content">
            <table>
              <thead><tr>
                <th>External URL</th><th>Domain</th><th>Anchor Text</th><th>Type</th><th>Rel</th>
              </tr></thead>
              <tbody>${{details}}</tbody>
            </table>
          </div>
        </td>
      </tr>
    `;
  }}).join('');
}}

document.getElementById('pageSearch').addEventListener('input', e => {{
  pageFilter.search = e.target.value; renderPages();
}});
document.getElementById('filterHasExternal').addEventListener('click', function() {{
  this.classList.toggle('active');
  pageFilter.hasExternal = this.classList.contains('active');
  renderPages();
}});
document.getElementById('filterHighCount').addEventListener('click', function() {{
  this.classList.toggle('active');
  pageFilter.highCount = this.classList.contains('active');
  renderPages();
}});

// ─── Domains Table ────────────────────────────────────────────────────────
let domainsSort = {{ col: 'count', dir: 'desc' }};
let domainFilter = {{ search: '', nonAuth: true, auth: false }};

function renderDomains() {{
  let domains = [...DATA.domains];

  if (domainFilter.search) {{
    const q = domainFilter.search.toLowerCase();
    domains = domains.filter(d => d.domain.toLowerCase().includes(q));
  }}
  if (domainFilter.nonAuth && !domainFilter.auth) domains = domains.filter(d => !d.is_authority);
  if (domainFilter.auth && !domainFilter.nonAuth) domains = domains.filter(d => d.is_authority);

  domains.sort((a, b) => {{
    let va, vb;
    if (domainsSort.col === 'count') {{ va = a.count; vb = b.count; }}
    else if (domainsSort.col === 'pages') {{ va = a.pages_count; vb = b.pages_count; }}
    else if (domainsSort.col === 'type') {{ va = a.is_authority ? 1 : 0; vb = b.is_authority ? 1 : 0; }}
    else {{ va = a.domain; vb = b.domain; }}
    if (typeof va === 'string') return domainsSort.dir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
    return domainsSort.dir === 'asc' ? va - vb : vb - va;
  }});

  const tbody = document.getElementById('domainsBody');
  if (!domains.length) {{
    tbody.innerHTML = '<tr><td colspan="5" class="no-results">No domains match your filters</td></tr>';
    return;
  }}

  tbody.innerHTML = domains.map((d, i) => {{
    const pagesList = d.pages.map(p =>
      `<tr><td class="url-cell"><a href="${{p}}" target="_blank" rel="noopener">${{p}}</a></td></tr>`
    ).join('');

    return `
      <tr>
        <td><button class="expand-btn" onclick="toggleDetail(this, 'domain-detail-${{i}}')">▸</button></td>
        <td class="url-cell"><a href="https://${{d.domain}}" target="_blank" rel="noopener">${{d.domain}}</a></td>
        <td><span class="badge ${{d.count >= 10 ? 'count-high' : d.count >= 5 ? 'count-med' : 'count-low'}}">${{d.count}}</span></td>
        <td>${{d.pages_count}}</td>
        <td>${{d.is_authority
          ? '<span class="badge authority">Authority</span>'
          : '<span class="badge non-authority">Non-Authority</span>'}}</td>
      </tr>
      <tr class="detail-row" id="domain-detail-${{i}}">
        <td colspan="5">
          <div class="detail-content">
            <table>
              <thead><tr><th>Found On Page</th></tr></thead>
              <tbody>${{pagesList}}</tbody>
            </table>
          </div>
        </td>
      </tr>
    `;
  }}).join('');
}}

document.getElementById('domainSearch').addEventListener('input', e => {{
  domainFilter.search = e.target.value; renderDomains();
}});
document.getElementById('filterNonAuth').addEventListener('click', function() {{
  this.classList.toggle('active');
  domainFilter.nonAuth = this.classList.contains('active');
  renderDomains();
}});
document.getElementById('filterAuth').addEventListener('click', function() {{
  this.classList.toggle('active');
  domainFilter.auth = this.classList.contains('active');
  renderDomains();
}});

// ─── All Links Table ──────────────────────────────────────────────────────
function buildAllLinks() {{
  const links = [];
  DATA.pages.forEach(p => {{
    p.external_links.forEach(l => {{
      links.push({{ source: p.url, ...l }});
    }});
  }});
  return links;
}}

const ALL_LINKS = buildAllLinks();
let linksFilter = {{ search: '' }};

function renderAllLinks() {{
  let links = [...ALL_LINKS];
  if (linksFilter.search) {{
    const q = linksFilter.search.toLowerCase();
    links = links.filter(l =>
      l.url.toLowerCase().includes(q) ||
      l.domain.toLowerCase().includes(q) ||
      l.anchor.toLowerCase().includes(q) ||
      l.source.toLowerCase().includes(q)
    );
  }}

  // Limit for performance
  const limited = links.slice(0, 500);
  const tbody = document.getElementById('linksBody');

  if (!limited.length) {{
    tbody.innerHTML = '<tr><td colspan="5" class="no-results">No links match your search</td></tr>';
    return;
  }}

  tbody.innerHTML = limited.map(l => `
    <tr>
      <td class="url-cell" style="max-width:250px"><a href="${{l.source}}" target="_blank">${{l.source}}</a></td>
      <td class="url-cell"><a href="${{l.url}}" target="_blank">${{l.url}}</a></td>
      <td>${{l.domain}}</td>
      <td class="anchor-text" title="${{l.anchor}}">${{l.anchor}}</td>
      <td>${{l.is_authority
        ? '<span class="badge authority">Authority</span>'
        : '<span class="badge non-authority">Non-Authority</span>'}}</td>
    </tr>
  `).join('');

  if (links.length > 500) {{
    tbody.innerHTML += `<tr><td colspan="5" class="no-results">
      Showing 500 of ${{links.length}} links. Use search to narrow results.</td></tr>`;
  }}
}}

document.getElementById('linkSearch').addEventListener('input', e => {{
  linksFilter.search = e.target.value; renderAllLinks();
}});

// ─── Expand/Collapse ──────────────────────────────────────────────────────
function toggleDetail(btn, id) {{
  btn.classList.toggle('open');
  document.getElementById(id).classList.toggle('open');
}}

// ─── CSV Export ───────────────────────────────────────────────────────────
function exportCSV(type) {{
  let csv = '';
  if (type === 'pages') {{
    csv = 'Page URL,Title,External Link Count\\n';
    DATA.pages.forEach(p => {{
      csv += `"${{p.url}}","${{p.title.replace(/"/g, '""')}}",${{p.external_count}}\\n`;
    }});
  }} else if (type === 'domains') {{
    csv = 'Domain,Total Links,Found On Pages,Is Authority\\n';
    DATA.domains.forEach(d => {{
      csv += `"${{d.domain}}",${{d.count}},${{d.pages_count}},${{d.is_authority}}\\n`;
    }});
  }} else {{
    csv = 'Source Page,External URL,Domain,Anchor Text,Is Authority\\n';
    ALL_LINKS.forEach(l => {{
      csv += `"${{l.source}}","${{l.url}}","${{l.domain}}","${{l.anchor.replace(/"/g, '""')}}",${{l.is_authority}}\\n`;
    }});
  }}

  const blob = new Blob([csv], {{ type: 'text/csv' }});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `external-link-audit-${{type}}-${{DATA.base_domain}}.csv`;
  a.click();
}}

// ─── Table Sorting ────────────────────────────────────────────────────────
document.querySelectorAll('#pagesTable th[data-sort]').forEach(th => {{
  th.addEventListener('click', () => {{
    const col = th.dataset.sort;
    if (pagesSort.col === col) pagesSort.dir = pagesSort.dir === 'asc' ? 'desc' : 'asc';
    else {{ pagesSort.col = col; pagesSort.dir = 'desc'; }}
    document.querySelectorAll('#pagesTable th').forEach(h => {{
      h.classList.remove('sorted');
      h.querySelector('.arrow').textContent = '';
    }});
    th.classList.add('sorted');
    th.querySelector('.arrow').textContent = pagesSort.dir === 'asc' ? '▲' : '▼';
    renderPages();
  }});
}});

document.querySelectorAll('#domainsTable th[data-sort]').forEach(th => {{
  th.addEventListener('click', () => {{
    const col = th.dataset.sort;
    if (domainsSort.col === col) domainsSort.dir = domainsSort.dir === 'asc' ? 'desc' : 'asc';
    else {{ domainsSort.col = col; domainsSort.dir = 'desc'; }}
    document.querySelectorAll('#domainsTable th').forEach(h => {{
      h.classList.remove('sorted');
      h.querySelector('.arrow').textContent = '';
    }});
    th.classList.add('sorted');
    th.querySelector('.arrow').textContent = domainsSort.dir === 'asc' ? '▲' : '▼';
    renderDomains();
  }});
}});

// ─── Initial Render ──────────────────────────────────────────────────────
renderPages();
renderDomains();
renderAllLinks();
</script>
</body>
</html>"""


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="External Link Auditor — Crawl a site and audit all external links"
    )
    parser.add_argument("url", help="Website URL to audit (e.g., https://example.com)")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"Maximum pages to crawl (default: {DEFAULT_MAX_PAGES})")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Delay between requests in seconds (default: {DEFAULT_DELAY})")
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT,
                        help=f"Output HTML file path (default: {DEFAULT_OUTPUT})")

    args = parser.parse_args()

    auditor = ExternalLinkAuditor(args.url, args.max_pages, args.delay)
    auditor.crawl()
    auditor.generate_report(args.output)


if __name__ == "__main__":
    main()
