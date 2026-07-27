from __future__ import annotations

import gzip
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Browser, BrowserContext, Page, Playwright

from .models import FetchResult, SiteConfig, UrlItem
from .rules import host_of, path_of


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/xml,text/xml,text/plain,text/html,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,fr;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}

CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "challenge-platform",
    "cf-chl-",
    "verify you are human",
)


def clean_error(value: object, limit: int = 1200) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def document_type(text: str) -> str:
    value = (text or "").lstrip("\ufeff \r\n\t")
    if re.search(r"<(?:[\w.-]+:)?sitemapindex[\s>]", value, re.I):
        return "sitemapindex"
    if re.search(r"<(?:[\w.-]+:)?urlset[\s>]", value, re.I):
        return "urlset"
    if re.search(r"<!doctype\s+html|<html[\s>]", value[:3000], re.I):
        return "html"
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if lines and any(re.match(r"^https?://", line, re.I) for line in lines):
        return "txt"
    return "unknown"


def is_challenge(status: Optional[int], text: str) -> bool:
    lower = (text or "").lower()
    return (
        status in {403, 429, 503}
        or any(marker in lower for marker in CHALLENGE_MARKERS)
    ) and any(marker in lower for marker in CHALLENGE_MARKERS)


def decode_body(body: bytes, content_type: str, url: str) -> str:
    if body[:2] == b"\x1f\x8b" or url.lower().endswith(".gz"):
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            pass

    charset_match = re.search(
        r"charset\s*=\s*[\"']?([^;\"'\s]+)", content_type or "", re.I
    )
    encodings: list[str] = []
    if charset_match:
        encodings.append(charset_match.group(1).strip())
    encodings.extend(["utf-8-sig", "utf-8", "latin-1"])

    for encoding in encodings:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


class BrowserFetcher:
    def __init__(
        self,
        playwright: Playwright,
        *,
        headless: bool = False,
        request_timeout: int = 45,
        browser_timeout: int = 120,
        challenge_wait: int = 35,
    ):
        self.playwright = playwright
        self.headless = headless
        self.request_timeout = request_timeout
        self.browser_timeout = browser_timeout
        self.challenge_wait = challenge_wait
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def close(self) -> None:
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()

    def _ensure_browser(self) -> None:
        if self.context is not None:
            return
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context(
            locale="es-ES",
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )
        self.page = self.context.new_page()
        self.page.set_default_navigation_timeout(self.browser_timeout * 1000)

    def fetch_requests(self, url: str) -> FetchResult:
        try:
            response = self.session.get(
                url,
                timeout=self.request_timeout,
                allow_redirects=True,
            )
            content_type = response.headers.get("Content-Type", "")
            text = decode_body(response.content, content_type, url)
            return FetchResult(
                url=url,
                status=response.status_code,
                content_type=content_type,
                text=text,
                method="requests",
            )
        except requests.RequestException as exc:
            return FetchResult(
                url=url,
                status=None,
                content_type="",
                text="",
                method="requests",
                error=clean_error(exc),
            )

    def fetch_browser(self, url: str) -> FetchResult:
        self._ensure_browser()
        assert self.page is not None

        first = self._navigate(url)
        if self._usable(first):
            self._copy_cookies()
            return first
        if not is_challenge(first.status, first.text):
            return first

        deadline = time.time() + self.challenge_wait
        while time.time() < deadline:
            try:
                title = self.page.title()
                html = self.page.content()
            except Exception:
                title, html = "", ""
            combined = f"{title}\n{html}".lower()
            if not any(marker in combined for marker in CHALLENGE_MARKERS):
                self.page.wait_for_timeout(1500)
                break
            self.page.wait_for_timeout(1000)

        second = self._navigate(url)
        if self._usable(second):
            self._copy_cookies()
        return second

    def _navigate(self, url: str) -> FetchResult:
        assert self.page is not None
        try:
            response = self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            return FetchResult(
                url=url,
                status=None,
                content_type="",
                text="",
                method="playwright-browser-response",
                error=f"Navigation failed: {clean_error(exc)}",
            )
        if response is None:
            try:
                visible = self.page.content()
            except Exception as exc:
                visible = ""
                error = f"No response; page content failed: {clean_error(exc)}"
            else:
                error = "Browser returned no Response object"
            return FetchResult(
                url=url,
                status=None,
                content_type="text/html",
                text=visible,
                method="playwright-browser-response",
                error=error,
            )
        try:
            content_type = response.headers.get("content-type", "")
            body = response.body()
            text = decode_body(body, content_type, url)
        except Exception as exc:
            return FetchResult(
                url=url,
                status=response.status,
                content_type=response.headers.get("content-type", ""),
                text="",
                method="playwright-browser-response",
                error=f"Could not read browser response: {clean_error(exc)}",
            )
        return FetchResult(
            url=url,
            status=response.status,
            content_type=content_type,
            text=text,
            method="playwright-browser-response",
        )

    def _copy_cookies(self) -> None:
        if self.context is None:
            return
        try:
            cookies = self.context.cookies()
        except Exception:
            return
        for cookie in cookies:
            try:
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            except Exception:
                continue

    @staticmethod
    def _usable(result: FetchResult) -> bool:
        return result.status is not None and 200 <= result.status < 300 and bool(result.text)

    def fetch(self, url: str, mode: str = "AUTO", expect_sitemap: bool = False) -> FetchResult:
        mode = (mode or "AUTO").upper()
        if mode in {"BROWSER", "PLAYWRIGHT", "GITHUB"}:
            return self.fetch_browser(url)

        direct = self.fetch_requests(url)
        if direct.status is not None and 200 <= direct.status < 300:
            if not expect_sitemap:
                return direct
            kind = document_type(direct.text)
            if kind in {"sitemapindex", "urlset", "txt"}:
                return direct
            if kind == "html" and self._looks_like_html_sitemap(direct.text, url):
                return direct
        return self.fetch_browser(url)

    @staticmethod
    def _looks_like_html_sitemap(text: str, url: str) -> bool:
        lower = (text or "").lower()
        if any(marker in lower for marker in CHALLENGE_MARKERS):
            return False
        href_count = len(re.findall(r"<a\b[^>]+href=", text or "", re.I))
        return "sitemap" in (url or "").lower() and href_count >= 5


class SitemapCrawler:
    def __init__(
        self,
        fetcher: BrowserFetcher,
        *,
        max_depth: int = 12,
        max_sitemaps: int = 5000,
        max_urls: int = 250000,
    ):
        self.fetcher = fetcher
        self.max_depth = max_depth
        self.max_sitemaps = max_sitemaps
        self.max_urls = max_urls

    def crawl(self, config: SiteConfig) -> tuple[list[UrlItem], list[str]]:
        queue: deque[tuple[str, int]] = deque(
            (url, 0) for url in config.sitemap_urls
        )
        queued = set(config.sitemap_urls)
        processed: set[str] = set()
        items: dict[str, UrlItem] = {}
        failures: list[str] = []

        while queue:
            sitemap_url, depth = queue.popleft()
            if sitemap_url in processed:
                continue
            if len(processed) >= self.max_sitemaps:
                failures.append(f"Stopped at max_sitemaps={self.max_sitemaps}")
                break
            if len(items) >= self.max_urls:
                failures.append(f"Stopped at max_urls={self.max_urls}")
                break

            processed.add(sitemap_url)
            result = self.fetcher.fetch(
                sitemap_url,
                mode=config.fetch_mode,
                expect_sitemap=True,
            )
            kind = document_type(result.text)
            print(
                f"SITEMAP {len(processed)} depth={depth} HTTP={result.status} "
                f"method={result.method} type={kind} {sitemap_url}"
            )

            if result.status is None or not (200 <= result.status < 300):
                failures.append(
                    f"{sitemap_url}: HTTP={result.status}, method={result.method}, "
                    f"error={result.error}"
                )
                continue

            try:
                children, rows = self._parse_document(
                    result.text,
                    kind,
                    sitemap_url,
                )
            except Exception as exc:
                failures.append(f"{sitemap_url}: parse error {clean_error(exc)}")
                continue

            for row in rows:
                if self._within_site(row.url, config.site):
                    items.setdefault(row.url, row)
                if len(items) >= self.max_urls:
                    break

            if depth >= self.max_depth:
                if children:
                    failures.append(f"{sitemap_url}: children skipped at depth={depth}")
                continue

            for child in children:
                if child not in queued:
                    queued.add(child)
                    queue.append((child, depth + 1))

        return list(items.values()), failures

    def _parse_document(
        self,
        text: str,
        kind: str,
        source_url: str,
    ) -> tuple[list[str], list[UrlItem]]:
        if kind == "txt":
            rows = [
                UrlItem(line.strip(), "", source_url)
                for line in text.splitlines()
                if re.match(r"^https?://", line.strip(), re.I)
            ]
            return [], rows

        if kind in {"sitemapindex", "urlset"}:
            return self._parse_xml(text, source_url)

        if kind == "html":
            return self._parse_html_sitemap(text, source_url)

        # Robust fallback for malformed XML or plain URL dumps.
        locs = re.findall(
            r"<(?:[\w.-]+:)?loc[^>]*>\s*(.*?)\s*</(?:[\w.-]+:)?loc>",
            text,
            re.I | re.S,
        )
        cleaned = [re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", loc).strip() for loc in locs]
        children = [url for url in cleaned if self._looks_like_sitemap(url)]
        rows = [
            UrlItem(url, "", source_url)
            for url in cleaned
            if re.match(r"^https?://", url, re.I) and url not in children
        ]
        if not children and not rows:
            raise ValueError("Unknown sitemap response")
        return children, rows

    def _parse_xml(self, text: str, source_url: str) -> tuple[list[str], list[UrlItem]]:
        root = ET.fromstring(text.lstrip("\ufeff"))
        root_name = root.tag.rsplit("}", 1)[-1].lower()
        children: list[str] = []
        rows: list[UrlItem] = []

        if root_name == "sitemapindex":
            for node in root:
                if node.tag.rsplit("}", 1)[-1].lower() != "sitemap":
                    continue
                loc = self._child_text(node, "loc")
                if loc:
                    children.append(loc)
            return children, rows

        if root_name == "urlset":
            for node in root:
                if node.tag.rsplit("}", 1)[-1].lower() != "url":
                    continue
                loc = self._child_text(node, "loc")
                if loc:
                    rows.append(
                        UrlItem(
                            url=loc,
                            lastmod=self._child_text(node, "lastmod"),
                            source_sitemap=source_url,
                        )
                    )
            return children, rows

        raise ValueError(f"Unexpected XML root {root_name}")

    @staticmethod
    def _child_text(node: ET.Element, name: str) -> str:
        for child in node:
            if child.tag.rsplit("}", 1)[-1].lower() == name:
                return (child.text or "").strip()
        return ""

    def _parse_html_sitemap(self, text: str, source_url: str) -> tuple[list[str], list[UrlItem]]:
        soup = BeautifulSoup(text, "html.parser")
        children: list[str] = []
        rows: list[UrlItem] = []
        for node in soup.find_all("a", href=True):
            href = urljoin(source_url, node.get("href", "").strip())
            if not re.match(r"^https?://", href, re.I):
                continue
            if self._looks_like_sitemap(href):
                children.append(href)
            else:
                rows.append(UrlItem(href, "", source_url))
        # Some HTML sitemap pages paginate with ?page=N links. Those are kept as
        # child sitemap documents when the current URL itself looks like a sitemap.
        if self._looks_like_sitemap(source_url):
            for node in soup.find_all("a", href=True):
                href = urljoin(source_url, node.get("href", "").strip())
                if re.search(r"[?&]page=\d+", href, re.I):
                    children.append(href)
        return list(dict.fromkeys(children)), rows

    @staticmethod
    def _looks_like_sitemap(url: str) -> bool:
        lower = (url or "").lower()
        return bool(
            "sitemap" in lower
            or lower.endswith(".xml")
            or lower.endswith(".xml.gz")
            or lower.endswith(".txt")
        )

    @staticmethod
    def _within_site(url: str, site_url: str) -> bool:
        if not url or not site_url:
            return False
        parsed = urlparse(url)
        site = urlparse(site_url)
        if (parsed.hostname or "").lower().removeprefix("www.") != (site.hostname or "").lower().removeprefix("www."):
            return False
        base_path = (site.path or "/").rstrip("/")
        if not base_path:
            base_path = "/"
        if base_path == "/":
            return True
        return (parsed.path or "/").startswith(base_path + "/") or (parsed.path or "/").rstrip("/") == base_path
