# HVAC Business Lead Scraper

A multi-threaded Google search scraper that automatically finds HVAC businesses across the US, visits their websites, and uses AI to extract structured business data (name, phone, email, address, services, logo). Outputs everything to a single CSV file.

## Features

- **Multi-threaded** — Run N workers in parallel (`--workers 10`), each scraping a different keyword+location+page combo simultaneously
- **Crash-safe resume** — Stop anytime (Ctrl+C) and restart later, even with a different number of workers. No progress is lost, no data is duplicated
- **13 AI providers with auto-failover** — Uses free-tier LLM APIs (Groq, Cerebras, SambaNova, Together, OpenRouter, NVIDIA, GitHub Models, Mistral). When one hits rate limits, it automatically switches to the next
- **SOCKS5 & HTTP proxy support** — Rotates proxies per request. Auto-creates a local HTTP bridge for SOCKS5 proxies (Chrome doesn't support SOCKS5 auth natively)
- **Google bot detection handling** — Detects CAPTCHAs and blocks, retries with exponential backoff (up to 7 attempts), marks blocked combos for retry on next run instead of skipping them
- **Smart URL filtering** — Skips social media, directories, manufacturers, job sites, and 70+ other non-business domains
- **Contact page discovery** — Automatically finds and scrapes `/contact`, `/about`, and similar pages to get more complete data

## Architecture

```
input.csv (keywords × locations)
        │
        ▼
┌─────────────────────────────────────┐
│            main.py                  │
│  ThreadPoolExecutor (N workers)     │
│  Each worker handles one combo:     │
│  (keyword, location, page)          │
└───────┬─────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│        google_scraper.py            │
│  SeleniumBase + undetected Chrome   │
│  Runs in subprocess (timeout-safe)  │
│  CAPTCHA/block detection + retry    │
└───────┬─────────────────────────────┘
        │ list of URLs
        ▼
┌─────────────────────────────────────┐
│       website_extractor.py          │
│  Visits homepage + contact page     │
│  Extracts clean text + logo URL     │
└───────┬─────────────────────────────┘
        │ HTML text
        ▼
┌─────────────────────────────────────┐
│        ai_extractor.py              │
│  Sends to LLM (Llama 3.3 70B)      │
│  JSON structured extraction         │
│  Auto provider rotation on errors   │
└───────┬─────────────────────────────┘
        │ structured data
        ▼
    output.csv (thread-safe append)
    progress.json (atomic save)
```

## Output CSV Columns

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
| `logo_url` | URL to business logo |

## Setup

### 1. Clone & install dependencies

```bash
git clone https://github.com/safeerabbas/Havc_Site.git
cd Havc_Site
pip install -r requirements.txt
```

### 2. Configure AI API keys

Copy the example env file and add your keys (all free-tier):

```bash
cp .env.example .env
```

Edit `.env` and fill in at least one key from any of these providers:

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

More keys = more throughput. The scraper automatically rotates between all configured providers.

### 3. Configure proxies (optional but recommended)

Create `proxies.txt` with one proxy per line:

```
# HTTP proxy
http://user:pass@host:port

# SOCKS5 proxy (auto-bridged for Chrome)
socks5://user:pass@host:port
```

> **Note:** Datacenter proxies get blocked by Google quickly. Use **residential rotating proxies** for best results.

### 4. Configure keywords and locations

Edit `input.csv`:

```csv
keywords,locations
HVAC repair,"New York, NY"
AC installation,"Los Angeles, CA"
furnace service,"Chicago, IL"
```

The scraper generates the cartesian product: every keyword × every location.

## Usage

```bash
# Run with default 5 workers
python main.py

# Run with 10 parallel workers
python main.py --workers 10

# Graceful stop: press Ctrl+C once (finishes current URLs, saves progress)
# Force stop: press Ctrl+C twice
```

### Resuming after a stop

Just run the same command again — with any number of workers:

```bash
python main.py --workers 5    # Run for a while, then Ctrl+C
python main.py --workers 10   # Resume with more workers — picks up exactly where it left off
python main.py --workers 3    # Or fewer workers — same progress, same data
```

Progress is tracked per `(keyword, location, page)` combo, not by worker. Changing worker count has zero effect on progress.

## Configuration

All settings are in `config.py`:

| Setting | Default | Description |
|---|---|---|
| `NUM_WORKERS` | 5 | Default parallel worker threads |
| `MAX_GOOGLE_PAGES` | 50 | Google result pages per combo |
| `RESULTS_PER_PAGE` | 10 | Results per Google page |
| `PAGE_LOAD_TIMEOUT` | 30 | Seconds to wait for page loads |
| `REQUEST_DELAY_MIN` | 3 | Min delay between requests (secs) |
| `REQUEST_DELAY_MAX` | 7 | Max delay between requests (secs) |
| `WEBSITE_VISIT_TIMEOUT` | 20 | Timeout for visiting target sites |
| `MAX_HTML_LENGTH` | 5000 | Max chars of text sent to AI |
| `RATE_LIMIT_COOLDOWN` | 120 | Seconds before retrying rate-limited AI provider |

## How Resume Works

Progress is stored in `progress.json`:

```json
{
  "completed_combos": ["HVAC repair||New York, NY||0", "HVAC repair||New York, NY||1"],
  "failed_combos": ["AC repair||Miami, FL||3"],
  "processed_urls": ["https://example-hvac.com", "https://cool-air.com"]
}
```

- **`completed_combos`** — Successfully scraped combos (won't be retried)
- **`failed_combos`** — Blocked by Google after max retries (retried on next run)
- **`processed_urls`** — Individual URLs already visited (never re-processed)

The file is saved atomically (write to temp file → `os.replace`) so it's never corrupted, even on crash.

## Project Structure

```
├── main.py                 # Orchestrator: thread pool, CSV output, shutdown handling
├── google_scraper.py       # Google search scraping with SeleniumBase + CAPTCHA detection
├── website_extractor.py    # Visits business websites, finds contact pages, extracts text
├── ai_extractor.py         # Multi-provider AI extraction with auto-failover
├── progress_tracker.py     # Thread-safe, crash-safe progress tracking
├── url_generator.py        # Generates Google search URLs for all keyword×location×page combos
├── socks_bridge.py         # Local HTTP-to-SOCKS5 proxy bridge for Chrome compatibility
├── config.py               # All configuration (API keys, timeouts, file paths, excluded domains)
├── input.csv               # Input keywords and locations
├── requirements.txt        # Python dependencies
├── .env.example            # Template for API keys
└── .gitignore
```

## Thread Safety

All shared resources are protected:

| Resource | Protection |
|---|---|
| `output.csv` | `threading.Lock` — one writer at a time |
| `progress.json` | `threading.Lock` + atomic writes via `os.replace` |
| AI provider rotation | `threading.Lock` — provider index, rate-limit state |
| AI content cache | Separate `threading.Lock` |
| Shutdown signal | `threading.Event` — checked before each new URL |

## Requirements

- Python 3.8+
- Chrome browser (SeleniumBase auto-downloads ChromeDriver)
- At least one AI provider API key
- (Optional) Residential rotating proxies

## License

MIT
