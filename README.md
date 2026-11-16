# Task 3 — Company Import and Impressum Enrichment

Dockerized Flask + Apache (`mod_wsgi`) + PostgreSQL service that

1. imports companies from a semicolon-separated CSV file,
2. exposes the stored companies through JSON endpoints,
3. enriches each company with legal data from its Impressum / Imprint page using the
   DeepSeek API.

The original Task 1 video endpoints are kept and still work.

## Architecture

- **web** — Apache + `mod_wsgi` serving `web_service.py`. Published on host port `8000`.
- **db** — PostgreSQL 16, persisted in the `db_data` named volume.
- **uploads** — named volume for Task 1 video uploads (`/app/uploads`, served under `/media/`).
- **schema.sql** — idempotent DDL for the `videos` and `companies` tables, applied when the
  web app initializes.
- **impressum.py** — page fetching, Impressum link discovery, visible-text extraction,
  token optimization, DeepSeek client.
- **enrich_companies.py** — standalone enrichment CLI that reads and updates PostgreSQL.

## Requirements

- Docker Desktop with Docker Compose v2.
- Python 3.9+ only for local unit tests or a host-side enrichment run (the container uses
  Python 3.11).
- A DeepSeek API key for enrichment.

## Quick start

```bash
docker compose up --build -d
```

This builds the image, starts PostgreSQL, waits for its healthcheck, and starts Apache on
`http://127.0.0.1:8000`. The WSGI app applies `schema.sql` idempotently when it initializes.

Check the legacy endpoint and the companies list:

```bash
curl http://127.0.0.1:8000/videos
curl http://127.0.0.1:8000/companies
```

On Windows PowerShell use `curl.exe` (the `curl` alias maps to `Invoke-WebRequest`).

## Configuration

Create `.env` in the project root (never commit it):

```dotenv
# Fixed inside Compose; host-side runs override these.
DB_HOST=db
DB_PORT=5432
DB_NAME=videos
DB_USER=video_user
DB_PASSWORD=video_pass
DEEPSEEK_API_KEY=<your key>
DEEPSEEK_MODEL=deepseek-chat
# Optional enrichment tuning (defaults shown)
MAX_TEXT_CHARS=8000
REQUEST_TIMEOUT=20
# USER_AGENT=Mozilla/5.0 (compatible; company-enrichment/1.0)
```

Docker Compose reads `.env` automatically. `DB_NAME`, `DB_USER`, and `DB_PASSWORD` are used
by both the `web` and `db` services; `DB_HOST` is always `db` and `DB_PORT` is always
`5432` inside the Compose network. `.env` also supplies `DEEPSEEK_API_KEY` /
`DEEPSEEK_MODEL` and the optional tuning variables `MAX_TEXT_CHARS` (default `8000`),
`REQUEST_TIMEOUT` (default `20`), and `USER_AGENT` (default: browser-like UA).

`.env` and `Task3.md` are excluded by `.gitignore` and `.dockerignore`, so secrets never
enter the image or the repository. `.env.example` contains only safe defaults and
placeholders.

DB credentials are applied when the `db_data` volume is first initialized. Changing them
afterwards requires recreating the volume or altering the PostgreSQL role manually.

## CSV import

Format (UTF-8, optionally with BOM, `;` delimiter, `"` quoting):

```csv
URL;Company Name;Language;Country;Postcode;City;Street;HouseNo;Web
https://nutrimmun.de;nutrimmun GmbH;German;Germany;;;;;https://nutrimmun.de
https://baroloandchampagne.com;Barolo & Champagne GbR;German;Germany;;;;;https://baroloandchampagne.com
```

- Open `http://127.0.0.1:8000/upload_companies` for the upload form, or use `curl`:

```bash
curl -F "file=@companies.csv" http://127.0.0.1:8000/upload_companies
```

- The first upload imports rows; later uploads update existing rows and insert new ones.
- Re-import is idempotent and updates only the columns present in the CSV header. A missing
  optional header preserves the existing DB value; a present but blank cell is stored as
  `NULL`. Enrichment results, `status`, `created_at`, and `updated_at` are preserved.
- `Company Name` and `Web` headers are required. The `Web` cell itself may be empty (a
  company selected for enrichment gets status `5`), but the column must exist. Re-import
  alone does not change an existing company's status.
- Rows without `Company Name` are skipped and logged; `total_rows` counts all parsed data
  rows, including skipped ones.
- `Web` may be given with or without a scheme. The importer trims surrounding whitespace
  and stores the value without adding a scheme; enrichment normalizes it before fetching.
- The legacy `URL` column is ignored (not stored).
- An empty file, a file whose header lacks `Company Name` or `Web` (for example one that uses
  `,` instead of `;` as the delimiter), invalid CSV, or non-UTF-8 input returns HTTP `400`
  with a JSON error. Nothing is imported in that case.

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/upload_companies` | HTML upload form |
| `POST` | `/upload_companies` | Import CSV (`file` field); returns `{"imported", "updated", "total_rows"}` |
| `GET` | `/companies` | List companies; optional `?status=N` |
| `GET` | `/companies/<company_name>` | One company or `404` |
| `GET` | `/videos` | Legacy: list uploaded videos |
| `POST` | `/upload` | Legacy: upload a video (`file`, `company_name`) |
| `GET` | `/get_video_url` | Legacy: video URL by `company_name` |
| `GET` | `/media/<filename>` | Legacy: serve an uploaded video |

Examples:

```bash
curl http://127.0.0.1:8000/companies
curl "http://127.0.0.1:8000/companies?status=0"
curl "http://127.0.0.1:8000/companies/nutrimmun%20GmbH"
```

## Status codes

| Value | Name | Meaning |
|---|---|---|
| `NULL` | pending | imported, not processed yet |
| `0` | `OK` | fields detected and address complete |
| `1` | `NO_IMPRESSUM_LINK_FOUND` | no Impressum/Imprint link on the home page |
| `2` | `PARSING_FAILED` | no text left on the Impressum page (DeepSeek is not called), no fields detected, or empty/unparseable DeepSeek content after retries |
| `3` | `ADDRESS_INCOMPLETE` | fields detected, but street/house/postcode/city incomplete |
| `4` | `ERROR` | transport, HTTP/auth, or malformed DeepSeek API envelope |
| `5` | `NO_WEBSITE` | empty or unusable `web` URL |

The status enum lives in `company_status.py` and is used by the enrichment script; the web
service stores and returns the integer values.

## Enrichment

Recommended: run inside the `web` container (DB host and API key are already configured).

```bash
docker compose exec web python enrich_companies.py --limit 2
docker compose exec web python enrich_companies.py --company "nutrimmun GmbH"
docker compose exec web python enrich_companies.py --status 3
```

Options:

- `--company NAME` — process only this exact `company_name`.
- `--limit N` — process at most N rows (positive integer).
- `--status N` — process only rows with this status.

When neither `--company` nor `--status` is given, the default selection is
`status IS NULL OR status <> 0`. Rows are processed in `id` order (`ORDER BY id`), as
required by the task; `--limit` only restricts the batch size and does not change the order.
`--company` processes that row regardless of its current status.

Pipeline per company:

1. Normalize `web` (prepend `https://` when the scheme is missing).
2. Fetch the home page with a browser-like User-Agent.
3. Find the Impressum/Imprint link (exact visible text first, `href` fallback).
4. Fetch the Impressum page, remove non-content tags including `<form>`, extract visible
   text.
5. Strip boilerplate and optimize the text for the token budget. If no text is left, the
   company gets status `2` and DeepSeek is not called.
6. Call DeepSeek (default `deepseek-chat`, JSON mode, `temperature=0`, retries with backoff).
7. On a valid DeepSeek response, store `impressum_url`, the normalized extracted fields,
   and the parsed JSON object returned by the model in `raw_json`; set `status` to `0`, `2`,
   or `3`. Blank or unparseable content after all retries sets `2`; transport/API failures
   set `4`, missing links `1`, empty or unusable websites `5`. Failure paths preserve
   previously extracted fields: statuses `1` and `5` also preserve `impressum_url`; status
   `2` (no text/unparseable) and status `4` once the Impressum URL is known update
   `impressum_url`. A valid response with no fields stores `raw_json = {}` and uses the CSV
   `company_name` as the `legal_name` fallback.
8. A failure for one company does not stop the batch.

Host-side run (optional; publish PostgreSQL port or use a tunnel):

```bash
export DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=videos DB_USER=video_user DB_PASSWORD=video_pass
export DEEPSEEK_API_KEY=<your key>
python enrich_companies.py --limit 5
```

PowerShell equivalent:

```powershell
$env:DB_HOST="127.0.0.1"; $env:DB_PORT="5432"; $env:DB_NAME="videos"
$env:DB_USER="video_user"; $env:DB_PASSWORD="video_pass"
$env:DEEPSEEK_API_KEY="<your key>"
python enrich_companies.py --limit 5
```

## Token optimization

Impressum pages often contain navigation, cookie banners, and footers. The script:

- removes boilerplate lines (`Cookie`, `Datenschutz`, `AGB`, `©`, menus, language switch);
- keeps a 200-character margin before the head anchor (`Impressum`, `Angaben gemäß § 5
  TMG/DDG`, `Anbieter`, `Diensteanbieter`) and cuts everything earlier;
- cuts everything after the last legal signal (phone, VAT id, e-mail, `HRB`/`HRA`,
  `Registergericht`, `Geschäftsführer`, `Verantwortlich`) plus a 400-character margin;
- verifies the result and falls back to the full text if no strong legal signal is found,
  or if the head anchor is missing from the cut;
- always applies a head+tail window when the resulting text is still longer than
  `MAX_TEXT_CHARS`, including fallback text, so the hard budget is never exceeded;
- logs method, character counts, and estimated tokens (`len(text) // 4`) against the
  original visible text before optimization.

## Tests

The suite currently contains 72 tests, with DeepSeek HTTP calls and the database mocked.
Unit tests require no running database, network, or API key. Tests for the web service and
the enrichment script need Flask and `psycopg2` installed and are skipped without them:

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install beautifulsoup4 lxml requests flask psycopg2-binary
.venv\Scripts\python -m unittest discover -s tests -v
# Linux/macOS
.venv/bin/pip install beautifulsoup4 lxml requests flask psycopg2-binary
.venv/bin/python -m unittest discover -s tests -v
```

Inside Docker:

```bash
docker compose exec web python -m unittest discover -s tests -v
```

## Troubleshooting

- **Port 8000 is busy** — stop the other project or change the `web` port mapping.
- **`Could not connect to the database`** — check `docker compose ps` and wait for the `db`
  healthcheck; `init_db()` retries for 20 seconds.
- **DeepSeek authorization failed** — verify `DEEPSEEK_API_KEY` in `.env` and recreate the
  container with `docker compose up -d --force-recreate web`.
- **No Impressum link found** — the site may render navigation with JavaScript; the HTML
  fallback cannot see it. Such companies get `status = 1`.
- **Import does not change status** — expected: re-import preserves enrichment data.

## Project structure

```text
Task3/
├── .env.example
├── apache-config.conf
├── companies.csv
├── company_status.py
├── docker-compose.yml
├── Dockerfile
├── enrich_companies.py
├── impressum.py
├── README.md
├── requirements.txt
├── schema.sql
├── tests/
│   ├── __init__.py
│   ├── test_enrich_companies.py
│   ├── test_impressum.py
│   └── test_web_service.py
├── web_service.py
├── web_service.wsgi
└── INTERNSHIP_TASK_3.txt  # local task text, ignored by git and Docker
```

## Data persistence

Named volumes `db_data` and `uploads` survive `docker compose down`. Use
`docker compose down -v` only when you intentionally want to delete all data.
