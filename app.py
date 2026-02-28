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
                 authority_domains: set, filter_mode: str = "All Pages",
                 filter_patterns: list = None, crawl_scope: str = "Exact Domain",
                 custom_domains: list = None):
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
        self.filter_mode = filter_mode
        self.filter_patterns = filter_patterns or []
        self.crawl_scope = crawl_scope
        self.custom_domains = [d.lower().replace("www.", "") for d in (custom_domains or [])]

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
        """Check if URL belongs to the crawl scope."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")

        if self.crawl_scope == "Exact Domain":
            # Only the exact domain (e.g., ovlg.com but NOT blog.ovlg.com)
            return domain == self.base_domain

        elif self.crawl_scope == "Include Subdomains":
            # Main domain + all subdomains (e.g., ovlg.com, blog.ovlg.com, app.ovlg.com)
            return domain == self.base_domain or domain.endswith("." + self.base_domain)

        elif self.crawl_scope == "Subdomain Only":
            # Only the exact subdomain entered (e.g., if user enters blog.ovlg.com,
            # only crawl blog.ovlg.com, not ovlg.com or app.ovlg.com)
            entered_domain = urlparse(self.start_url).netloc.lower().replace("www.", "")
            return domain == entered_domain

        elif self.crawl_scope == "Custom Domains":
            # User-specified list of domains to treat as internal
            all_domains = set(self.custom_domains) | {self.base_domain}
            for allowed in all_domains:
                if domain == allowed or domain.endswith("." + allowed):
                    return True
            return False

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

    def _matches_filter(self, url: str) -> bool:
        """Check if URL matches the filter criteria."""
        if self.filter_mode == "All Pages" or not self.filter_patterns:
            return True

        url_lower = url.lower()
        matches_any = any(pattern.lower() in url_lower for pattern in self.filter_patterns)

        if self.filter_mode == "Include Only":
            return matches_any
        elif self.filter_mode == "Exclude":
            return not matches_any
        return True

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

            # Only record page data if it matches the filter
            if self._matches_filter(normalized):
                self.pages_data[normalized] = {
                    "title": title,
                    "external_links": external_links,
                    "external_count": len(external_links),
                }

            # Always follow internal links for discovery regardless of filter
            for link in internal_links:
                if link not in self.visited:
                    self.queue.append(link)

            time.sleep(self.delay)

        total_ext = sum(p['external_count'] for p in self.pages_data.values())
        progress_bar.progress(1.0)
        progress_text.text("✅ Crawl complete!")

        filter_msg = ""
        if self.filter_mode != "All Pages" and self.filter_patterns:
            filter_msg = f" · Filter: {self.filter_mode} ({', '.join(self.filter_patterns[:3])})"

        status_text.success(
            f"Crawled **{len(self.visited)}** pages · "
            f"Audited **{len(self.pages_data)}** matching pages · "
            f"Found **{total_ext}** external links across "
            f"**{len(self.domain_summary)}** unique domains"
            f"{filter_msg}"
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

    audit_mode = st.radio(
        "What do you want to audit?",
        ["🌐 Entire Domain", "🔀 Subdomain", "📄 Exact URL"],
        help=(
            "**Entire Domain**: Crawl all pages on a domain (e.g., ovlg.com including all subdomains). "
            "**Subdomain**: Crawl only a specific subdomain (e.g., blog.ovlg.com). "
            "**Exact URL**: Audit a single page instantly."
        ),
    )

    # ── Exact URL Mode ─────────────────────────────────────────────────────
    if audit_mode == "📄 Exact URL":
        url_input = st.text_input(
            "Page URL",
            placeholder="https://www.ovlg.com/blog/some-long-article.html",
            help="Paste any page URL to instantly see all its external links"
        )

        st.markdown("---")
        st.markdown("**Authority Domains**")
        st.caption("Domains matching these patterns are flagged as 'Authority'")
        authority_text = st.text_area(
            "One per line",
            value="\n".join(DEFAULT_AUTHORITY_DOMAINS),
            height=200,
            label_visibility="collapsed",
            key="auth_exact",
        )
        authority_domains = set(
            line.strip() for line in authority_text.split("\n") if line.strip()
        )

        st.markdown("---")
        start_crawl = st.button("🔍 Audit This Page", use_container_width=True, type="primary")

        # Defaults for unused params
        crawl_scope = "Exact Domain"
        custom_domains = []
        max_pages = 1
        delay = 0.3
        filter_mode = "All Pages"
        filter_patterns = []

    # ── Subdomain Mode ─────────────────────────────────────────────────────
    elif audit_mode == "🔀 Subdomain":
        url_input = st.text_input(
            "Subdomain URL",
            placeholder="https://blog.ovlg.com",
            help="Enter the subdomain to crawl (e.g., blog.ovlg.com). Only pages on this exact subdomain will be audited."
        )

        st.markdown("---")
        col1, col2 = st.columns(2)
        with col1:
            max_pages = st.number_input("Max Pages", min_value=10, max_value=10000,
                                         value=1000, step=100, key="mp_sub")
        with col2:
            delay = st.number_input("Delay (sec)", min_value=0.1, max_value=5.0,
                                     value=0.3, step=0.1, key="dl_sub")

        st.markdown("---")
        st.markdown("**🎯 URL Path Filter**")
        st.caption("Optionally filter pages within this subdomain")

        filter_mode = st.radio(
            "Filter mode",
            ["All Pages", "Include Only", "Exclude"],
            horizontal=True,
            help="Include Only = audit ONLY matching URLs. Exclude = skip matching URLs.",
            key="fm_sub",
        )

        url_patterns = ""
        if filter_mode != "All Pages":
            url_patterns = st.text_area(
                "URL patterns (one per line)",
                placeholder="/blog/\n/category/",
                help="Example: /blog/ will match all URLs containing '/blog/' in the path.",
                height=100,
                key="up_sub",
            )
        filter_patterns = []
        if filter_mode != "All Pages" and url_patterns:
            filter_patterns = [p.strip() for p in url_patterns.strip().split("\n") if p.strip()]

        st.markdown("---")
        st.markdown("**Authority Domains**")
        st.caption("Domains matching these patterns are flagged as 'Authority'")
        authority_text = st.text_area(
            "One per line",
            value="\n".join(DEFAULT_AUTHORITY_DOMAINS),
            height=200,
            label_visibility="collapsed",
            key="auth_sub",
        )
        authority_domains = set(
            line.strip() for line in authority_text.split("\n") if line.strip()
        )

        st.markdown("---")
        start_crawl = st.button("🚀 Start Audit", use_container_width=True, type="primary", key="btn_sub")

        crawl_scope = "Subdomain Only"
        custom_domains = []

    # ── Entire Domain Mode ─────────────────────────────────────────────────
    else:
        url_input = st.text_input(
            "Domain",
            placeholder="https://www.ovlg.com",
            help="Enter the domain to crawl. All pages on this domain and its subdomains will be audited."
        )

        st.markdown("---")

        include_subdomains = st.checkbox(
            "Include subdomains (e.g., blog.ovlg.com, app.ovlg.com)",
            value=True,
            help="When checked, pages on subdomains are also crawled and audited."
        )

        st.markdown("---")
        col1, col2 = st.columns(2)
        with col1:
            max_pages = st.number_input("Max Pages", min_value=10, max_value=10000,
                                         value=1000, step=100, key="mp_full")
        with col2:
            delay = st.number_input("Delay (sec)", min_value=0.1, max_value=5.0,
                                     value=0.3, step=0.1, key="dl_full")

        st.markdown("---")
        st.markdown("**🎯 URL Path Filter**")
        st.caption("Optionally limit which pages get audited")

        filter_mode = st.radio(
            "Filter mode",
            ["All Pages", "Include Only", "Exclude"],
            horizontal=True,
            help="Include Only = audit ONLY matching URLs. Exclude = skip matching URLs.",
            key="fm_full",
        )

        url_patterns = ""
        if filter_mode != "All Pages":
            url_patterns = st.text_area(
                "URL patterns (one per line)",
                placeholder="/blog/\n/forum/\n/resources/",
                help="Example: /blog/ will match all URLs containing '/blog/' in the path.",
                height=100,
                key="up_full",
            )

        # Quick filters
        if filter_mode != "All Pages":
            st.caption("Quick filters:")
            qcol1, qcol2 = st.columns(2)
            with qcol1:
                if st.button("📝 Blog", use_container_width=True, key="qf_blog"):
                    url_patterns = "/blog/"
                if st.button("💬 Forum", use_container_width=True, key="qf_forum"):
                    url_patterns = "/forum/\n/community/\n/discuss/"
            with qcol2:
                if st.button("📄 Services", use_container_width=True, key="qf_services"):
                    url_patterns = "/debt-settlement/\n/bankruptcy/\n/debt-consolidation/"
                if st.button("❓ Q&A", use_container_width=True, key="qf_qa"):
                    url_patterns = "/questions/\n/answers/\n/ask/"

        filter_patterns = []
        if filter_mode != "All Pages" and url_patterns:
            filter_patterns = [p.strip() for p in url_patterns.strip().split("\n") if p.strip()]

        st.markdown("---")
        st.markdown("**Authority Domains**")
        st.caption("Domains matching these patterns are flagged as 'Authority'")
        authority_text = st.text_area(
            "One per line",
            value="\n".join(DEFAULT_AUTHORITY_DOMAINS),
            height=200,
            label_visibility="collapsed",
            key="auth_full",
        )
        authority_domains = set(
            line.strip() for line in authority_text.split("\n") if line.strip()
        )

        st.markdown("---")
        start_crawl = st.button("🚀 Start Audit", use_container_width=True, type="primary", key="btn_full")

        crawl_scope = "Include Subdomains" if include_subdomains else "Exact Domain"
        custom_domains = []


# ─── Main Area ────────────────────────────────────────────────────────────────

if "results" not in st.session_state:
    st.session_state.results = None
if "single_page_result" not in st.session_state:
    st.session_state.single_page_result = None

if start_crawl and url_input:
    # Clean up URL
    url = url_input.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    st.session_state.results = None
    st.session_state.single_page_result = None

    if audit_mode == "📄 Exact URL":
        # ── Single Page Audit ──────────────────────────────────────────────
        with st.spinner(f"Fetching {url}..."):
            try:
                session = requests.Session()
                session.headers.update({
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                })
                resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                content_type = resp.headers.get("content-type", "")

                if "text/html" not in content_type:
                    st.error("This URL did not return an HTML page.")
                else:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    title_tag = soup.find("title")
                    title = title_tag.get_text(strip=True) if title_tag else url

                    external_links = []
                    seen = set()
                    page_domain = urlparse(url).netloc.lower().replace("www.", "")

                    for a_tag in soup.find_all("a", href=True):
                        href = a_tag["href"].strip()
                        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
                            continue
                        full_url = urljoin(url, href)
                        parsed = urlparse(full_url)
                        if parsed.scheme not in ("http", "https"):
                            continue
                        link_domain = parsed.netloc.lower().replace("www.", "")
                        if link_domain != page_domain and full_url not in seen:
                            seen.add(full_url)
                            anchor_text = a_tag.get_text(strip=True) or "[no anchor text]"
                            rel_attrs = a_tag.get("rel", [])
                            external_links.append({
                                "url": full_url,
                                "domain": link_domain,
                                "anchor": anchor_text[:200],
                                "is_authority": is_authority_domain(link_domain, authority_domains),
                                "rel": ", ".join(rel_attrs) if rel_attrs else "",
                            })

                    st.session_state.single_page_result = {
                        "url": url,
                        "title": title,
                        "external_links": external_links,
                        "total": len(external_links),
                    }
                    st.rerun()

            except Exception as e:
                st.error(f"Failed to fetch page: {e}")

    else:
        # ── Full Site Crawl ────────────────────────────────────────────────
        progress_bar = st.progress(0)
        progress_text = st.empty()
        status_text = st.empty()

        auditor = ExternalLinkAuditor(url, max_pages, delay, authority_domains,
                                          filter_mode, filter_patterns, crawl_scope,
                                          custom_domains)
        auditor.crawl(progress_bar, progress_text, status_text)
        st.session_state.results = auditor.get_results()
        st.rerun()

elif start_crawl and not url_input:
    st.warning("Please enter a URL in the sidebar.")


# ─── Single Page Results ──────────────────────────────────────────────────────

if st.session_state.get("single_page_result"):
    data = st.session_state.single_page_result
    auth_count = sum(1 for l in data["external_links"] if l["is_authority"])
    non_auth_count = data["total"] - auth_count

    st.markdown(f"### 📄 Single Page Audit")
    st.markdown(f"**{data['title']}**")
    st.caption(f"{data['url']}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Total External Links", data["total"])
    c2.metric("Non-Authority", non_auth_count)
    c3.metric("Authority", auth_count)

    st.markdown("---")

    # Search & filter
    search_single = st.text_input("Search links", placeholder="Filter by URL, domain, or anchor text...",
                                   key="search_single")
    scol1, scol2 = st.columns(2)
    with scol1:
        type_filter = st.radio("Show", ["All", "Non-Authority Only", "Authority Only"],
                                horizontal=True, key="single_type_filter")

    links = data["external_links"]
    if search_single:
        q = search_single.lower()
        links = [l for l in links if q in l["url"].lower() or q in l["domain"].lower()
                 or q in l["anchor"].lower()]
    if type_filter == "Non-Authority Only":
        links = [l for l in links if not l["is_authority"]]
    elif type_filter == "Authority Only":
        links = [l for l in links if l["is_authority"]]

    # CSV download
    csv_rows = [(l["url"], l["domain"], l["anchor"],
                  "Authority" if l["is_authority"] else "Non-Authority", l["rel"])
                 for l in links]
    csv_str, fname = make_csv_download(csv_rows,
        ["External URL", "Domain", "Anchor Text", "Type", "Rel"],
        f"single-page-audit.csv")
    st.download_button("📥 Download CSV", csv_str, fname, "text/csv", key="dl_single")

    st.caption(f"Showing {len(links)} links")

    if links:
        import pandas as pd
        df = pd.DataFrame(links)
        df = df[["url", "domain", "anchor", "is_authority", "rel"]]
        df.columns = ["External URL", "Domain", "Anchor Text", "Authority", "Rel"]
        df["Authority"] = df["Authority"].map({True: "✅ Authority", False: "⚠️ Non-Authority"})
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No external links found matching your filters.")


# ─── Display Results ──────────────────────────────────────────────────────────

if st.session_state.get("single_page_result"):
    pass  # Already displayed above

elif st.session_state.results:
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

    Audit any website's external links — find unnecessary outbound links
    and identify opportunities to replace them with authoritative (.gov, .edu) sources.

    ### Three ways to audit

    - **🌐 Entire Domain** — Crawl all pages on a domain (with or without subdomains)
    - **🔀 Subdomain** — Crawl only a specific subdomain (e.g., `blog.ovlg.com`)
    - **📄 Exact URL** — Paste any single page URL and instantly see all its external links

    ### What you get

    - Every external link on every page, with domain, anchor text, and rel attributes
    - Links classified as **Authority** (.gov, .edu, CFPB, FTC, etc.) or **Non-Authority**
    - Search, filter, sort across all results
    - **CSV export** for your content team

    ---
    *Select your audit type and enter a URL in the sidebar to get started.*
    """)
