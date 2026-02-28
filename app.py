"""
External Link Auditor — Streamlit Web App
==========================================
A web-based tool to crawl any website and audit all external links.
Deploy on Streamlit Cloud, your own server, or run locally.

Usage:
    streamlit run app.py
"""

import json
import re
import time
import streamlit as st
import warnings
from collections import defaultdict
from datetime import datetime
from urllib.parse import urljoin, urlparse
from io import StringIO
import csv
import base64

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# ─── Page Config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="External Link Auditor",
    page_icon="🔗",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Constants ────────────────────────────────────────────────────────────────

REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (compatible; ExternalLinkAuditor/1.0; "
    "+https://github.com/external-link-auditor)"
)

DEFAULT_AUTHORITY_DOMAINS = [
    ".gov", ".edu", ".mil",
    "who.int", "irs.gov", "consumerfinance.gov", "ftc.gov",
    "sec.gov", "federalreserve.gov", "treasury.gov",
    "ncua.gov", "fdic.gov", "cfpb.gov",
    "cdc.gov", "nih.gov", "fda.gov",
    "wikipedia.org", "britannica.com",
    "reuters.com", "apnews.com",
]


def is_authority_domain(domain: str, authority_set: set) -> bool:
    domain = domain.lower()
    for auth in authority_set:
        if domain.endswith(auth):
            return True
    return False


# ─── Crawler ──────────────────────────────────────────────────────────────────

class ExternalLinkAuditor:
    def __init__(self, start_url: str, max_pages: int, delay: float,
                 authority_domains: set):
        parsed = urlparse(start_url)
        if not parsed.scheme:
            start_url = "https://" + start_url
            parsed = urlparse(start_url)

        self.start_url = start_url
        self.base_domain = parsed.netloc.lower().replace("www.", "")
        self.scheme = parsed.scheme
        self.max_pages = max_pages
        self.delay = delay
        self.authority_domains = authority_domains

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        })

        self.visited = set()
        self.queue = [start_url]
        self.pages_data = {}
        self.domain_summary = defaultdict(lambda: {"count": 0, "pages": set()})
        self.errors = []

    def _is_internal(self, url: str) -> bool:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        return domain == self.base_domain

    def _normalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        normalized = parsed._replace(fragment="")
        path = normalized.path.rstrip("/") if normalized.path != "/" else "/"
        normalized = normalized._replace(path=path)
        return normalized.geturl()

    def _is_crawlable(self, url: str) -> bool:
        skip_extensions = {
            ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
            ".css", ".js", ".ico", ".xml", ".json", ".zip", ".gz",
            ".mp3", ".mp4", ".avi", ".mov", ".woff", ".woff2", ".ttf",
            ".eot", ".otf",
        }
        parsed = urlparse(url)
        path_lower = parsed.path.lower()
        return not any(path_lower.endswith(ext) for ext in skip_extensions)

    def _try_sitemap(self, progress_text):
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

                    for url_tag in soup.find_all("url"):
                        loc = url_tag.find("loc")
                        if loc and loc.text:
                            discovered.add(loc.text.strip())
            except Exception:
                pass

        for url in discovered:
            normalized = self._normalize_url(url)
            if self._is_internal(normalized) and self._is_crawlable(normalized):
                if normalized not in self.visited:
                    self.queue.append(normalized)

        if discovered:
            progress_text.text(f"📄 Found {len(discovered)} URLs from sitemap")

        return len(discovered)

    def _fetch_page(self, url: str):
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
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else page_url

        internal_links = set()
        external_links = []
        seen_external = set()

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue

            full_url = urljoin(page_url, href)
            parsed = urlparse(full_url)

            if parsed.scheme not in ("http", "https"):
                continue

            if self._is_internal(full_url):
                normalized = self._normalize_url(full_url)
                if self._is_crawlable(normalized):
                    internal_links.add(normalized)
            else:
                domain = parsed.netloc.lower().replace("www.", "")
                if full_url not in seen_external:
                    seen_external.add(full_url)
                    anchor_text = a_tag.get_text(strip=True) or "[no anchor text]"
                    is_auth = is_authority_domain(domain, self.authority_domains)
                    rel_attrs = a_tag.get("rel", [])

                    external_links.append({
                        "url": full_url,
                        "anchor": anchor_text[:200],
                        "domain": domain,
                        "is_authority": is_auth,
                        "rel": ", ".join(rel_attrs) if rel_attrs else "",
                    })

                    self.domain_summary[domain]["count"] += 1
                    self.domain_summary[domain]["pages"].add(page_url)

        return title, internal_links, external_links

    def crawl(self, progress_bar, progress_text, status_text):
        progress_text.text("🔍 Checking sitemap...")
        sitemap_count = self._try_sitemap(progress_text)

        progress_text.text(f"🕷️ Starting crawl (queue: {len(self.queue)} URLs)...")

        while self.queue and len(self.visited) < self.max_pages:
            url = self.queue.pop(0)
            normalized = self._normalize_url(url)

            if normalized in self.visited:
                continue

            self.visited.add(normalized)
            count = len(self.visited)

            # Update progress
            pct = min(count / self.max_pages, 1.0)
            progress_bar.progress(pct)
            progress_text.text(
                f"Crawling [{count}/{self.max_pages}]: {normalized[:80]}..."
            )

            html = self._fetch_page(normalized)
            if html is None:
                continue

            title, internal_links, external_links = self._extract_links(html, normalized)

            self.pages_data[normalized] = {
                "title": title,
                "external_links": external_links,
                "external_count": len(external_links),
            }

            for link in internal_links:
                if link not in self.visited:
                    self.queue.append(link)

            time.sleep(self.delay)

        total_ext = sum(p['external_count'] for p in self.pages_data.values())
        progress_bar.progress(1.0)
        progress_text.text("✅ Crawl complete!")
        status_text.success(
            f"Crawled **{len(self.visited)}** pages · "
            f"Found **{total_ext}** external links across "
            f"**{len(self.domain_summary)}** unique domains"
        )

    def get_results(self):
        pages_list = []
        for url, data in sorted(self.pages_data.items(),
                                 key=lambda x: -x[1]["external_count"]):
            pages_list.append({
                "url": url,
                "title": data["title"],
                "external_count": data["external_count"],
                "external_links": data["external_links"],
            })

        domain_list = []
        for domain, info in sorted(self.domain_summary.items(),
                                     key=lambda x: -x[1]["count"]):
            domain_list.append({
                "domain": domain,
                "count": info["count"],
                "pages_count": len(info["pages"]),
                "is_authority": is_authority_domain(domain, self.authority_domains),
                "pages": sorted(info["pages"]),
            })

        return {
            "site": self.start_url,
            "base_domain": self.base_domain,
            "crawl_date": datetime.now().isoformat(),
            "total_pages": len(self.pages_data),
            "total_external_links": sum(
                p["external_count"] for p in self.pages_data.values()
            ),
            "total_domains": len(self.domain_summary),
            "pages": pages_list,
            "domains": domain_list,
            "errors": self.errors,
        }


# ─── Helper: CSV Download ────────────────────────────────────────────────────

def make_csv_download(data_rows, columns, filename):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in data_rows:
        writer.writerow(row)
    csv_string = output.getvalue()
    return csv_string, filename


# ─── Custom CSS ───────────────────────────────────────────────────────────────

st.markdown("""
<style>
    .block-container { max-width: 1200px; padding-top: 2rem; }
    .stMetric { background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 10px; padding: 16px; }
    [data-testid="stMetricValue"] { font-family: 'JetBrains Mono', monospace; }
    .authority-badge {
        background: rgba(52, 211, 153, 0.15);
        color: #34d399;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 12px;
        font-weight: 600;
    }
    .non-authority-badge {
        background: rgba(251, 146, 60, 0.15);
        color: #fb923c;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 12px;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# ─── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🔗 External Link Auditor")
    st.markdown("---")

    url_input = st.text_input(
        "Website URL",
        placeholder="https://www.ovlg.com",
        help="Enter the full URL of the website to audit"
    )

    col1, col2 = st.columns(2)
    with col1:
        max_pages = st.number_input("Max Pages", min_value=10, max_value=10000,
                                value=1000, step=100)
    with col2:
        delay = st.number_input("Delay (sec)", min_value=0.1, max_value=5.0,
                                 value=0.3, step=0.1)

    st.markdown("---")
    st.markdown("**Authority Domains**")
    st.caption("Domains matching these patterns are flagged as 'Authority'")
    authority_text = st.text_area(
        "One per line",
        value="\n".join(DEFAULT_AUTHORITY_DOMAINS),
        height=200,
        label_visibility="collapsed",
    )
    authority_domains = set(
        line.strip() for line in authority_text.split("\n") if line.strip()
    )

    st.markdown("---")
    start_crawl = st.button("🚀 Start Audit", use_container_width=True, type="primary")


# ─── Main Area ────────────────────────────────────────────────────────────────

if "results" not in st.session_state:
    st.session_state.results = None

if start_crawl and url_input:
    # Clean up URL
    url = url_input.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    st.session_state.results = None

    progress_bar = st.progress(0)
    progress_text = st.empty()
    status_text = st.empty()

    auditor = ExternalLinkAuditor(url, max_pages, delay, authority_domains)
    auditor.crawl(progress_bar, progress_text, status_text)
    st.session_state.results = auditor.get_results()
    st.rerun()

elif start_crawl and not url_input:
    st.warning("Please enter a website URL in the sidebar.")


# ─── Display Results ──────────────────────────────────────────────────────────

if st.session_state.results:
    data = st.session_state.results

    # Stats row
    non_auth_domains = [d for d in data["domains"] if not d["is_authority"]]
    auth_domains = [d for d in data["domains"] if d["is_authority"]]
    non_auth_link_count = sum(d["count"] for d in non_auth_domains)

    st.markdown(f"### Audit Results: `{data['base_domain']}`")
    st.caption(f"Crawled on {datetime.fromisoformat(data['crawl_date']).strftime('%B %d, %Y at %I:%M %p')}")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Pages Crawled", data["total_pages"])
    c2.metric("External Links", data["total_external_links"])
    c3.metric("Unique Domains", data["total_domains"])
    c4.metric("Non-Authority", len(non_auth_domains))
    c5.metric("Authority", len(auth_domains))

    st.markdown("---")

    # Tabs
    tab_pages, tab_domains, tab_all, tab_errors = st.tabs([
        f"📄 Pages ({data['total_pages']})",
        f"🌐 Domains ({data['total_domains']})",
        f"🔗 All Links ({data['total_external_links']})",
        f"⚠️ Errors ({len(data['errors'])})",
    ])

    # ── Pages Tab ──────────────────────────────────────────────────────────
    with tab_pages:
        search_pages = st.text_input("Search pages", placeholder="Filter by URL or title...",
                                      key="search_pages")
        fcol1, fcol2 = st.columns([1, 4])
        with fcol1:
            min_links = st.number_input("Min external links", min_value=0, value=1)

        filtered_pages = data["pages"]
        if search_pages:
            q = search_pages.lower()
            filtered_pages = [p for p in filtered_pages
                              if q in p["url"].lower() or q in p["title"].lower()]
        filtered_pages = [p for p in filtered_pages if p["external_count"] >= min_links]

        # CSV download
        csv_rows = [(p["url"], p["title"], p["external_count"]) for p in filtered_pages]
        csv_str, fname = make_csv_download(csv_rows,
            ["Page URL", "Title", "External Link Count"],
            f"pages-{data['base_domain']}.csv")
        st.download_button("📥 Download CSV", csv_str, fname, "text/csv", key="dl_pages")

        st.caption(f"Showing {len(filtered_pages)} pages")

        for page in filtered_pages:
            count_label = f"🔴 {page['external_count']}" if page['external_count'] >= 10 else \
                          f"🟡 {page['external_count']}" if page['external_count'] >= 5 else \
                          f"🟢 {page['external_count']}"

            with st.expander(f"{count_label}  —  {page['url']}", expanded=False):
                st.caption(f"**Title:** {page['title']}")
                if page["external_links"]:
                    import pandas as pd
                    df = pd.DataFrame(page["external_links"])
                    df = df[["url", "domain", "anchor", "is_authority", "rel"]]
                    df.columns = ["External URL", "Domain", "Anchor Text", "Authority", "Rel"]
                    df["Authority"] = df["Authority"].map({True: "✅ Yes", False: "❌ No"})
                    st.dataframe(df, use_container_width=True, hide_index=True)
                else:
                    st.info("No external links on this page.")

    # ── Domains Tab ────────────────────────────────────────────────────────
    with tab_domains:
        search_domains = st.text_input("Search domains", placeholder="Filter by domain...",
                                        key="search_domains")
        dcol1, dcol2 = st.columns(2)
        with dcol1:
            domain_type_filter = st.radio(
                "Filter by type",
                ["All", "Non-Authority Only", "Authority Only"],
                horizontal=True, index=1,
            )

        filtered_domains = data["domains"]
        if search_domains:
            q = search_domains.lower()
            filtered_domains = [d for d in filtered_domains if q in d["domain"].lower()]
        if domain_type_filter == "Non-Authority Only":
            filtered_domains = [d for d in filtered_domains if not d["is_authority"]]
        elif domain_type_filter == "Authority Only":
            filtered_domains = [d for d in filtered_domains if d["is_authority"]]

        csv_rows = [(d["domain"], d["count"], d["pages_count"],
                      "Authority" if d["is_authority"] else "Non-Authority")
                     for d in filtered_domains]
        csv_str, fname = make_csv_download(csv_rows,
            ["Domain", "Total Links", "Found On Pages", "Type"],
            f"domains-{data['base_domain']}.csv")
        st.download_button("📥 Download CSV", csv_str, fname, "text/csv", key="dl_domains")

        st.caption(f"Showing {len(filtered_domains)} domains")

        for dom in filtered_domains:
            badge = "✅" if dom["is_authority"] else "⚠️"
            with st.expander(
                f"{badge} **{dom['domain']}** — {dom['count']} links on {dom['pages_count']} pages",
                expanded=False
            ):
                st.caption("Found on these pages:")
                for page_url in dom["pages"]:
                    st.markdown(f"- [{page_url}]({page_url})")

    # ── All Links Tab ──────────────────────────────────────────────────────
    with tab_all:
        search_links = st.text_input("Search links", placeholder="Filter by URL, domain, or anchor...",
                                      key="search_links")

        all_links = []
        for page in data["pages"]:
            for link in page["external_links"]:
                all_links.append({
                    "Source Page": page["url"],
                    "External URL": link["url"],
                    "Domain": link["domain"],
                    "Anchor Text": link["anchor"],
                    "Authority": "✅ Yes" if link["is_authority"] else "❌ No",
                    "Rel": link["rel"],
                })

        if search_links:
            q = search_links.lower()
            all_links = [l for l in all_links
                         if q in l["External URL"].lower()
                         or q in l["Domain"].lower()
                         or q in l["Anchor Text"].lower()
                         or q in l["Source Page"].lower()]

        csv_rows = [(l["Source Page"], l["External URL"], l["Domain"],
                      l["Anchor Text"], l["Authority"], l["Rel"]) for l in all_links]
        csv_str, fname = make_csv_download(csv_rows,
            ["Source Page", "External URL", "Domain", "Anchor Text", "Authority", "Rel"],
            f"all-links-{data['base_domain']}.csv")
        st.download_button("📥 Download CSV", csv_str, fname, "text/csv", key="dl_links")

        st.caption(f"Showing {min(len(all_links), 500)} of {len(all_links)} links")

        if all_links:
            import pandas as pd
            df = pd.DataFrame(all_links[:500])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No links match your search.")

    # ── Errors Tab ─────────────────────────────────────────────────────────
    with tab_errors:
        if data["errors"]:
            import pandas as pd
            df = pd.DataFrame(data["errors"])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.success("No errors during crawl!")

else:
    # Landing page
    st.markdown("""
    # 🔗 External Link Auditor

    Crawl your website and audit every external link — find unnecessary outbound links
    and identify opportunities to replace them with authoritative (.gov, .edu) sources.

    ### How it works

    1. Enter your domain in the sidebar
    2. Click **Start Audit**
    3. The tool crawls your site via sitemap + internal links
    4. Review results across three views: Pages, Domains, All Links
    5. Export CSV for your content team

    ### Views

    - **Pages View** — Every page ranked by external link count. Expand to see details.
    - **Domains View** — All external domains ranked by frequency. Filter non-authority domains.
    - **All Links View** — Flat searchable list of every external link.

    ---
    *Configure max pages, crawl delay, and authority domains in the sidebar.*
    """)
