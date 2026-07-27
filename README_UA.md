# Gamblizard ES/FR — URL Export Only

Ця версія виконує лише перший етап:

1. Читає sitemap та sitemap index.
2. За потреби використовує Playwright тільки для відкриття заблокованого sitemap-файла.
3. Фільтрує URL за `Tech part`: Brand URL patterns, Category URL patterns, Include URL Regex, Exclude URL Regex.
4. Записує потрібні URL у `tech-dump_urls` і `tech-auto_url_cache`.
5. У режимі `MONTHLY_FRESH` створює/оновлює вкладку `YYYY-MM_ES_URLs` або `YYYY-MM_FR_URLs` і бере лише URL з `lastmod` обраного місяця.

## Що ця версія НЕ робить

- не відкриває кожну сторінку;
- не завантажує HTML сторінок;
- не шукає H1, bonus або ref link;
- не формує MATCH/MISSING Brands/Categories;
- не очищає вкладки Brands, Categories або Missing.

## Файли для GitHub

Замініть увесь вміст репозиторію файлами з цього пакета. Критичні файли:

- `.github/workflows/gamblizard-parser.yml`
- `parser/main.py`
- `parser/fetcher.py`
- `parser/models.py`
- `parser/rules.py`
- `parser/sheets_client.py`
- `requirements.txt`
- `run_parser.py`

## Apps Script

- ES: `apps_script/Code_ES.gs`
- FR: `apps_script/Code_FR.gs`

Назва workflow-файла не змінилася: `gamblizard-parser.yml`.
Токен і GitHub-конфігурацію повторно вводити не потрібно після простої заміни коду Apps Script.
