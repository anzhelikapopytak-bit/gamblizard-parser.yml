# Gamblizard ES + FR automated parser

This package replaces the old long-running Apps Script parser with:

- two independent Google Sheets (one ES, one FR);
- one Apps Script controller in each spreadsheet;
- one shared GitHub Actions repository;
- Python + requests + Playwright for sitemap and HTML access;
- automatic write-back to the spreadsheet;
- MONTHLY_FRESH Apps Script triggers and a 5-minute completion poll;
- raw sitemap CSV backups in GitHub Artifacts.

## Correct directions

- ES: `https://gamblizard.com/es`
- FR: `https://gamblizard.ca/fr` (French-speaking Canada)

## Files

- `.github/workflows/gamblizard-parser.yml` — GitHub workflow.
- `parser/` — Python parser.
- `apps_script/Code_ES.gs` — paste into the ES spreadsheet Apps Script.
- `apps_script/Code_FR.gs` — paste into the FR spreadsheet Apps Script.
- `config/Tech_part_ES.csv` and `config/Tech_part_FR.csv` — reference source settings.

## GitHub installation

1. Upload the package contents to one GitHub repository, preserving folders.
2. Create a Google Cloud service account and enable Google Sheets API.
3. Share both Google spreadsheets with the service-account email as Editor.
4. In GitHub repository: `Settings → Secrets and variables → Actions`.
5. Create secret `GOOGLE_SERVICE_ACCOUNT_JSON` containing the complete JSON key.
6. Keep the repository public if using the free public-repository runner allowance.

## Google Sheet installation

### ES spreadsheet

Open `Extensions → Apps Script`, replace the code with `apps_script/Code_ES.gs`, save, and reload the spreadsheet.

### FR spreadsheet

Use `apps_script/Code_FR.gs` in its separate Apps Script project.

In each spreadsheet:

1. `00. Setup / update structure`.
2. `01. Configure GitHub` and enter owner, repository, branch, and fine-grained token.
3. `09. Test GitHub connection`; HTTP 200 means the workflow is visible.
4. `02. Run now`.
5. `06. Install monthly trigger` when manual testing succeeds.

The token is stored in that spreadsheet's Script Properties, so ES and FR remain independent.

## Automation

- ES default suggestion: day 2 at about 03:00 spreadsheet time.
- FR default suggestion: day 3 at about 04:00.
- The actual day and hour are entered when installing the trigger.
- Apps Script dispatches GitHub and writes `QUEUED` into `tech-auto_status`.
- GitHub updates progress and final status directly through Google Sheets API.
- Apps Script polls every five minutes and removes the polling trigger after a final state.
- Optional completion email is configured together with GitHub credentials.

## Source fetch mode

Column `Fetch Mode` in `Tech part`:

- `AUTO`: requests first; Playwright only when required.
- `GITHUB`, `BROWSER`, or `PLAYWRIGHT`: use Chromium immediately.

Legalbet, Casasdeapuestas and Tribuna ES are preset for browser mode. Tribuna uses only:

- `https://tribuna.com/sitemap/es-casas-de-apuestas-1.xml`
- `https://tribuna.com/sitemap/es-casino-1.xml`

This prevents loading the `persons`, Arabic, English and other irrelevant sitemap branches.

## Results

Working results are written to:

- `1_ MAIN Brands`
- `2_Missing Brands`
- `3_Categories`
- `4_Missing Categories`

Technical data and progress are written to the existing `tech-*` sheets. Full raw and candidate URL CSV files remain in the GitHub run artifact, so very large sitemap exports do not overload Google Sheets.

## Safe behavior

`Setup / update structure` does not clear working results. It adds missing columns and default sources, fills blank settings, corrects the FR Gamblizard URL, and replaces the broad Tribuna root sitemap with two relevant ES sitemap files.


## Universal v2 and monthly freshness

The GitHub code is shared by ES and FR. Each Apps Script sends `geo`, `spreadsheet_id`, `run_id`, `run_mode`, and the current `YYYY-MM`. Python reads the current `Tech part` directly from the target spreadsheet, so source URLs and rules do not need to be duplicated in GitHub.

Run modes:

- `FULL`: clears and rebuilds the full working output.
- `MONTHLY_FRESH`: processes only candidate URLs whose sitemap `lastmod` belongs to the selected month. It creates or rebuilds `{YYYY-MM}_{GEO}_URLs`, `{YYYY-MM}_{GEO}_Brands`, and `{YYYY-MM}_{GEO}_Categories`. The three monthly tabs are cleared once per run and then all sites are appended, so later competitors no longer overwrite earlier competitors.
- `RETRY_FAILED`: reruns sources marked ERROR/FAILED/PARTIAL in `Tech part`.

URLs without sitemap `lastmod` are skipped in `MONTHLY_FRESH` and remain available in `FULL`. Existing OUR brand/category maps are loaded from technical dumps before a monthly run, so fresh competitor pages are compared against the full previously known Gamblizard inventory.
