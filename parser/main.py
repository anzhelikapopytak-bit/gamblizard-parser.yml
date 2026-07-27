from __future__ import annotations

import csv
import hashlib
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from .fetcher import BrowserFetcher, SitemapCrawler, clean_error
from .models import SiteConfig, UrlItem
from .rules import classify_item, host_of, prefilter_item
from .sheets_client import SheetsClient, utc_now

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output")))
RAW_DIR = OUTPUT_DIR / "raw"
FILTERED_DIR = OUTPUT_DIR / "filtered"

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GEO = os.getenv("GEO", "ES").strip().upper()
RUN_ID = os.getenv("RUN_ID", "").strip() or f"{GEO}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
RUN_MODE = os.getenv("RUN_MODE", "FULL").strip().upper()
RUN_MONTH = os.getenv("RUN_MONTH", "").strip()

HEADLESS = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes"}
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT_SECONDS", "60"))
CHALLENGE_WAIT = int(os.getenv("AUTOMATIC_CHALLENGE_WAIT_SECONDS", "20"))
MAX_DEPTH = int(os.getenv("MAX_DEPTH", "12"))
MAX_SITEMAPS = int(os.getenv("MAX_SITEMAPS_PER_SITE", "5000"))
MAX_URLS = int(os.getenv("MAX_URLS_PER_SITE", "500000"))

RUN_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")
STARTED_AT = utc_now()


def resolve_month(value: str) -> tuple[str, str, str]:
    candidate = value if re.fullmatch(r"\d{4}-\d{2}", value or "") else RUN_DATE[:7]
    first = datetime.strptime(candidate + "-01", "%Y-%m-%d").date()
    next_first = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
    last = next_first - timedelta(days=1)
    return candidate, first.isoformat(), last.isoformat()


MONTH_KEY, MONTH_FROM, MONTH_TO = resolve_month(RUN_MONTH)
MONTHLY_LABEL = f"{MONTH_KEY}_{GEO}"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
FILTERED_DIR.mkdir(parents=True, exist_ok=True)


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


def date_in_range(value: str, start: str, end: str) -> bool:
    if not value:
        return False
    try:
        current = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return datetime.fromisoformat(start).date() <= current <= datetime.fromisoformat(end).date()
    except ValueError:
        return False


def url_dump_row(config: SiteConfig, item: UrlItem) -> list[str]:
    detection = classify_item(item, config, GEO, "")
    detected_type = detection.page_type if detection.page_type in {"BRAND", "CATEGORY"} else "CANDIDATE"
    return [
        RUN_DATE,
        config.site,
        config.role,
        item.url,
        item.lastmod,
        item.source_sitemap,
        detected_type,
    ]


def cache_rows(config: SiteConfig, items: list[UrlItem]) -> list[list]:
    site_key = host_of(config.site)
    return [
        [RUN_ID, site_key, index, item.url, item.lastmod, item.source_sitemap, config.role, config.site]
        for index, item in enumerate(items, start=1)
    ]


def process_site(
    config: SiteConfig,
    site_index: int,
    total_sites: int,
    crawler: SitemapCrawler,
    sheets: SheetsClient,
) -> tuple[int, int, list[str]]:
    sheets.update_site_status(config, "RUNNING", "Reading sitemap only", 0, RUN_ID)
    sheets.set_status(
        RUN_ID,
        RUN_MODE,
        "FETCHING_SITEMAP",
        current_site=config.site,
        site_index=site_index,
        started_at=STARTED_AT,
        message=f"Site {site_index}/{total_sites}: reading sitemap; page HTML will not be opened",
    )
    sheets.log("process_site", "INFO", "Sitemap export started", config.site)

    all_items, failures = crawler.crawl(config)
    write_url_csv(RAW_DIR / safe_file_name(config.site), all_items)

    candidates = [item for item in all_items if prefilter_item(item, config, GEO)]
    skipped_without_lastmod = 0
    if RUN_MODE == "MONTHLY_FRESH":
        filtered: list[UrlItem] = []
        for item in candidates:
            if date_in_range(item.lastmod, MONTH_FROM, MONTH_TO):
                filtered.append(item)
            elif not item.lastmod:
                skipped_without_lastmod += 1
        candidates = filtered

    write_url_csv(FILTERED_DIR / safe_file_name(config.site), candidates)
    rows = [url_dump_row(config, item) for item in candidates]

    sheets.set_status(
        RUN_ID,
        RUN_MODE,
        "WRITING_URLS",
        current_site=config.site,
        site_index=site_index,
        processed_urls=0,
        total_urls=len(candidates),
        started_at=STARTED_AT,
        message=f"Writing {len(candidates)} filtered sitemap URLs to Google Sheets",
    )

    sheets.append_rows("tech-dump_urls", rows)
    sheets.append_rows("tech-auto_url_cache", cache_rows(config, candidates))
    if RUN_MODE == "MONTHLY_FRESH" and rows:
        sheets.append_monthly_dump(MONTHLY_LABEL, rows, [], [])

    message = f"Sitemap URLs={len(all_items)}; exported={len(candidates)}"
    if skipped_without_lastmod:
        message += f"; monthly skipped without lastmod={skipped_without_lastmod}"
    if failures:
        message += f"; sitemap warnings={len(failures)}"
    status = "PARTIAL" if failures else "DONE"
    sheets.update_site_status(config, status, message, len(candidates), RUN_ID, utc_now())
    for failure in failures:
        sheets.log("process_site", "WARN", "Sitemap warning", f"{config.site}: {failure}")
    sheets.log("process_site", "INFO", "Sitemap export finished", f"{config.site}: {message}")
    print(f"SITE DONE {site_index}/{total_sites} {config.site}: {message}", flush=True)
    return len(all_items), len(candidates), failures


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
    configs.sort(key=lambda config: config.row_number)

    # URL-only export must not delete or rewrite Brands/Categories/Missing tabs.
    if RUN_MODE == "FULL":
        sheets.clear_data("tech-dump_urls")
        sheets.clear_data("tech-auto_url_cache")
        sheets.clear_data("tech-log")
    elif RUN_MODE == "MONTHLY_FRESH":
        sheets.prepare_monthly_dump(MONTHLY_LABEL)
        sheets.clear_data("tech-auto_url_cache")
        sheets.clear_data("tech-log")

    sheets.set_status(
        RUN_ID,
        RUN_MODE,
        "STARTING",
        started_at=STARTED_AT,
        message=f"URL-only sitemap export started for {GEO}; no page HTML; sites={len(configs)}",
    )
    sheets.log("main", "INFO", "URL-only export started", f"run={RUN_ID}; geo={GEO}; mode={RUN_MODE}")

    total_found = 0
    total_exported = 0
    warning_count = 0

    try:
        with sync_playwright() as playwright:
            fetcher = BrowserFetcher(
                playwright,
                headless=HEADLESS,
                request_timeout=REQUEST_TIMEOUT,
                browser_timeout=BROWSER_TIMEOUT,
                challenge_wait=CHALLENGE_WAIT,
            )
            crawler = SitemapCrawler(fetcher, max_depth=MAX_DEPTH, max_sitemaps=MAX_SITEMAPS, max_urls=MAX_URLS)
            try:
                for index, config in enumerate(configs, start=1):
                    if sheets.stop_requested():
                        raise RuntimeError("STOP_REQUESTED")
                    try:
                        found, exported, failures = process_site(config, index, len(configs), crawler, sheets)
                        total_found += found
                        total_exported += exported
                        warning_count += len(failures)
                    except Exception as exc:
                        if isinstance(exc, RuntimeError) and str(exc) == "STOP_REQUESTED":
                            raise
                        message = clean_error(exc, 45000)
                        warning_count += 1
                        sheets.update_site_status(config, "ERROR", message, 0, RUN_ID, utc_now())
                        sheets.log("main", "ERROR", "Site export failed", f"{config.site}: {message}")
                        print(f"SITE ERROR {config.site}: {message}", flush=True)
            finally:
                fetcher.close()

        phase = "DONE" if warning_count == 0 else "PARTIAL"
        message = (
            f"URL-only export finished; sites={len(configs)}; sitemap URLs={total_found}; "
            f"exported={total_exported}; warnings={warning_count}; no HTML pages opened"
        )
        sheets.set_status(
            RUN_ID,
            RUN_MODE,
            phase,
            site_index=len(configs),
            processed_urls=total_exported,
            total_urls=total_exported,
            done=True,
            started_at=STARTED_AT,
            message=message,
        )
        sheets.log("main", "INFO" if phase == "DONE" else "WARN", "URL-only export finished", message)
        print(message, flush=True)
        return 0

    except RuntimeError as exc:
        if str(exc) == "STOP_REQUESTED":
            sheets.set_status(RUN_ID, RUN_MODE, "STOPPED", done=True, started_at=STARTED_AT, message="Stopped from Google Sheet")
            sheets.log("main", "WARN", "Export stopped", RUN_ID)
            return 0
        raise
    except Exception as exc:
        message = clean_error(exc, 45000)
        sheets.set_status(RUN_ID, RUN_MODE, "ERROR", done=True, started_at=STARTED_AT, message=message)
        sheets.log("main", "ERROR", "URL export fatal error", message)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
