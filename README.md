# HVAC Business Lead & Review Scraper

A two-stage data pipeline for HVAC businesses across the US:

1. **Lead Scraper** (`main.py` + helpers) — finds HVAC businesses via Google Search, visits their websites, and uses AI to extract structured business data (name, phone, email, address, services, logo) into a CSV.
2. **Review Scraper** (`review/`) — takes that business list and collects **ratings and reviews** from Google Maps, Yelp, BBB, Angi, and HomeAdvisor into a PostgreSQL database (and a flattened CSV export).

A third helper, **`fill_missing.py`**, backfills empty fields in an existing business CSV.

---

## Table of Contents

- [Part 1 — Lead Scraper](#part-1--lead-scraper)
  - [Features](#features)
  - [Architecture](#architecture)
  - [Output CSV columns](#output-csv-columns)
  - [Setup](#setup)
  - [Usage & resume](#usage--resume)
  - [Configuration](#configuration-configpy)
  - [`fill_missing.py` — backfill helper](#fill_missingpy--backfill-helper)
- [Part 2 — Review Scraper (`review/`)](#part-2--review-scraper-review)
  - [What it does](#what-it-does)
  - [Architecture & execution model](#architecture--execution-model)
  - [Per-site scrapers](#per-site-scrapers)
  - [The Yelp problem & 3-layer strategy](#the-yelp-problem--3-layer-strategy)
  - [Anti-block infrastructure](#anti-block-infrastructure)
  - [Storage: PostgreSQL](#storage-postgresql)
  - [Progress & resume](#progress--resume)
  - [Configuration](#configuration-reviewconfigpy)
  - [Running the review scraper](#running-the-review-scraper)
  - [`review/` file reference](#review-file-reference)
- [Project structure](#project-structure)
- [Requirements](#requirements)
- [License](#license)

---

# Part 1 — Lead Scraper

A multi-threaded Google search scraper that finds HVAC businesses, visits their websites, and uses AI to extract structured business data. Outputs everything to a single CSV.

## Features

- **Multi-threaded** — Run N workers in parallel (`--workers 10`), each scraping a different keyword+location+page combo simultaneously.
- **Crash-safe resume** — Stop anytime (Ctrl+C) and restart later, even with a different number of workers. No progress lost, no data duplicated.
- **13 AI providers with auto-failover** — Uses free-tier LLM APIs (Groq, Cerebras, SambaNova, Together, OpenRouter, NVIDIA, GitHub Models, Mistral). Configured as **15 slots** (some providers have multiple accounts for load-balancing). When one hits a rate limit, it rotates to the next.
- **SOCKS5 & HTTP proxy support** — Rotates proxies per request. Auto-creates a local HTTP bridge for SOCKS5 proxies (Chrome doesn't support SOCKS5 auth natively).
- **Google bot-detection handling** — Detects CAPTCHAs/blocks and retries with exponential backoff (up to 7 attempts). Combos blocked ≥4 of 7 times are marked for retry on the next run instead of being skipped.
- **Smart URL filtering** — Skips social media, directories, manufacturers, job sites, `.gov`, and ~70 other non-business domains.
- **Contact-page discovery** — Automatically finds and scrapes `/contact`, `/about`, and similar pages for more complete data.
- **Token-efficient AI** — Regex pre-extraction of emails/phones, BeautifulSoup cleaning (80–90% token reduction), and MD5 content-hash caching to avoid re-sending identical pages.

## Architecture

```
input.csv (keywords × locations)
        │
        ▼
┌─────────────────────────────────────┐
│            main.py                  │
│  ThreadPoolExecutor (N workers)     │
│  Streams work units on demand:      │
│  (keyword, location, page)          │
└───────┬─────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│        google_scraper.py            │
│  SeleniumBase + undetected Chrome   │
│  Runs in subprocess (180s timeout)  │
│  CAPTCHA/block detection + retry    │
│  Proxy rotation + SOCKS5 bridge     │
└───────┬─────────────────────────────┘
        │ list of URLs (deduped by domain)
        ▼
┌─────────────────────────────────────┐
│       website_extractor.py          │
│  requests (fast): homepage +        │
│  contact page, clean text + logo    │
└───────┬─────────────────────────────┘
        │ HTML text
        ▼
┌─────────────────────────────────────┐
│        ai_extractor.py              │
│  Regex pre-extract → BS4 clean →    │
│  junk filter → hash cache → LLM     │
│  13 providers / 15 slots, failover  │
└───────┬─────────────────────────────┘
        │ structured data
        ▼
    output.csv (thread-safe append)
    progress.json (atomic save)
    content_cache.json (AI dedup)
```

## Output CSV columns

| Column | Description |
|---|---|
| `keyword` | Search keyword used |
| `location` | Location searched |
| `google_page` | Google results page number |
| `business_name` | Extracted business name |
| `website_url` | Business website URL |
| `business_description` | What the business does |
| `services_offered` | List of services |
| `contact_phone` | Phone number(s) |
| `contact_email` | Email address(es) |
| `address` | Full street address |
| `business_city` | City |
| `business_state` | State |
| `supply_location` | Geographic areas the business serves |
| `logo_url` | URL to business logo |

## Setup

### 1. Clone & install

```bash
git clone https://github.com/SafeerAbbas624/HVAC_USA_Scraper.git
cd HVAC_USA_Scraper
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure AI API keys

```bash
cp .env.example .env
```

Edit `.env` and add at least one key (all free-tier):

| Provider | Free Tier | Get Key |
|---|---|---|
| Groq | 30 req/min, 14,400/day | https://console.groq.com/keys |
| Cerebras | 30 req/min | https://cloud.cerebras.ai/ |
| SambaNova | Free tier | https://cloud.sambanova.ai/ |
| Together AI | $5 free credit | https://api.together.ai/ |
| OpenRouter | Some free models | https://openrouter.ai/ |
| NVIDIA | Free tier | https://build.nvidia.com/ |
| GitHub Models | 15 req/min, 150/day | https://github.com/marketplace/models |
| Mistral | Free tier | https://console.mistral.ai/ |

More keys = more throughput. The scraper rotates across all configured providers; you can add a second/third account per provider (e.g. `GROQ_API_KEY_2`, `GITHUB_TOKEN_3`) for extra slots.

### 3. Configure proxies (optional but recommended)

Create `proxies.txt` with one proxy per line:

```
# HTTP proxy
http://user:pass@host:port

# SOCKS5 proxy (auto-bridged for Chrome)
socks5://user:pass@host:port
```

> **Note:** Datacenter proxies get blocked by Google quickly. Use **residential rotating proxies** for best results. `proxies.txt` is gitignored because it contains credentials.

### 4. Configure keywords and locations

Edit `input.csv`:

```csv
keywords,locations
HVAC repair,"New York, NY"
AC installation,"Los Angeles, CA"
furnace service,"Chicago, IL"
```

The scraper generates the cartesian product: every keyword × every location.

## Usage & resume

```bash
# Default workers (10)
python main.py

# Custom worker count
python main.py --workers 20

# Graceful stop: Ctrl+C once (finishes current URLs, saves progress)
# Force stop: Ctrl+C twice
```

Resume by re-running the same command with **any** worker count — progress is tracked per `(keyword, location, page)` combo, not per worker:

```bash
python main.py --workers 5    # run, then Ctrl+C
python main.py --workers 10   # resumes exactly where it left off
```

`progress.json` is saved atomically (write to temp file → `os.replace`) so it's never corrupted, even on crash. It records `completed_combos` (won't retry), `failed_combos` (blocked — retried next run), and `processed_urls` (never re-visited).

## Configuration (`config.py`)

| Setting | Default | Description |
|---|---|---|
| `NUM_WORKERS` | 10 | Default parallel worker threads (`--workers` overrides) |
| `MAX_GOOGLE_PAGES` | 50 | Google result pages per combo |
| `RESULTS_PER_PAGE` | 10 | Results per Google page |
| `PAGE_LOAD_TIMEOUT` | 30 | Seconds to wait for page loads |
| `REQUEST_DELAY_MIN` / `MAX` | 3 / 7 | Random delay between website visits (secs) |
| `WEBSITE_VISIT_TIMEOUT` | 20 | Timeout for visiting target sites |
| `MAX_RAW_HTML_LENGTH` | 200 KB | Cap on raw HTML fetched |
| `MAX_HTML_LENGTH` | 5000 | Max chars of cleaned text sent to the AI |
| `RATE_LIMIT_COOLDOWN` | 120 | Base cooldown after a provider rate-limits |
| `MAX_PROVIDER_COOLDOWN` | 300 | Cap for progressive provider backoff |
| `EXCLUDED_DOMAINS` | ~70 | Social/directory/manufacturer/.gov domains to skip |
| `CONTACT_PATHS` | 10 | Contact/about paths tried per site |

All AI providers are OpenAI-compatible chat/completions endpoints. Some support native JSON `response_format`; OpenRouter does not (markdown is stripped) and uses `verify_ssl=False` plus referer headers.

## `fill_missing.py` — backfill helper

Fills empty fields in an existing business CSV (default `hvac_companies_cleaned.csv`). For each row that has a `website_url` but is missing ≥1 target field, it visits the site with SeleniumBase (UC mode), runs the same AI extraction pipeline, and **conservatively merges** results — only empty fields are filled, existing data is never overwritten.

```bash
python fill_missing.py --workers 5          # backfill all incomplete rows
python fill_missing.py --test               # process only the first 2 rows
python fill_missing.py --csv path/to.csv    # custom input CSV
```

The CSV is saved atomically after each row; progress is tracked separately in `fill_missing_progress.json`.

---

# Part 2 — Review Scraper (`review/`)

## What it does

Given a CSV of HVAC businesses (default: `hvac_companies_cleaned.csv` at the project root), the review scraper collects **ratings** and up to **100 reviews per site** from five platforms:

| Site | What's collected | Reviews? |
|---|---|---|
| **Google Maps** | Star rating + review cards | ✅ up to 100 |
| **Yelp** | Rating + reviews (via dataset/API — see below) | ✅ up to 100 (dataset) / 3 (API) |
| **BBB** | Letter grade mapped to a 0–5 numeric rating | ❌ rating only |
| **Angi** | Rating + reviews | ✅ up to 100 |
| **HomeAdvisor** | Rating + reviews | ✅ up to 100 |

Results are written to **PostgreSQL** (the source of truth) and exported to a flattened CSV at the end of a run.

> The module docstring lists 17 candidate sites (Houzz, Thumbtack, Trustpilot, etc.); only the 5 above are active. The others live as disabled stubs in [`archived_files/disabled_sites/`](archived_files/disabled_sites/).

## Architecture & execution model

The scraper runs in **two phases**:

```
hvac_companies_cleaned.csv
        │  read_input_csv()
        ▼
┌──────────────────────────────────────────────────────────────┐
│  PHASE 1 — process_rows()                                      │
│  ThreadPoolExecutor: MAX_PARALLEL_ROWS (10) rows at once       │
│                                                                │
│   each row → process_row():                                    │
│     ThreadPoolExecutor: SITES_PER_ROW (5) sites in parallel    │
│       each site → run_with_retries() → child Chrome process    │
│         search() → scrape() → {status, rating, reviews}        │
│     backfill business_name (gmaps>bbb>angi>homeadvisor>yelp)   │
│     upsert_full_row() → Postgres                               │
│                                                                │
│   → up to 10 × 5 = 50 concurrent Chrome subprocesses           │
└───────────────────────────────┬────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│  PHASE 2 — retry_blocked_pass()  (up to RETRY_MAX_PASSES = 3)  │
│   collect rows with blocked/error sites                        │
│   reset those sites → re-run ONLY them → upsert_site_partial() │
│   sleep RETRY_INTERPASS_SLEEP (60s) between passes             │
└───────────────────────────────┬────────────────────────────────┘
                                 ▼
        export_to_csv()  →  review_output.csv
```

**Status model** — every (row, site) ends in one of:

- `done` / `not_found` — **terminal** (never retried)
- `blocked` / `error` — **retriable** (re-run in Phase 2)
- `pending` — not yet attempted

**Timeouts:** each site attempt runs in its own Chrome subprocess with a hard wall-clock cap of `SITE_HARD_TIMEOUT` (120s); a row stops waiting after `ROW_HARD_TIMEOUT` (2400s = 40 min) and harvests whatever finished. A hung Chrome is force-killed. The row timeout doesn't kill threads — it just stops waiting and keeps the finished results.

**Graceful shutdown:** `SIGINT`/`SIGTERM` flush the progress tracker atomically, stop the cleanup daemon, and exit. A global `_shutdown` flag short-circuits new work.

**Throughput** (measured on an 18-core box): 4 rows → ~37 rows/h, **8 rows → ~113 rows/h**, 12 rows → ~73 rows/h (too much CPU contention). `MAX_PARALLEL_ROWS = 10` is the tuned default.

## Per-site scrapers

Every scraper subclasses `SiteScraper` (in [`base_scraper.py`](review/base_scraper.py)) and implements two methods:

- `search(sb, row) -> url | None` — locate the business's profile page
- `scrape(sb, profile_url) -> dict` — extract `{rating, reviews, business_name}`

Class attributes control behavior: `SITE_ID`, `DOMAIN`, `HAS_REVIEWS`, `MAX_REVIEWS` (default 100), `MAX_RETRIES`, and `HEAVY_BLOCKER`. The base `run()` normalizes every return into `{status, rating, reviews}`.

| Scraper | `HAS_REVIEWS` | `HEAVY_BLOCKER` | Retries | How it works |
|---|---|---|---|---|
| **google_maps** | ✅ | — | 5 | Selenium UC mode. Finds the place via `/maps/search/`, rewrites the URL with the `!9m1!1b1` deep-link to force the **Reviews tab**, scrolls (up to 60 iterations), expands "More" buttons, extracts cards (`div.jftiEf`). |
| **bbb** | ❌ | ✅ | 10 | CDP mode. Searches `bbb.org/search`, opens the profile, reads the **letter grade** and maps it (`A+`→5.0 … `F`→1.0). No individual reviews. |
| **angi** | ✅ | ✅ | 10 | Finds `/companylist/...-reviews-{id}.htm` profiles via DuckDuckGo, opens in CDP mode (Cloudflare-resistant), extracts `LocalBusiness` JSON-LD with CSS/regex fallback. ~30% of requests are Cloudflare-blocked → handled by retries. |
| **homeadvisor** | ✅ | ❌ | 5 | Finds `/rated.*` profiles via DuckDuckGo, opens in CDP, extracts JSON-LD. `HEAVY_BLOCKER=False` on purpose — most HVAC firms simply have no HA profile, so a null result is usually correct, not a transient block (avoids wasting retries). |
| **yelp** | ✅ | ❌ | 2 | **No web scraping** — see below. |

**Discovery helpers** (`sites/_common.py`): DuckDuckGo HTML search with Bing fallback, JSON-LD parsing (`ratingValue` + review array), CDP page open/scroll/eval, fuzzy `name_match()`, and `domain_apex()` (used as a search term when a row has no `business_name`).

## The Yelp problem & 3-layer strategy

Yelp's edge layer (**DataDome**) returns **403 to every datacenter / shared-proxy IP** — verified on 2026-04-29 across 30+ attempts (UC mode, CDP, 5 `curl_cffi` TLS fingerprints, `api.yelp.com`, `m.yelp.com`, `/robots.txt`, Wayback, SERP, and Google Maps). So `yelp.py` never scrapes the website. Instead, `scrape()` runs three fallback layers:

```
Layer 1 — Yelp Academic Open Dataset (offline, free, no key)
  1a  get_yelp_data_fast()  → O(1) lookup in prebuilt yelp_matches.json
  1b  get_yelp_data()       → load full index, exact then fuzzy match
  Coverage: ~160K businesses, ~7M reviews (MA/OR/TX/FL/GA/BC/OH/CO/WA)
  Hit rate on a nationwide HVAC list: ~0.5%, but matches are 100% real
  and return up to 100 reviews each.
        │ miss
        ▼
Layer 2 — Yelp Fusion API (online, free 5000 calls/day, needs YELP_API_KEY)
  /businesses/matches → /businesses/{id} → /businesses/{id}/reviews
  Nationwide, but the free tier returns only ~3 reviews per business.
  Calls go through curl_cffi (impersonate=chrome124), not Selenium.
        │ miss / no key
        ▼
Layer 3 — NOT_FOUND (terminal)
```

### The Yelp dataset loader (`yelp_dataset_loader.py`)

A standalone module that builds and queries the offline dataset.

- **Matching:** exact key lookup on `STATE|city-slug|normalized_name`, then a fuzzy **Jaccard token-overlap** scan within the same city (threshold **0.7**). Matches that overlap *only* on generic HVAC words (`heating`, `cooling`, `air`, `hvac`, `service`, …, an 18-word stoplist) are rejected.
- **Files** (all under `review/yelp_dataset/`, gitignored — downloaded from archive.org):

  | File | Size | Role |
  |---|---|---|
  | `yelp_academic_dataset_business.json.gz` | ~22 MB | Source business records |
  | `yelp_academic_dataset_review.json.gz` | ~2.86 GB | Source reviews |
  | `yelp_business_index.json` | ~46 MB | Built business index |
  | `yelp_reviews_cache.json` | ~2 MB | Reviews for matched businesses (≤100 each) |
  | `yelp_matches.json` | ~2 MB | Prebuilt O(1) row→result lookup |

- **CLI:**
  ```bash
  # One-time: match your CSV to the dataset and build the fast caches
  python -m review.yelp_dataset_loader build hvac_companies_cleaned.csv
  # Inspect dataset stats (paths, index size, top states)
  python -m review.yelp_dataset_loader stats
  ```
  Building the review cache streams the 2.86 GB file once (~10 min). Missing dataset files degrade gracefully → the scraper falls through to the API or NOT_FOUND.

To enable Layer 2, get a free key at <https://www.yelp.com/developers/v3/manage_app> and add `YELP_API_KEY=...` to `.env`.

## Anti-block infrastructure

| Component | Responsibility |
|---|---|
| [`browser.py`](review/browser.py) | Runs each site attempt in a **child process** with `SeleniumBase` (UC mode + CDP, headed under an Xvfb virtual display). Caps V8 heap (`--max-old-space-size=512`), randomizes user-agent/window size, staggers startup, kills hung Chrome after `SITE_HARD_TIMEOUT`, and drives the retry loop with exponential backoff `min(3·2^(n-1), 30)s`. |
| [`proxies.py`](review/proxies.py) | Thread-safe proxy rotation. `pick_proxy(exclude=…)` avoids reusing a proxy across concurrent retries (falls back to any proxy if all are excluded). SOCKS5 proxies are bridged to local HTTP. A **SOCKS5 auth failure rotates the proxy without consuming a retry attempt.** |
| [`block_detect.py`](review/block_detect.py) | Heuristic block detection: checks the URL (`/sorry`, `captcha`, `challenge`), title, and first 8 KB of body against `BLOCK_SIGNAL_STRINGS` (19 signals — Cloudflare, PerimeterX, DataDome, reCAPTCHA, …), plus a `MIN_VALID_BODY_BYTES` (1024) size floor and a 404/empty check. |
| [`cleanup_daemon.py`](review/cleanup_daemon.py) | Every `MEM_CLEANUP_INTERVAL` (300s): `gc.collect()`, drops the Linux page cache, and reaps **orphaned** Chrome/Xvfb processes (`ppid==1`) — active workers' children are never touched. |

> ⚠️ The scraper needs **residential proxies** in `proxies.txt`. With no proxies it warns and hits sites from the local IP, which blocks within minutes.

## Storage: PostgreSQL

`db.py` uses a **flat schema** — one `reviews` row per business, matching the CSV 1:1 for trivial export.

- **Table `reviews`:** `row_idx` (PK), the input columns (`business_name`, `website_url`, `address`, `business_city`, `business_state`, `supply_location`, `logo_url`, `contact_phone/email`, …), then per-site columns — `*_rating` (real) and `*_reviews` (JSONB) — plus `created_at` / `updated_at`. BBB has only a rating column. Indexes on `business_name` and `(business_state, business_city)`.
- **Concurrency:** a `ThreadedConnectionPool` (1–16 connections). All writes are `INSERT … ON CONFLICT (row_idx) DO UPDATE`, so up to ~50 concurrent workers can write safely.
  - `upsert_full_row()` — initial write of a whole row (Phase 1)
  - `upsert_site_partial()` — updates only the retried sites' columns, preserving prior successes (Phase 2)
  - `update_business_name_if_empty()` — backfills a discovered name without clobbering
  - `import_csv()` / `export_to_csv()` — migrate a legacy CSV in, or dump the table out (10 MB cell limit for large review JSON)

### Configure Postgres

Add to `.env`:

```bash
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=hvac_reviews
POSTGRES_USER=hvac
POSTGRES_PASSWORD=your_password_here
```

Create the database and let the scraper build the schema on first run:

```bash
createdb hvac_reviews        # or: CREATE DATABASE hvac_reviews;
python -m review.runner --rows 5   # init_schema() runs automatically
```

## Progress & resume

`progress.py` maintains `review_progress.json`:

```json
{
  "version": 1,
  "rows": {
    "0": {
      "business_name": "Century A/C Supply",
      "sites": {
        "google_maps": {"status": "done",      "attempts": 1},
        "yelp":        {"status": "not_found",  "attempts": 1},
        "bbb":         {"status": "blocked",    "attempts": 3},
        "angi":        {"status": "done",       "attempts": 2},
        "homeadvisor": {"status": "done",       "attempts": 1}
      },
      "row_complete": false
    }
  }
}
```

- Written atomically (temp file → `os.replace`) with a **5-second debounce**, and force-flushed on shutdown.
- `collect_retry_targets()` finds rows with `blocked`/`error` sites for Phase 2; `reset_site()` sets them back to `pending` while keeping the `attempts` counter.
- On restart, rows already `done`/`not_found` are skipped — the scraper only re-runs what's incomplete.

## Configuration (`review/config.py`)

| Setting | Default | Description |
|---|---|---|
| `MAX_REVIEWS_PER_SITE` | 100 | Hard cap on reviews captured per site |
| `SITES_PER_ROW` | 5 | Sites scraped in parallel per row |
| `MAX_PARALLEL_ROWS` | 10 | Rows processed concurrently (→ up to 50 Chrome procs) |
| `ROW_HARD_TIMEOUT` | 2400 | Per-row wall-clock cap (40 min) |
| `SITE_HARD_TIMEOUT` | 120 | Per-site Chrome subprocess cap |
| `DEFAULT_MAX_RETRIES` | 5 | Per-site attempts for normal sites |
| `HEAVY_BLOCKER_RETRIES` | 10 | Per-site attempts for Yelp/BBB/Angi |
| `WORKER_STAGGER_SECONDS` | (0, 5) | Random per-site startup delay |
| `RECONNECT_TIME_RANGE` | (5, 9) | `uc_open_with_reconnect` range |
| `MEM_CLEANUP_INTERVAL` | 300 | Orphan Chrome/Xvfb reaper interval |
| `RETRY_MAX_PASSES` | 3 | Phase-2 retry sweeps |
| `RETRY_INTERPASS_SLEEP` | 60 | Seconds between retry passes |
| `BLOCK_SIGNAL_STRINGS` | 19 strings | Substrings that signal a block |
| `MIN_VALID_BODY_BYTES` | 1024 | Min body size to consider a page valid |

## Running the review scraper

### Direct

```bash
python -m review.runner                 # full run over the input CSV
python -m review.runner --rows 50        # first 50 rows (testing)
python -m review.runner --start 1000     # resume from row index 1000
python -m review.runner --retry-passes 5 # more Phase-2 sweeps
```

CLI flags: `--rows N`, `--start N`, `--input PATH`, `--output PATH`, `--progress PATH`, `--log-file PATH`, `--retry-passes N` (defaults come from `config.py`).

### Production (survives SSH disconnects)

[`run_production.sh`](review/run_production.sh) wraps the runner in a detached **tmux** session:

```bash
bash review/run_production.sh            # launch in tmux session 'review-scraper'
bash review/run_production.sh status     # session state, last 20 log lines,
                                         #   rows tracked/complete, live Chrome count
bash review/run_production.sh stop        # SIGTERM the runner, wait 5s, kill session
tmux attach -t review-scraper            # watch live logs
```

Logs go to `review/review.log`. Progress, output CSV, and logs all live under `review/`.

## `review/` file reference

| File | Purpose |
|---|---|
| `runner.py` | Orchestrator — Phase 1 (parallel rows), Phase 2 (retry sweep), signals, CSV export |
| `config.py` | All review-scraper config: paths, concurrency, timeouts, retries, block signals, logging |
| `run_production.sh` | tmux launcher with `status` / `stop` subcommands |
| `base_scraper.py` | `SiteScraper` base class + status constants (`done`/`not_found`/`blocked`/`error`/`pending`) |
| `browser.py` | Multiprocess Chrome runner, UC/CDP setup, proxy bridging, retry loop |
| `proxies.py` | Thread-safe proxy loading & rotation |
| `block_detect.py` | Cloudflare/PerimeterX/DataDome/CAPTCHA + 404 detection |
| `cleanup_daemon.py` | Orphan Chrome/Xvfb reaper, cache drop, GC |
| `db.py` | PostgreSQL schema + upserts + CSV import/export |
| `progress.py` | `review_progress.json` tracker (debounced, atomic, resume) |
| `csv_io.py` | Input reader + thread-safe output append / in-place row merge |
| `json_utils.py` | Review (de)serialization for CSV cells and JSONB |
| `sites/__init__.py` | `SITE_REGISTRY`, `ALL_SITE_IDS`, `get_scraper()` |
| `sites/_common.py` | Shared discovery (DDG/Bing), CDP helpers, JSON-LD, fuzzy matching |
| `sites/google_maps.py`, `yelp.py`, `bbb.py`, `angi.py`, `homeadvisor.py` | The five active scrapers |
| `yelp_dataset_loader.py` | Offline Yelp dataset builder/lookups + CLI |
| `_harvest*.py`, `_inspect*.py`, `_probe.py`, `_smoke.py`, `_verify_selectors.py` | Dev/debug tools used to reverse-engineer selectors (not part of a normal run) |

> **Not committed** (see `.gitignore`): `review/yelp_dataset/` (the multi-GB dataset), `review/review.log`, `review_progress.json`, output CSVs, DB dumps, and `.tmp`/`.bak` files. Download the Yelp dataset and let the runtime files regenerate.

---

## Project structure

```
├── main.py                 # Lead scraper orchestrator (thread pool, CSV output, shutdown)
├── google_scraper.py       # Google SERP scraping (SeleniumBase UC + CAPTCHA detection)
├── website_extractor.py    # Visits sites, finds contact pages, extracts text + logo
├── ai_extractor.py         # Multi-provider AI extraction with auto-failover
├── progress_tracker.py     # Thread-safe, crash-safe progress for the lead scraper
├── url_generator.py        # Builds Google search URLs (UULE geo-targeting)
├── socks_bridge.py         # Local HTTP→SOCKS5 bridge for Chrome
├── fill_missing.py         # Backfills empty fields in an existing business CSV
├── config.py               # Lead-scraper config (AI providers, timeouts, paths, domains)
├── input.csv               # Input keywords × locations
├── requirements.txt        # Python dependencies
├── .env.example            # Template for API keys + Postgres + Yelp settings
├── archived_files/
│   └── disabled_sites/     # Stubs for the 12 inactive review sites
└── review/                 # ── Review Scraper subsystem (see Part 2) ──
    ├── runner.py
    ├── config.py
    ├── run_production.sh
    ├── base_scraper.py  browser.py  proxies.py  block_detect.py  cleanup_daemon.py
    ├── db.py  progress.py  csv_io.py  json_utils.py
    ├── yelp_dataset_loader.py
    └── sites/
        ├── __init__.py  _common.py
        └── google_maps.py  yelp.py  bbb.py  angi.py  homeadvisor.py
```

## Requirements

- **Python 3.8+**
- **Chrome** (SeleniumBase auto-downloads ChromeDriver) + **Xvfb** (virtual display for UC mode)
- **PostgreSQL** (for the review scraper)
- At least one **AI provider API key** (for the lead scraper)
- Extra Python deps used by the review scraper: `psycopg2` (PostgreSQL) and `curl_cffi` (Yelp API with TLS impersonation)
- (Optional) **residential rotating proxies**, and a **Yelp Fusion API key** for live Yelp data

## License

MIT
