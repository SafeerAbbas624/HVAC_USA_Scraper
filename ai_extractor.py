"""
AI-powered data extraction with multi-provider rotation.

Supports multiple free AI APIs (Groq, Cerebras, SambaNova, Together, OpenRouter).
Automatically switches to the next provider when rate limits are hit.
"""
import hashlib
import json
import logging
import re
import threading
import time
import warnings

import requests
import urllib3
from bs4 import BeautifulSoup

# Suppress InsecureRequestWarning for providers with verify_ssl=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS")

import config

logger = logging.getLogger("scraper")

# ---------------------------------------------------------------------------
# Provider rotation state (protected by _provider_lock)
# ---------------------------------------------------------------------------
_provider_lock = threading.Lock()
_current_provider_idx = 0
_rate_limited_until = {}   # provider index -> unix timestamp when cooldown expires
_consecutive_failures = {} # provider index -> count of consecutive 429s


def _get_available_providers():
    """Return list of providers that have an API key configured."""
    return [
        (i, p) for i, p in enumerate(config.AI_PROVIDERS)
        if p.get("api_key")
    ]


def _get_provider():
    """
    Get the next available provider that isn't rate-limited.
    Returns (index, provider_dict) or (None, None) if all exhausted.
    Thread-safe: acquires _provider_lock for the selection phase,
    releases it before any sleep so other threads aren't blocked.
    """
    global _current_provider_idx
    available = _get_available_providers()
    if not available:
        return None, None

    with _provider_lock:
        now = time.time()
        # Try starting from current, wrapping around
        for _ in range(len(available)):
            idx, provider = available[_current_provider_idx % len(available)]
            cooldown_until = _rate_limited_until.get(idx, 0)
            if now >= cooldown_until:
                return idx, provider
            _current_provider_idx += 1

        # All rate-limited — find the one with shortest remaining cooldown
        soonest_idx, soonest_provider = available[0]
        soonest_time = _rate_limited_until.get(soonest_idx, 0)
        for idx, provider in available:
            t = _rate_limited_until.get(idx, 0)
            if t < soonest_time:
                soonest_idx, soonest_provider = idx, provider
                soonest_time = t
        wait_secs = max(0, soonest_time - now)

    # Sleep outside the lock so other threads can proceed
    logger.info(f"All AI providers rate-limited. Waiting {wait_secs:.0f}s for {soonest_provider['name']}...")
    time.sleep(wait_secs + 1)
    with _provider_lock:
        _rate_limited_until.pop(soonest_idx, None)
    return soonest_idx, soonest_provider


def _mark_rate_limited(idx):
    """Mark a provider as rate-limited with progressive backoff cooldown."""
    global _current_provider_idx
    with _provider_lock:
        _consecutive_failures[idx] = _consecutive_failures.get(idx, 0) + 1
        fail_count = _consecutive_failures[idx]
        cooldown = min(
            config.RATE_LIMIT_COOLDOWN * fail_count,
            getattr(config, "MAX_PROVIDER_COOLDOWN", 300),
        )
        _rate_limited_until[idx] = time.time() + cooldown
        _current_provider_idx += 1
    provider_name = config.AI_PROVIDERS[idx]["name"]
    logger.warning(f"Provider {provider_name} rate-limited (fail #{fail_count}), cooldown {cooldown}s, switching...")


def _mark_success(idx):
    """Reset consecutive failure count after a successful request."""
    with _provider_lock:
        _consecutive_failures[idx] = 0


# ---------------------------------------------------------------------------
# Content extraction & filtering (reduce AI token usage by 80-90%)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_content_cache = {}  # md5 hash -> extracted dict (deduplicates identical pages)

JUNK_PATTERNS = [
    "domain for sale", "this domain", "buy this domain",
    "coming soon", "under construction", "site not found",
    "parked domain", "page not found", "404 not found",
    "account suspended", "website expired", "website is suspended",
    "default web page", "apache2 default page",
    "welcome to nginx", "it works!", "test page",
    "this page isn't working", "access denied",
    "403 forbidden", "502 bad gateway", "503 service",
]


def _smart_html_to_text(html: str) -> str:
    """Extract only high-value text using BeautifulSoup (80-90% token reduction)."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content elements
    for tag in soup(["script", "style", "noscript", "svg", "path", "iframe"]):
        tag.decompose()

    parts = []

    # Title
    if soup.title:
        parts.append(f"TITLE: {soup.title.get_text(strip=True)}")

    # Meta description
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        parts.append(f"META: {meta['content']}")

    # Headings (h1, h2, h3)
    for tag in soup.find_all(["h1", "h2", "h3"]):
        text = tag.get_text(strip=True)
        if text:
            parts.append(text)

    # First 20 paragraphs (skip tiny fragments)
    for p in soup.find_all("p")[:20]:
        text = p.get_text(strip=True)
        if text and len(text) > 15:
            parts.append(text)

    # Address tags (often contain business address)
    for addr in soup.find_all("address"):
        text = addr.get_text(separator=" ", strip=True)
        if text:
            parts.append(f"ADDRESS: {text}")

    # Footer (often has phone, address, email)
    footer = soup.find("footer")
    if footer:
        footer_text = footer.get_text(separator=" ", strip=True)[:500]
        if footer_text:
            parts.append(f"FOOTER: {footer_text}")

    return "\n".join(parts)


def _pre_extract_contacts(html: str) -> dict:
    """Pre-extract emails and phones via regex from raw HTML (free, no AI tokens)."""
    result = {"emails": [], "phones": []}

    # --- Emails ---
    emails = set()
    # From mailto: links (most reliable)
    mailto = re.findall(
        r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
        html, re.IGNORECASE,
    )
    emails.update(e.lower() for e in mailto)
    # From plain text
    plain = re.findall(
        r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', html,
    )
    emails.update(e.lower() for e in plain)
    # Filter junk/placeholder emails
    junk_domains = {
        'example.com', 'email.com', 'domain.com', 'yoursite.com',
        'sentry.io', 'wixpress.com', 'wordpress.com', 'change.me',
        'yourcompany.com', 'test.com',
    }
    result["emails"] = [
        e for e in emails
        if not any(j in e for j in junk_domains)
    ][:5]

    # --- US Phone numbers ---
    phones = set()
    # From tel: links (most reliable)
    tel = re.findall(
        r'tel:[\+]?1?[\-\s]?(\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})',
        html,
    )
    phones.update(tel)
    # From plain text
    plain_phones = re.findall(
        r'(?<!\d)\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}(?!\d)', html,
    )
    phones.update(plain_phones)
    result["phones"] = list(phones)[:5]

    return result


def _is_junk_site(text: str) -> bool:
    """Detect junk/parked/error pages to skip AI entirely."""
    if len(text.strip()) < 150:
        return True
    text_lower = text.lower()
    return any(pattern in text_lower for pattern in JUNK_PATTERNS)


def _content_hash(text: str) -> str:
    """MD5 hash of cleaned text for deduplication."""
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()


# System prompt instructing the AI to extract business data
# Slimmed down: website_url and logo_url are injected in Python, not by AI
SYSTEM_PROMPT = """You are a data extraction assistant. Extract business information from website text.
Return ONLY a valid JSON object with these exact keys:

{
    "business_name": "Name of the business",
    "business_description": "One-sentence description of what the business does",
    "services_offered": "Comma-separated list of services",
    "contact_phone": "Phone number(s) if found",
    "contact_email": "Email address(es) if found",
    "address": "Full street address if found",
    "business_city": "City from the address",
    "business_state": "2-letter US state abbreviation (e.g., NY, CA, TX)"
}

Rules:
- Return ONLY the JSON object, no markdown or extra text.
- Use "" for fields not found. Do not invent information.
- For business_state, always use standard 2-letter US state abbreviation.
- Extract the ACTUAL physical location from the address on the website."""


def extract_business_data(
    homepage_html: str,
    contact_html: str = "",
    logo_url: str = "",
    website_url: str = "",
) -> dict:
    """
    Send content to AI and extract structured business data.

    Tries multiple AI providers with automatic fallback on rate limits.
    """
    default_result = {
        "business_name": "",
        "website_url": website_url,
        "business_description": "",
        "services_offered": "",
        "contact_phone": "",
        "contact_email": "",
        "address": "",
        "business_city": "",
        "business_state": "",
        "logo_url": logo_url,
    }

    if not homepage_html:
        logger.warning(f"No HTML content to analyze for {website_url}")
        return default_result

    # Phase 1: Pre-extract emails and phones via regex (free, no AI tokens)
    all_html = homepage_html + "\n" + (contact_html or "")
    pre_contacts = _pre_extract_contacts(all_html)

    # Phase 2: Smart HTML reduction via BeautifulSoup (80-90% token savings)
    homepage_text = _smart_html_to_text(homepage_html)
    contact_text = _smart_html_to_text(contact_html) if contact_html else ""
    combined_text = homepage_text
    if contact_text:
        combined_text += "\n\n--- CONTACT PAGE ---\n" + contact_text

    # Phase 3: Junk site filtering (skip parked/error/construction pages)
    if _is_junk_site(combined_text):
        logger.info(f"Skipping junk/parked site: {website_url}")
        if pre_contacts["emails"]:
            default_result["contact_email"] = ", ".join(pre_contacts["emails"])
        if pre_contacts["phones"]:
            default_result["contact_phone"] = ", ".join(pre_contacts["phones"])
        return default_result

    # Phase 4: Content hash deduplication (catches identical template/parked pages)
    content_key = _content_hash(combined_text)
    with _cache_lock:
        cached_entry = _content_cache.get(content_key)
    if cached_entry is not None:
        logger.info(f"Cache hit for {website_url} (duplicate content)")
        cached = cached_entry.copy()
        cached["website_url"] = website_url
        cached["logo_url"] = logo_url or cached.get("logo_url", "")
        if pre_contacts["emails"]:
            cached["contact_email"] = ", ".join(pre_contacts["emails"])
        if pre_contacts["phones"]:
            cached["contact_phone"] = ", ".join(pre_contacts["phones"])
        return cached

    # Phase 5: Truncate cleaned text and build AI prompt
    combined_text = combined_text[:config.MAX_HTML_LENGTH]
    user_content = f"Website: {website_url}\n\n{combined_text}"

    # Try each provider with rotation on rate limits
    available = _get_available_providers()
    max_attempts = max(len(available) * 4, 8)

    for attempt in range(max_attempts):
        idx, provider = _get_provider()
        if provider is None:
            logger.error("No AI providers configured with API keys!")
            return default_result

        try:
            headers = {
                "Authorization": f"Bearer {provider['api_key']}",
                "Content-Type": "application/json",
            }
            # Merge any provider-specific headers (e.g. OpenRouter needs HTTP-Referer)
            if provider.get("extra_headers"):
                headers.update(provider["extra_headers"])

            payload = {
                "model": provider["model"],
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.1,
                "max_tokens": 1000,
            }
            # Only add response_format if provider supports it (default True)
            if provider.get("supports_response_format", True):
                payload["response_format"] = {"type": "json_object"}

            logger.info(f"[{provider['name']}] Sending data for {website_url} (attempt {attempt+1}/{max_attempts})")
            verify_ssl = provider.get("verify_ssl", True)
            response = requests.post(
                provider["api_url"],
                json=payload,
                headers=headers,
                timeout=(10, 60),
                verify=verify_ssl,
            )

            # Rate limit, payload too large, or provider error → switch provider
            if response.status_code in (429, 413):
                logger.warning(f"[{provider['name']}] HTTP {response.status_code} for {website_url}")
                _mark_rate_limited(idx)
                continue

            if response.status_code >= 400:
                body = response.text[:300] if response.text else ""
                logger.warning(f"[{provider['name']}] HTTP {response.status_code} for {website_url}: {body}")
                _mark_rate_limited(idx)
                continue
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            # Remove any markdown formatting (e.g., ```json\n...\n```)
            content = re.sub(r"^```(?:json)?\s*\n", "", content)
            content = re.sub(r"\n```\s*$", "", content)
            extracted = json.loads(content)

            # Ensure all expected keys exist
            for key in default_result:
                if key not in extracted:
                    extracted[key] = default_result[key]

            # Merge regex pre-extracted contacts (more reliable than AI for email/phone)
            if pre_contacts["emails"]:
                extracted["contact_email"] = ", ".join(pre_contacts["emails"])
            if pre_contacts["phones"]:
                extracted["contact_phone"] = ", ".join(pre_contacts["phones"])

            extracted["website_url"] = website_url
            if not extracted.get("logo_url"):
                extracted["logo_url"] = logo_url

            # Cache result for content deduplication
            with _cache_lock:
                _content_cache[content_key] = extracted.copy()

            logger.info(f"[{provider['name']}] Successfully extracted data for {website_url}")
            _mark_success(idx)
            return extracted

        except requests.exceptions.RequestException as e:
            logger.error(f"[{provider['name']}] API request failed for {website_url}: {e}")
            _mark_rate_limited(idx)
            continue

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.warning(f"[{provider['name']}] Failed to parse response for {website_url}: {e}")
            _mark_rate_limited(idx)  # switch to next provider instead of giving up
            continue

        except Exception as e:
            logger.warning(f"[{provider['name']}] Unexpected error for {website_url}: {e}")
            _mark_rate_limited(idx)  # switch to next provider instead of giving up
            continue

    logger.error(f"All AI providers exhausted after {max_attempts} attempts for {website_url}")
    # Still use regex-extracted contacts even if AI failed
    if pre_contacts["emails"]:
        default_result["contact_email"] = ", ".join(pre_contacts["emails"])
    if pre_contacts["phones"]:
        default_result["contact_phone"] = ", ".join(pre_contacts["phones"])
    return default_result

