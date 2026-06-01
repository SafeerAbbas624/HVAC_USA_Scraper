"""
Configuration settings for the Google Search Web Scraper.
"""
import os
import logging

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# =============================================================================
# AI Provider Configuration (Multiple FREE providers with auto-fallback)
# All use OpenAI-compatible chat/completions format.
# When one provider hits rate limits, the scraper automatically switches.
# API keys are loaded from .env file — see .env.example for the template.
# =============================================================================
AI_PROVIDERS = [
    {   # Groq Account 1 — 30 req/min, 14,400 req/day
        "name": "Groq",
        "api_url": "https://api.groq.com/openai/v1/chat/completions",
        "api_key": os.getenv("GROQ_API_KEY", ""),
        "model": "llama-3.3-70b-versatile",
    },
    {   # Groq Account 2
        "name": "Groq2",
        "api_url": "https://api.groq.com/openai/v1/chat/completions",
        "api_key": os.getenv("GROQ_API_KEY_2", ""),
        "model": "llama-3.3-70b-versatile",
    },
    {   # Cerebras Account 1 — 30 req/min, fast inference
        "name": "Cerebras",
        "api_url": "https://api.cerebras.ai/v1/chat/completions",
        "api_key": os.getenv("CEREBRAS_API_KEY", ""),
        "model": "llama-3.3-70b",
    },
    {   # Cerebras Account 2
        "name": "Cerebras2",
        "api_url": "https://api.cerebras.ai/v1/chat/completions",
        "api_key": os.getenv("CEREBRAS_API_KEY_2", ""),
        "model": "llama-3.3-70b",
    },
    {   # SambaNova Account 1
        "name": "SambaNova",
        "api_url": "https://api.sambanova.ai/v1/chat/completions",
        "api_key": os.getenv("SAMBANOVA_API_KEY", ""),
        "model": "Meta-Llama-3.3-70B-Instruct",
    },
    {   # SambaNova Account 2
        "name": "SambaNova2",
        "api_url": "https://api.sambanova.ai/v1/chat/completions",
        "api_key": os.getenv("SAMBANOVA_API_KEY_2", ""),
        "model": "Meta-Llama-3.3-70B-Instruct",
    },
    {   # Together AI — $5 free credit on signup
        "name": "Together",
        "api_url": "https://api.together.xyz/v1/chat/completions",
        "api_key": os.getenv("TOGETHER_API_KEY", ""),
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    },
    {   # OpenRouter Account 1 — some free models
        "name": "OpenRouter",
        "api_url": "https://openrouter.ai/api/v1/chat/completions",
        "api_key": os.getenv("OPENROUTER_API_KEY", ""),
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "extra_headers": {"HTTP-Referer": "https://hvac-scraper.local", "X-Title": "HVAC Scraper"},
        "supports_response_format": False,
        "verify_ssl": False,
    },
    {   # OpenRouter Account 2
        "name": "OpenRouter2",
        "api_url": "https://openrouter.ai/api/v1/chat/completions",
        "api_key": os.getenv("OPENROUTER_API_KEY_2", ""),
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "extra_headers": {"HTTP-Referer": "https://hvac-scraper.local", "X-Title": "HVAC Scraper"},
        "supports_response_format": False,
        "verify_ssl": False,
    },
    {   # NVIDIA — free tier available
        "name": "NVIDIA",
        "api_url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "api_key": os.getenv("NVIDIA_API_KEY", ""),
        "model": "meta/llama-3.3-70b-instruct",
    },
    {   # GitHub Account 1 — 15 req/min, 150 req/day
        "name": "GitHub",
        "api_url": "https://models.inference.ai.azure.com/chat/completions",
        "api_key": os.getenv("GITHUB_TOKEN", ""),
        "model": "Llama-3.3-70B-Instruct",
    },
    {   # GitHub Account 2
        "name": "GitHub2",
        "api_url": "https://models.inference.ai.azure.com/chat/completions",
        "api_key": os.getenv("GITHUB_TOKEN_2", ""),
        "model": "Llama-3.3-70B-Instruct",
    },
    {   # GitHub Account 3
        "name": "GitHub3",
        "api_url": "https://models.inference.ai.azure.com/chat/completions",
        "api_key": os.getenv("GITHUB_TOKEN_3", ""),
        "model": "Llama-3.3-70B-Instruct",
    },
    {   # Mistral AI Account 1
        "name": "Mistral",
        "api_url": "https://api.mistral.ai/v1/chat/completions",
        "api_key": os.getenv("MISTRAL_API_KEY", ""),
        "model": "mistral-small-latest",
    },
    {   # Mistral AI Account 2
        "name": "Mistral2",
        "api_url": "https://api.mistral.ai/v1/chat/completions",
        "api_key": os.getenv("MISTRAL_API_KEY_2", ""),
        "model": "mistral-small-latest",
    },
]

RATE_LIMIT_COOLDOWN = 120      # Base seconds to wait before retrying a rate-limited provider
MAX_PROVIDER_COOLDOWN = 300    # Max cooldown cap (5 minutes) for progressive backoff

# =============================================================================
# Threading / Worker Configuration
# =============================================================================
NUM_WORKERS = 10                # Default number of concurrent worker threads

# =============================================================================
# Scraping Configuration
# =============================================================================
MAX_GOOGLE_PAGES = 50          # Total Google result pages to scrape per combination
RESULTS_PER_PAGE = 10          # Google results per page
PAGE_LOAD_TIMEOUT = 30         # Seconds to wait for page load
REQUEST_DELAY_MIN = 3          # Minimum delay between requests (seconds)
REQUEST_DELAY_MAX = 7          # Maximum delay between requests (seconds)
WEBSITE_VISIT_TIMEOUT = 20     # Seconds to wait when visiting target websites
MAX_RAW_HTML_LENGTH = 200000   # Max raw HTML to fetch from websites (200KB)
MAX_HTML_LENGTH = 5000         # Max characters of cleaned text to send to AI

# =============================================================================
# File Paths
# =============================================================================
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INPUT_CSV = os.path.join(_BASE_DIR, "input.csv")
OUTPUT_CSV = os.path.join(_BASE_DIR, "output.csv")
PROGRESS_FILE = os.path.join(_BASE_DIR, "progress.json")
PROXIES_FILE = os.path.join(_BASE_DIR, "proxies.txt")
LOG_FILE = os.path.join(_BASE_DIR, "scraper.log")

# =============================================================================
# Social Media & Excluded Domains
# =============================================================================
EXCLUDED_DOMAINS = [
    # Social Media
    "facebook.com", "youtube.com", "instagram.com", "linkedin.com",
    "twitter.com", "x.com", "pinterest.com", "tiktok.com",
    "snapchat.com", "tumblr.com", "flickr.com", "whatsapp.com",
    "telegram.org", "reddit.com", "nextdoor.com",
    # Search / Tech
    "google.com", "googleapis.com", "gstatic.com", "apple.com",
    "microsoft.com", "bing.com",
    # E-commerce
    "amazon.com", "ebay.com", "craigslist.org",
    # Directories & Marketplaces
    "yelp.com", "bbb.org", "yellowpages.com", "yellowpages.ca",
    "mapquest.com", "angi.com", "angieslist.com", "homeadvisor.com",
    "houzz.com", "thumbtack.com", "homestars.com", "localprobook.com",
    "porch.com", "bark.com", "networx.com", "expertise.com",
    "chamberofcommerce.com", "manta.com", "superpages.com",
    "citysearch.com", "buildzoom.com", "findlocal.com",
    # Big-Box / National Retailers
    "homedepot.com", "homedepot.ca", "lowes.com", "walmart.com",
    "costco.com", "menards.com", "sears.com",
    # HVAC Manufacturers (NOT local service businesses)
    "trane.com", "carrier.com", "bryant.com", "lennox.com",
    "goodmanmfg.com", "rheem.com", "daikincomfort.com", "daikin.com",
    "johnsoncontrols.com", "york.com", "mitsubishicomfort.com",
    "fujitsugeneral.com", "americanstandardair.com", "ruud.com",
    "amana-hac.com", "comfortmaker.com",
    # News / Media / Government / Education
    "wikipedia.org", "wbrc.com", "finehomesandliving.com",
    "energy.gov", "epa.gov", ".gov",
    # Online Parts Stores
    "store.acpro.com", "supplyhouse.com", "alpinehomeair.com",
    # Job / Salary Sites
    "ziprecruiter.com", "indeed.com", "glassdoor.com", "salary.com",
    "careerbuilder.com", "monster.com", "simplyhired.com",
]

# =============================================================================
# Contact Page Paths to Try
# =============================================================================
CONTACT_PATHS = [
    "/contact",
    "/contact-us",
    "/contactus",
    "/contact_us",
    "/about",
    "/about-us",
    "/aboutus",
    "/about_us",
    "/get-in-touch",
    "/reach-us",
]

# =============================================================================
# Output CSV Headers
# =============================================================================
OUTPUT_HEADERS = [
    "keyword",
    "location",
    "google_page",
    "business_name",
    "website_url",
    "business_description",
    "services_offered",
    "contact_phone",
    "contact_email",
    "address",
    "business_city",
    "business_state",
    "supply_location",
    "logo_url",
]

# =============================================================================
# Logging Configuration
# =============================================================================
def setup_logging():
    """Configure logging for the scraper."""
    logger = logging.getLogger("scraper")
    logger.setLevel(logging.DEBUG)

    # File handler
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    # Formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger

