from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class SiteConfig:
    row_number: int
    locale: str
    site: str
    sitemap_urls: list[str]
    brand_patterns: str
    bonus_footprint: str
    category_patterns: str
    sitemap_type: str
    role: str
    enabled: bool
    notes: str = ""
    affiliate_network: str = ""
    ref_rule: str = ""
    monthly_label: str = ""
    monthly_from: str = ""
    monthly_to: str = ""
    fetch_mode: str = "AUTO"
    include_regex: str = ""
    exclude_regex: str = ""
    last_status: str = ""
    last_message: str = ""
    last_url_count: int = 0
    last_run_id: str = ""
    last_finished: str = ""

    @property
    def is_our(self) -> bool:
        return self.role.strip().lower() == "our"


@dataclass(slots=True)
class UrlItem:
    url: str
    lastmod: str = ""
    source_sitemap: str = ""


@dataclass(slots=True)
class FetchResult:
    url: str
    status: Optional[int]
    content_type: str
    text: str
    method: str
    error: str = ""


@dataclass(slots=True)
class Detection:
    page_type: str
    brand: str = ""
    brand_key: str = ""
    category_key: str = ""
    url_key: str = ""
    h1: str = ""
    h1_key: str = ""
    html: str = ""
    bonus: str = ""
    ref_link: str = ""
    reason: str = ""


@dataclass(slots=True)
class BrandRecord:
    run_date: str
    site: str
    role: str
    url: str
    lastmod: str
    brand: str
    brand_key: str
    match_status: str
    bonus: str = ""
    ref_link: str = ""


@dataclass(slots=True)
class CategoryRecord:
    run_date: str
    site: str
    role: str
    url: str
    lastmod: str
    category_key: str
    match_status: str
    matched_our_url: str = ""
    h1: str = ""
    commercial_key: str = ""


@dataclass(slots=True)
class SiteRunResult:
    config: SiteConfig
    total_sitemap_urls: int = 0
    candidate_urls: int = 0
    relevant_urls: int = 0
    brands: list[BrandRecord] = field(default_factory=list)
    categories: list[CategoryRecord] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    raw_csv: str = ""
    candidate_csv: str = ""


@dataclass(slots=True)
class RunSummary:
    run_id: str
    geo: str
    started_at: str
    finished_at: str = ""
    mode: str = "FULL"
    status: str = "RUNNING"
    processed_sites: int = 0
    failed_sites: int = 0
    total_candidates: int = 0
    total_relevant: int = 0
    message: str = ""
