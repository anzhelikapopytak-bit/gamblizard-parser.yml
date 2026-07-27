from __future__ import annotations

import csv
import hashlib
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

from .fetcher import BrowserFetcher, SitemapCrawler, clean_error, document_type
from .models import (
    BrandRecord,
    CategoryRecord,
    Detection,
    SiteConfig,
    SiteRunResult,
    UrlItem,
)
from .rules import (
    brand_key,
    classify_item,
    extract_bonus,
    extract_ref_link,
    host_of,
    prefilter_item,
)
from .sheets_client import SHEET_HEADERS, SheetsClient, utc_now


BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output")))
RAW_DIR = OUTPUT_DIR / "raw"
CANDIDATE_DIR = OUTPUT_DIR / "candidates"
DEBUG_DIR = OUTPUT_DIR / "debug"

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GEO = os.getenv("GEO", "ES").strip().upper()
RUN_ID = os.getenv("RUN_ID", "").strip() or f"{GEO}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
RUN_MODE = os.getenv("RUN_MODE", "FULL").strip().upper()
RUN_MONTH = os.getenv("RUN_MONTH", "").strip()
CLEAR_MISSING = os.getenv("CLEAR_MISSING", "false").lower() in {"1", "true", "yes"}

HEADLESS = os.getenv("HEADLESS", "false").lower() in {"1", "true", "yes"}
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "45"))
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT_SECONDS", "120"))
CHALLENGE_WAIT = int(os.getenv("AUTOMATIC_CHALLENGE_WAIT_SECONDS", "35"))
MAX_DEPTH = int(os.getenv("MAX_DEPTH", "12"))
MAX_SITEMAPS = int(os.getenv("MAX_SITEMAPS_PER_SITE", "5000"))
MAX_URLS = int(os.getenv("MAX_URLS_PER_SITE", "250000"))
MAX_HTML_PAGES = int(os.getenv("MAX_HTML_PAGES_PER_SITE", "15000"))
HTML_WORKERS = int(os.getenv("HTML_WORKERS", "10"))
HTML_TIMEOUT = int(os.getenv("HTML_TIMEOUT_SECONDS", "30"))

RUN_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")
STARTED_AT = utc_now()


def resolve_month(value: str) -> tuple[str, str, str]:
    """Return YYYY-MM, inclusive start and inclusive end dates."""
    candidate = value if re.fullmatch(r"\d{4}-\d{2}", value or "") else RUN_DATE[:7]
    first = datetime.strptime(candidate + "-01", "%Y-%m-%d").date()
    if first.month == 12:
        next_first = date(first.year + 1, 1, 1)
    else:
        next_first = date(first.year, first.month + 1, 1)
    last = next_first - timedelta(days=1)
    return candidate, first.isoformat(), last.isoformat()


MONTH_KEY, MONTH_FROM, MONTH_TO = resolve_month(RUN_MONTH)
MONTHLY_LABEL = f"{MONTH_KEY}_{GEO}"


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def safe_file_name(value: str, suffix: str = ".csv") -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "site").removeprefix("www.")
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{host}_{parsed.path.strip('/') or 'root'}")[:120]
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{raw}_{digest}{suffix}"


def write_url_csv(path: Path, rows: list[UrlItem]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["URL", "Last Modified", "Source Sitemap"])
        for row in rows:
            writer.writerow([row.url, row.lastmod, row.source_sitemap])


def direct_html_fetch(url: str, cookies: dict[str, str]) -> tuple[str, str]:
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9,fr;q=0.8,en;q=0.7",
            },
            cookies=cookies,
            timeout=HTML_TIMEOUT,
            allow_redirects=True,
        )
        text = response.text
        if response.status_code == 200 and "<html" in text[:3000].lower():
            return text, "requests"
        return "", f"HTTP {response.status_code}"
    except requests.RequestException as exc:
        return "", clean_error(exc)


def fetch_html_map(
    items: list[UrlItem],
    fetcher: BrowserFetcher,
    sheets: SheetsClient,
    site: str,
) -> tuple[dict[str, str], list[str]]:
    html_map: dict[str, str] = {}
    failures: list[str] = []
    if not items:
        return html_map, failures

    limited = items[:MAX_HTML_PAGES]
    if len(items) > MAX_HTML_PAGES:
        failures.append(
            f"HTML candidate limit reached: {MAX_HTML_PAGES} of {len(items)} pages"
        )

    cookies = fetcher.session.cookies.get_dict()
    failed_urls: list[str] = []

    with ThreadPoolExecutor(max_workers=max(1, HTML_WORKERS)) as executor:
        futures = {
            executor.submit(direct_html_fetch, item.url, cookies): item.url
            for item in limited
        }
        completed = 0
        for future in as_completed(futures):
            url = futures[future]
            completed += 1
            try:
                html, method = future.result()
            except Exception as exc:
                html, method = "", clean_error(exc)
            if html:
                html_map[url] = html
            else:
                failed_urls.append(url)
            if completed % 250 == 0:
                print(f"HTML {site}: {completed}/{len(limited)}, fallback={len(failed_urls)}")

    # Browser fallback is deliberately sequential because Playwright Page is not thread-safe.
    for index, url in enumerate(failed_urls, start=1):
        if sheets.stop_requested():
            raise RuntimeError("STOP_REQUESTED")
        result = fetcher.fetch_browser(url)
        if result.status is not None and 200 <= result.status < 300 and result.text:
            html_map[url] = result.text
        else:
            failures.append(
                f"HTML {url}: HTTP={result.status}, method={result.method}, error={result.error}"
            )
            if result.text:
                debug = DEBUG_DIR / safe_file_name(url, ".html")
                debug.write_text(result.text, encoding="utf-8", errors="replace")
        if index % 25 == 0:
            print(f"Browser HTML fallback {site}: {index}/{len(failed_urls)}")

    return html_map, failures


def date_in_range(value: str, start: str, end: str) -> bool:
    if not value or not start or not end:
        return False
    try:
        current = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        start_date = datetime.fromisoformat(start[:10]).date()
        end_date = datetime.fromisoformat(end[:10]).date()
        return start_date <= current <= end_date
    except ValueError:
        return False


def relevant_cache_rows(config: SiteConfig, items: list[UrlItem]) -> list[list]:
    key = host_of(config.site)
    return [
        [RUN_ID, key, index, item.url, item.lastmod, item.source_sitemap, config.role, config.site]
        for index, item in enumerate(items)
    ]


def process_site(
    config: SiteConfig,
    site_index: int,
    fetcher: BrowserFetcher,
    crawler: SitemapCrawler,
    sheets: SheetsClient,
    our_brand_map: dict[str, BrandRecord],
    our_category_map: dict[str, CategoryRecord],
    existing_missing_brands: set[str],
    existing_missing_categories: set[str],
) -> SiteRunResult:
    sheets.update_site_status(config, "RUNNING", "Fetching sitemap", 0, RUN_ID)
    sheets.set_status(
        RUN_ID,
        RUN_MODE,
        "FETCHING_SITEMAP",
        current_site=config.site,
        site_index=site_index,
        started_at=STARTED_AT,
        message=f"Fetching {len(config.sitemap_urls)} root sitemap(s)",
    )
    sheets.log("process_site", "INFO", "Site started", config.site)

    all_items, crawl_failures = crawler.crawl(config)
    raw_path = RAW_DIR / safe_file_name(config.site)
    write_url_csv(raw_path, all_items)

    candidates = [item for item in all_items if prefilter_item(item, config, GEO)]
    skipped_without_lastmod = 0
    if RUN_MODE == "MONTHLY_FRESH":
        fresh_candidates: list[UrlItem] = []
        for item in candidates:
            if date_in_range(item.lastmod, MONTH_FROM, MONTH_TO):
                fresh_candidates.append(item)
            elif not item.lastmod:
                skipped_without_lastmod += 1
        candidates = fresh_candidates
    candidate_path = CANDIDATE_DIR / safe_file_name(config.site)
    write_url_csv(candidate_path, candidates)

    sheets.update_site_status(
        config,
        "ANALYZING",
        f"Fetched {len(all_items)} URLs; {len(candidates)} candidates",
        len(all_items),
        RUN_ID,
    )
    sheets.set_status(
        RUN_ID,
        RUN_MODE,
        "FETCHING_HTML",
        current_site=config.site,
        site_index=site_index,
        processed_urls=0,
        total_urls=len(candidates),
        started_at=STARTED_AT,
        message=f"Fetching HTML for {len(candidates)} candidates",
    )

    html_map, html_failures = fetch_html_map(candidates, fetcher, sheets, config.site)
    result = SiteRunResult(
        config=config,
        total_sitemap_urls=len(all_items),
        candidate_urls=len(candidates),
        failures=[*crawl_failures, *html_failures],
        raw_csv=str(raw_path),
        candidate_csv=str(candidate_path),
    )
    if RUN_MODE == "MONTHLY_FRESH" and skipped_without_lastmod:
        sheets.log(
            "process_site", "INFO", "Monthly URLs without lastmod skipped",
            f"{config.site}: {skipped_without_lastmod}",
        )

    dump_url_rows: list[list] = []
    dump_brand_rows: list[list] = []
    dump_category_rows: list[list] = []
    main_brand_rows: list[list] = []
    missing_brand_rows: list[list] = []
    category_output_rows: list[list] = []
    missing_category_rows: list[list] = []
    monthly_url_rows: list[list] = []
    monthly_brand_rows: list[list] = []
    monthly_category_rows: list[list] = []

    processed_brand_keys: set[str] = set()
    processed_category_keys: set[str] = set()

    for index, item in enumerate(candidates, start=1):
        if index % 100 == 0:
            sheets.set_status(
                RUN_ID,
                RUN_MODE,
                "ANALYZING",
                current_site=config.site,
                site_index=site_index,
                url_index=index,
                processed_urls=index,
                total_urls=len(candidates),
                started_at=STARTED_AT,
                message=f"Analyzed {index}/{len(candidates)} candidate URLs",
            )
        if index % 500 == 0 and sheets.stop_requested():
            raise RuntimeError("STOP_REQUESTED")

        html = html_map.get(item.url, "")
        detection = classify_item(item, config, GEO, html)
        if detection.page_type not in {"BRAND", "CATEGORY"}:
            continue

        result.relevant_urls += 1
        dump_url = [
            RUN_DATE,
            config.site,
            config.role,
            item.url,
            item.lastmod,
            item.source_sitemap,
            detection.page_type,
        ]
        dump_url_rows.append(dump_url)
        if RUN_MODE == "MONTHLY_FRESH":
            monthly_url_rows.append(dump_url)

        if detection.page_type == "BRAND":
            key = detection.brand_key or brand_key(detection.brand, GEO)
            if not key:
                continue
            dedupe_key = f"{host_of(config.site)}|{key}"
            if dedupe_key in processed_brand_keys:
                continue
            processed_brand_keys.add(dedupe_key)

            if config.is_our:
                record = BrandRecord(
                    RUN_DATE, config.site, config.role, item.url, item.lastmod,
                    detection.brand, key, "OUR",
                )
                result.brands.append(record)
                our_brand_map.setdefault(key, record)
            else:
                matched = our_brand_map.get(key)
                status = "MATCH" if matched else "MISSING"
                bonus = ""
                ref_link = ""
                if status == "MISSING":
                    if not html:
                        browser_result = fetcher.fetch(item.url, mode=config.fetch_mode)
                        if browser_result.status == 200:
                            html = browser_result.text
                    bonus = extract_bonus(html, config, GEO)
                    ref_link = extract_ref_link(html, item.url)

                record = BrandRecord(
                    RUN_DATE, config.site, config.role, item.url, item.lastmod,
                    detection.brand, key, status, bonus, ref_link,
                )
                result.brands.append(record)
                main_brand_rows.append([
                    matched.url if matched else "",
                    matched.lastmod if matched else "",
                    matched.run_date if matched else "",
                    matched.brand if matched else "",
                    "BRAND" if matched else "",
                    config.site,
                    item.url,
                    item.lastmod,
                    RUN_DATE,
                    "BRAND",
                    status,
                    detection.brand,
                    bonus,
                    "",
                ])
                missing_key = f"{item.url.lower()}|{detection.brand.lower()}"
                if status == "MISSING" and missing_key not in existing_missing_brands:
                    missing_brand_rows.append([
                        item.url,
                        detection.brand,
                        bonus,
                        ref_link,
                        "",
                        config.affiliate_network,
                        "",
                        "",
                    ])
                    existing_missing_brands.add(missing_key)

            dump_brand = [
                RUN_DATE, config.site, config.role, item.url, item.lastmod,
                "BRAND", detection.brand, key,
                result.brands[-1].match_status if result.brands else "",
                result.brands[-1].bonus if result.brands else "",
                result.brands[-1].ref_link if result.brands else "",
            ]
            dump_brand_rows.append(dump_brand)
            if RUN_MODE == "MONTHLY_FRESH":
                monthly_brand_rows.append(dump_brand)
            continue

        # CATEGORY
        category_key = detection.category_key or detection.h1_key or detection.url_key
        if not category_key:
            continue
        dedupe_key = f"{host_of(config.site)}|{category_key}"
        if dedupe_key in processed_category_keys:
            continue
        processed_category_keys.add(dedupe_key)

        if config.is_our:
            record = CategoryRecord(
                RUN_DATE, config.site, config.role, item.url, item.lastmod,
                category_key, "OUR", item.url, detection.h1,
                detection.h1_key or category_key,
            )
            result.categories.append(record)
            for key in {category_key, detection.h1_key, detection.url_key}:
                if key:
                    our_category_map.setdefault(key, record)
        else:
            matched = None
            for key in (detection.h1_key, detection.url_key, category_key):
                if key and key in our_category_map:
                    matched = our_category_map[key]
                    break
            status = "MATCH" if matched else "MISSING"
            record = CategoryRecord(
                RUN_DATE, config.site, config.role, item.url, item.lastmod,
                category_key, status, matched.url if matched else "",
                detection.h1, detection.h1_key or category_key,
            )
            result.categories.append(record)
            category_output_rows.append([
                matched.url if matched else "",
                "CATEGORY",
                item.url,
                config.site,
                status,
                RUN_DATE,
                f"H1: {detection.h1} | Key: {category_key} | Mode: H1",
            ])
            missing_key = f"{config.site.lower()}|{item.url.lower()}"
            if status == "MISSING" and missing_key not in existing_missing_categories:
                missing_category_rows.append([
                    RUN_DATE,
                    item.url,
                    detection.h1,
                    "",
                    f"Key: {category_key}",
                    config.site,
                    "CATEGORY",
                ])
                existing_missing_categories.add(missing_key)

        dump_category = [
            RUN_DATE,
            config.site,
            config.role,
            item.url,
            item.lastmod,
            "CATEGORY",
            category_key,
            result.categories[-1].match_status if result.categories else "",
            result.categories[-1].matched_our_url if result.categories else "",
            detection.h1,
            detection.h1_key or category_key,
        ]
        dump_category_rows.append(dump_category)
        if RUN_MODE == "MONTHLY_FRESH":
            monthly_category_rows.append(dump_category)

    # Write this site's data in bounded chunks.
    sheets.append_rows("tech-dump_urls", dump_url_rows)
    sheets.append_rows("tech-dump_brands", dump_brand_rows)
    sheets.append_rows("tech-dump_categories", dump_category_rows)
    sheets.append_rows("tech-auto_url_cache", relevant_cache_rows(config, candidates))
    if main_brand_rows:
        sheets.append_rows("1_ MAIN Brands", main_brand_rows)
    if missing_brand_rows:
        sheets.append_rows("2_Missing Brands", missing_brand_rows)
    if category_output_rows:
        sheets.append_rows("3_Categories", category_output_rows)
    if missing_category_rows:
        sheets.append_rows("4_Missing Categories", missing_category_rows)

    if RUN_MODE == "MONTHLY_FRESH" and (monthly_url_rows or monthly_brand_rows or monthly_category_rows):
        sheets.append_monthly_dump(
            MONTHLY_LABEL, monthly_url_rows, monthly_brand_rows, monthly_category_rows
        )

    final_status = "PARTIAL" if result.failures else "DONE"
    sheets.update_site_status(
        config,
        final_status,
        " | ".join(result.failures)[:45000] or (
            f"Fetched {len(all_items)} URLs; candidates {len(candidates)}; "
            f"relevant {result.relevant_urls}"
        ),
        len(all_items),
        RUN_ID,
        utc_now(),
    )
    sheets.log(
        "process_site",
        "WARN" if result.failures else "INFO",
        f"Site {final_status}",
        f"{config.site}; raw={len(all_items)}; candidates={len(candidates)}; "
        f"relevant={result.relevant_urls}; failures={' | '.join(result.failures)}",
    )
    return result


def write_summary_csv(results: list[SiteRunResult]) -> Path:
    path = OUTPUT_DIR / "summary.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow([
            "Site", "Role", "Status", "Raw URLs", "Candidate URLs",
            "Relevant URLs", "Brands", "Categories", "Failures",
            "Raw CSV", "Candidate CSV",
        ])
        for result in results:
            writer.writerow([
                result.config.site,
                result.config.role,
                "PARTIAL" if result.failures else "DONE",
                result.total_sitemap_urls,
                result.candidate_urls,
                result.relevant_urls,
                len(result.brands),
                len(result.categories),
                " | ".join(result.failures),
                result.raw_csv,
                result.candidate_csv,
            ])
    return path


def load_existing_our_maps(
    sheets: SheetsClient,
) -> tuple[dict[str, BrandRecord], dict[str, CategoryRecord]]:
    """Load prior OUR inventory so monthly competitor matching stays correct."""
    brand_map: dict[str, BrandRecord] = {}
    for row in sheets.read_values("'tech-dump_brands'!A2:K"):
        padded = list(row) + [""] * (11 - len(row))
        if str(padded[2]).strip().lower() != "our":
            continue
        key = str(padded[7]).strip()
        if not key:
            continue
        brand_map[key] = BrandRecord(
            str(padded[0]), str(padded[1]), str(padded[2]), str(padded[3]),
            str(padded[4]), str(padded[6]), key, str(padded[8] or "OUR"),
            str(padded[9]), str(padded[10]),
        )

    category_map: dict[str, CategoryRecord] = {}
    for row in sheets.read_values("'tech-dump_categories'!A2:K"):
        padded = list(row) + [""] * (11 - len(row))
        if str(padded[2]).strip().lower() != "our":
            continue
        record = CategoryRecord(
            str(padded[0]), str(padded[1]), str(padded[2]), str(padded[3]),
            str(padded[4]), str(padded[6]), str(padded[7] or "OUR"),
            str(padded[8]), str(padded[9]), str(padded[10]),
        )
        for key in {str(padded[6]).strip(), str(padded[10]).strip()}:
            if key:
                category_map[key] = record
    return brand_map, category_map


def main() -> int:
    if GEO not in {"ES", "FR"}:
        raise ValueError("GEO must be ES or FR")
    if RUN_MODE not in {"FULL", "MONTHLY_FRESH", "RETRY_FAILED"}:
        raise ValueError("RUN_MODE must be FULL, MONTHLY_FRESH or RETRY_FAILED")

    sheets = SheetsClient(SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON)
    sheets.setup_structure()
    configs = sheets.read_site_configs(GEO, RUN_MODE)
    if not configs:
        raise RuntimeError(f"No enabled {GEO} sources found in Tech part")
    if not any(config.is_our for config in configs):
        raise RuntimeError("Tech part must contain one enabled row with Role=our")

    # Always process our site first so competitor matching has source maps.
    configs.sort(key=lambda config: (0 if config.is_our else 1, config.row_number))

    if RUN_MODE == "FULL":
        sheets.clear_for_new_run(clear_missing=CLEAR_MISSING)
    elif RUN_MODE == "MONTHLY_FRESH":
        sheets.prepare_monthly_dump(MONTHLY_LABEL)
        sheets.clear_data("tech-auto_url_cache")
        sheets.clear_data("tech-log")

    sheets.set_status(
        RUN_ID,
        RUN_MODE,
        "STARTING",
        started_at=STARTED_AT,
        message=f"GitHub parser started for {GEO}; mode={RUN_MODE}; month={MONTH_KEY}; sites={len(configs)}",
    )
    sheets.log("main", "INFO", "Parser started", f"run={RUN_ID}; geo={GEO}; mode={RUN_MODE}")

    results: list[SiteRunResult] = []
    if RUN_MODE == "MONTHLY_FRESH":
        our_brand_map, our_category_map = load_existing_our_maps(sheets)
    else:
        our_brand_map = {}
        our_category_map = {}
    existing_missing_brands = sheets.existing_missing_brand_keys()
    existing_missing_categories = sheets.existing_missing_category_keys()

    try:
        with sync_playwright() as playwright:
            fetcher = BrowserFetcher(
                playwright,
                headless=HEADLESS,
                request_timeout=REQUEST_TIMEOUT,
                browser_timeout=BROWSER_TIMEOUT,
                challenge_wait=CHALLENGE_WAIT,
            )
            crawler = SitemapCrawler(
                fetcher,
                max_depth=MAX_DEPTH,
                max_sitemaps=MAX_SITEMAPS,
                max_urls=MAX_URLS,
            )
            try:
                for index, config in enumerate(configs):
                    if sheets.stop_requested():
                        raise RuntimeError("STOP_REQUESTED")
                    try:
                        result = process_site(
                            config,
                            index,
                            fetcher,
                            crawler,
                            sheets,
                            our_brand_map,
                            our_category_map,
                            existing_missing_brands,
                            existing_missing_categories,
                        )
                        results.append(result)
                    except RuntimeError as exc:
                        if str(exc) == "STOP_REQUESTED":
                            raise
                        message = clean_error(exc, 45000)
                        sheets.update_site_status(config, "ERROR", message, 0, RUN_ID, utc_now())
                        sheets.log("main", "ERROR", "Site fatal error", f"{config.site}: {message}")
                        results.append(SiteRunResult(config=config, failures=[message]))
                    except Exception as exc:
                        message = clean_error(exc, 45000)
                        sheets.update_site_status(config, "ERROR", message, 0, RUN_ID, utc_now())
                        sheets.log("main", "ERROR", "Site fatal error", f"{config.site}: {message}")
                        results.append(SiteRunResult(config=config, failures=[message]))
            finally:
                fetcher.close()

        summary_path = write_summary_csv(results)
        failed = sum(1 for result in results if result.failures and result.relevant_urls == 0)
        partial = sum(1 for result in results if result.failures and result.relevant_urls > 0)
        phase = "DONE" if failed == 0 and partial == 0 else "PARTIAL"
        message = (
            f"Finished {len(results)} sites; failed={failed}; partial={partial}; "
            f"brands={sum(len(r.brands) for r in results)}; "
            f"categories={sum(len(r.categories) for r in results)}; "
            f"artifact={summary_path.name}"
        )
        sheets.set_status(
            RUN_ID,
            RUN_MODE,
            phase,
            site_index=len(results),
            processed_urls=sum(r.relevant_urls for r in results),
            total_urls=sum(r.candidate_urls for r in results),
            done=True,
            started_at=STARTED_AT,
            message=message,
        )
        sheets.log("main", "INFO" if phase == "DONE" else "WARN", "Parser finished", message)
        print(message)
        return 0

    except RuntimeError as exc:
        if str(exc) == "STOP_REQUESTED":
            sheets.set_status(
                RUN_ID,
                RUN_MODE,
                "STOPPED",
                done=True,
                started_at=STARTED_AT,
                message="Stopped from Google Sheet",
            )
            sheets.log("main", "WARN", "Parser stopped", RUN_ID)
            return 0
        raise
    except Exception as exc:
        message = clean_error(exc, 45000)
        sheets.set_status(
            RUN_ID,
            RUN_MODE,
            "ERROR",
            done=True,
            started_at=STARTED_AT,
            message=message,
        )
        sheets.log("main", "ERROR", "Parser fatal error", message)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
