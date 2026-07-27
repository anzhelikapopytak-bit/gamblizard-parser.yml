from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Iterable, Sequence

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .models import SiteConfig


TECH_HEADERS = [
    "Local",
    "Site URL",
    "Sitemap URL",
    "Brand URL patterns",
    "Bonus footprint",
    "Category URL patterns",
    "Sitemap type",
    "Role",
    "Enabled",
    "Notes",
    "Affiliate Network",
    "Ref rule",
    "Monthly Dump Label",
    "Monthly Lastmod From",
    "Monthly Lastmod To",
    "Fetch Mode",
    "Include URL Regex",
    "Exclude URL Regex",
    "Last Status",
    "Last Message",
    "Last URL Count",
    "Last Run ID",
    "Last Finished",
]

SHEET_HEADERS = {
    "1_ MAIN Brands": [
        "Our URL", "Our Last Modified", "Our Parsing Date", "Our Brand Name",
        "Our Page Type", "Competitor", "Competitor URL", "Last Modified",
        "Parsing Date", "Page Type", "MATCH / MISSING", "Brand Name",
        "Bonus", "SEO Comment",
    ],
    "2_Missing Brands": [
        "URL", "BRAND", "Bonus", "ref_link", "Affiliate Comment",
        "Affiliate Network", "SEO content", "Comments",
    ],
    "3_Categories": [
        "Our URL", "Page type", "Competitor URL", "Competitor",
        "Status - MATCH / MISSING", "Parsing date", "SEO comment",
    ],
    "4_Missing Categories": [
        "Date", "URL", "Top keywords", "Search volume", "Seo comment",
        "Competitor", "Page type",
    ],
    "tech-dump_urls": [
        "Run Date", "Site", "Role", "URL", "Last Modified",
        "Source Sitemap", "Detected Type",
    ],
    "tech-dump_brands": [
        "Run Date", "Site", "Role", "URL", "Last Modified", "Page Type",
        "Brand", "Brand Key", "Match Status", "Bonus", "Ref Link",
    ],
    "tech-dump_categories": [
        "Run Date", "Site", "Role", "URL", "Last Modified", "Page Type",
        "Category Key", "Match Status", "Matched Our URL", "H1",
        "Commercial Key",
    ],
    "tech-log": ["Date", "Function", "Level", "Message", "Details"],
    "tech-auto_url_cache": [
        "Run ID", "Site Key", "Index", "URL", "Last Modified",
        "Source Sitemap", "Role", "Site",
    ],
    "tech-auto_status": [
        "Run ID", "Mode", "Phase", "Current Site", "Site Index",
        "URL Index", "Processed URLs", "Total URLs", "Done", "Started At",
        "Updated At", "Message",
    ],
}

CLEAR_ON_START = [
    "1_ MAIN Brands",
    "3_Categories",
    "tech-dump_urls",
    "tech-dump_brands",
    "tech-dump_categories",
    "tech-auto_url_cache",
    "tech-log",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def quote_sheet(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def col_letter(number: int) -> str:
    result = ""
    while number:
        number, rem = divmod(number - 1, 26)
        result = chr(65 + rem) + result
    return result


class SheetsClient:
    def __init__(self, spreadsheet_id: str, service_account_json: str):
        if not spreadsheet_id:
            raise ValueError("SPREADSHEET_ID is empty")
        if not service_account_json:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is empty")

        info = json.loads(service_account_json)
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.spreadsheet_id = spreadsheet_id
        self.service = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        self.sheet_ids: dict[str, int] = {}
        self.refresh_metadata()

    def _execute(self, request_factory, attempts: int = 8):
        for attempt in range(attempts):
            try:
                return request_factory().execute()
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                if status not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                    raise
                delay = min(60.0, (2 ** attempt) + 0.35)
                print(f"Sheets API HTTP {status}; retry in {delay:.2f}s")
                time.sleep(delay)
        raise RuntimeError("Unexpected Sheets API retry termination")

    def refresh_metadata(self) -> None:
        response = self._execute(
            lambda: self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets.properties(sheetId,title,gridProperties)",
            )
        )
        self.sheet_ids = {
            sh["properties"]["title"]: sh["properties"]["sheetId"]
            for sh in response.get("sheets", [])
        }

    def ensure_sheet(self, name: str, rows: int = 1000, cols: int = 20) -> int:
        if name in self.sheet_ids:
            return self.sheet_ids[name]
        response = self._execute(
            lambda: self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": name,
                                    "gridProperties": {
                                        "rowCount": rows,
                                        "columnCount": cols,
                                        "frozenRowCount": 1,
                                    },
                                }
                            }
                        }
                    ]
                },
            )
        )
        sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]
        self.sheet_ids[name] = sheet_id
        return sheet_id

    def setup_structure(self) -> None:
        self.ensure_sheet("Tech part", rows=1000, cols=len(TECH_HEADERS))
        self.write_header("Tech part", TECH_HEADERS)
        for name, headers in SHEET_HEADERS.items():
            self.ensure_sheet(name, rows=1000, cols=len(headers))
            self.write_header(name, headers)
        self._style_headers()

    def _style_headers(self) -> None:
        requests = []
        for name in ["Tech part", *SHEET_HEADERS.keys()]:
            sheet_id = self.sheet_ids.get(name)
            if sheet_id is None:
                continue
            requests.extend([
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {
                                    "red": 31 / 255,
                                    "green": 78 / 255,
                                    "blue": 120 / 255,
                                },
                                "textFormat": {
                                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                    "bold": True,
                                },
                                "wrapStrategy": "WRAP",
                                "verticalAlignment": "MIDDLE",
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
            ])
        if requests:
            self._execute(
                lambda: self.service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": requests},
                )
            )

    def write_header(self, sheet: str, headers: Sequence[str]) -> None:
        self._execute(
            lambda: self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{quote_sheet(sheet)}!A1:{col_letter(len(headers))}1",
                valueInputOption="RAW",
                body={"values": [list(headers)]},
            )
        )

    def read_values(self, a1_range: str) -> list[list]:
        response = self._execute(
            lambda: self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=a1_range,
            )
        )
        return response.get("values", [])

    def update_values(self, a1_range: str, rows: Sequence[Sequence]) -> None:
        if not rows:
            return
        self._execute(
            lambda: self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=a1_range,
                valueInputOption="RAW",
                body={"values": [list(r) for r in rows]},
            )
        )

    def append_rows(self, sheet: str, rows: Sequence[Sequence], chunk_size: int = 3000) -> None:
        if not rows:
            return
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start:start + chunk_size]
            self._execute(
                lambda chunk=chunk: self.service.spreadsheets().values().append(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{quote_sheet(sheet)}!A:ZZ",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [list(r) for r in chunk]},
                )
            )

    def clear_data(self, sheet: str) -> None:
        self.ensure_sheet(sheet)
        self._execute(
            lambda: self.service.spreadsheets().values().clear(
                spreadsheetId=self.spreadsheet_id,
                range=f"{quote_sheet(sheet)}!A2:ZZ",
                body={},
            )
        )

    def clear_for_new_run(self, clear_missing: bool = False) -> None:
        targets = list(CLEAR_ON_START)
        if clear_missing:
            targets.extend(["2_Missing Brands", "4_Missing Categories"])
        for sheet in targets:
            self.clear_data(sheet)

    def read_site_configs(self, geo: str, mode: str = "FULL") -> list[SiteConfig]:
        rows = self.read_values(f"{quote_sheet('Tech part')}!A2:W")
        result: list[SiteConfig] = []
        for offset, row in enumerate(rows, start=2):
            padded = list(row) + [""] * (23 - len(row))
            locale = str(padded[0]).strip().upper()
            site = str(padded[1]).strip()
            if not site or locale != geo.upper():
                continue
            enabled = str(padded[8]).strip().upper() in {"YES", "TRUE", "1", "Y"}
            if not enabled:
                continue
            last_status = str(padded[18]).strip().upper()
            if mode.upper() == "RETRY_FAILED" and last_status not in {"ERROR", "FAILED", "PARTIAL"}:
                continue
            sitemap_urls = [
                part.strip()
                for part in str(padded[2]).replace("\r", "\n").split("\n")
                if part.strip()
            ]
            if not sitemap_urls:
                continue
            try:
                last_count = int(float(padded[20])) if str(padded[20]).strip() else 0
            except ValueError:
                last_count = 0
            result.append(
                SiteConfig(
                    row_number=offset,
                    locale=locale,
                    site=site,
                    sitemap_urls=sitemap_urls,
                    brand_patterns=str(padded[3]),
                    bonus_footprint=str(padded[4]),
                    category_patterns=str(padded[5]),
                    sitemap_type=str(padded[6] or "AUTO"),
                    role=str(padded[7] or "competitor"),
                    enabled=enabled,
                    notes=str(padded[9]),
                    affiliate_network=str(padded[10]),
                    ref_rule=str(padded[11]),
                    monthly_label=str(padded[12]),
                    monthly_from=str(padded[13]),
                    monthly_to=str(padded[14]),
                    fetch_mode=str(padded[15] or "AUTO").upper(),
                    include_regex=str(padded[16]),
                    exclude_regex=str(padded[17]),
                    last_status=last_status,
                    last_message=str(padded[19]),
                    last_url_count=last_count,
                    last_run_id=str(padded[21]),
                    last_finished=str(padded[22]),
                )
            )
        return result

    def update_site_status(
        self,
        config: SiteConfig,
        status: str,
        message: str,
        url_count: int,
        run_id: str,
        finished: str = "",
    ) -> None:
        self.update_values(
            f"{quote_sheet('Tech part')}!S{config.row_number}:W{config.row_number}",
            [[status, message[:45000], url_count, run_id, finished]],
        )

    def set_status(
        self,
        run_id: str,
        mode: str,
        phase: str,
        current_site: str = "",
        site_index: int = 0,
        url_index: int = 0,
        processed_urls: int = 0,
        total_urls: int | str = "",
        done: bool = False,
        started_at: str = "",
        message: str = "",
    ) -> None:
        self.clear_data("tech-auto_status")
        self.update_values(
            f"{quote_sheet('tech-auto_status')}!A2:L2",
            [[
                run_id,
                mode,
                phase,
                current_site,
                site_index,
                url_index,
                processed_urls,
                total_urls,
                done,
                started_at,
                utc_now(),
                message[:45000],
            ]],
        )

    def get_phase(self) -> str:
        rows = self.read_values(f"{quote_sheet('tech-auto_status')}!C2:C2")
        if not rows or not rows[0]:
            return ""
        return str(rows[0][0]).strip().upper()

    def stop_requested(self) -> bool:
        return self.get_phase() == "STOP_REQUESTED"

    def log(self, function: str, level: str, message: str, details: str = "") -> None:
        self.append_rows(
            "tech-log",
            [[utc_now(), function, level, message, details[:45000]]],
            chunk_size=1,
        )

    def existing_missing_brand_keys(self) -> set[str]:
        rows = self.read_values(f"{quote_sheet('2_Missing Brands')}!A2:B")
        result: set[str] = set()
        for row in rows:
            url = str(row[0]).strip() if row else ""
            brand = str(row[1]).strip() if len(row) > 1 else ""
            if url or brand:
                result.add(f"{url.lower()}|{brand.lower()}")
        return result

    def existing_missing_category_keys(self) -> set[str]:
        rows = self.read_values(f"{quote_sheet('4_Missing Categories')}!B2:G")
        result: set[str] = set()
        for row in rows:
            url = str(row[0]).strip() if row else ""
            competitor = str(row[4]).strip() if len(row) > 4 else ""
            if url:
                result.add(f"{competitor.lower()}|{url.lower()}")
        return result

    def replace_sheet_rows(self, sheet: str, headers: Sequence[str], rows: Sequence[Sequence]) -> None:
        self.ensure_sheet(sheet, rows=max(1000, len(rows) + 20), cols=len(headers))
        self.clear_data(sheet)
        self.write_header(sheet, headers)
        self.append_rows(sheet, rows)

    @staticmethod
    def safe_monthly_label(label: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)[:40]

    def prepare_monthly_dump(self, label: str) -> None:
        """Clear the three monthly tabs once at the beginning of a run."""
        safe = self.safe_monthly_label(label)
        if not safe:
            return
        self.replace_sheet_rows(
            f"{safe}_URLs", SHEET_HEADERS["tech-dump_urls"], []
        )
        self.replace_sheet_rows(
            f"{safe}_Brands", SHEET_HEADERS["tech-dump_brands"], []
        )
        self.replace_sheet_rows(
            f"{safe}_Categories", SHEET_HEADERS["tech-dump_categories"], []
        )

    def append_monthly_dump(
        self,
        label: str,
        url_rows: Sequence[Sequence],
        brand_rows: Sequence[Sequence],
        category_rows: Sequence[Sequence],
    ) -> None:
        """Append one site's fresh rows without overwriting earlier sites."""
        safe = self.safe_monthly_label(label)
        if not safe:
            return
        if url_rows:
            self.append_rows(f"{safe}_URLs", url_rows)
        if brand_rows:
            self.append_rows(f"{safe}_Brands", brand_rows)
        if category_rows:
            self.append_rows(f"{safe}_Categories", category_rows)
