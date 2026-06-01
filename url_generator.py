"""
URL generation utilities for Google Search and Google Maps.

Contains the core functions for UULE encoding and URL generation.
These functions should NOT be modified.
"""
import base64
import urllib.parse


# Character set used for UULE length encoding
_UULE_CHARS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789-_"
)


def encode_location_for_uule(location: str) -> str:
    """
    Encode a location string into a Google UULE parameter.

    The UULE parameter is used by Google to geo-target search results
    to a specific location without relying on the user's IP address.

    Args:
        location: The location name (e.g., "New York, NY").

    Returns:
        The UULE-encoded string for use in Google search URLs.
    """
    location_bytes = location.encode("utf-8")
    encoded = base64.b64encode(location_bytes).decode("utf-8")
    length_char = _UULE_CHARS[len(location_bytes) % len(_UULE_CHARS)]
    return f"w+CAIQICI{length_char}{encoded}"


def generate_google_search_urls(keyword: str, location: str) -> list:
    """
    Generate Google Search URLs for pages 1-10 of results.

    Produces 10 URLs with start values: 0, 10, 20, ..., 90.

    Args:
        keyword: The search keyword.
        location: The location for geo-targeted results.

    Returns:
        A list of 10 Google Search URLs (one per page).
    """
    uule = encode_location_for_uule(location)
    encoded_keyword = urllib.parse.quote_plus(keyword)
    urls = []
    for page in range(10):
        start = page * 10
        url = (
            f"https://www.google.com/search?"
            f"q={encoded_keyword}"
            f"&uule={uule}"
            f"&start={start}"
            f"&num=10"
        )
        urls.append(url)
    return urls


def generate_google_maps_urls(keyword: str, location: str) -> str:
    """
    Generate a Google Maps search URL for the given keyword and location.

    Args:
        keyword: The search keyword.
        location: The location for the search.

    Returns:
        A Google Maps search URL string.
    """
    query = f"{keyword} in {location}"
    encoded_query = urllib.parse.quote(query)
    return f"https://www.google.com/maps/search/{encoded_query}"


# =========================================================================
# Extended pagination helper (pages 11-50)
# This builds on the core functions above without modifying them.
# =========================================================================

def generate_search_url_for_page(keyword: str, location: str, page_idx: int) -> str:
    """
    Build a single Google Search URL for the given (keyword, location, page_idx).

    Avoids the cost of building all max_pages URLs when only one is needed.

    Args:
        keyword: The search keyword.
        location: The location for geo-targeted results.
        page_idx: 0-based page index (0 = first page).

    Returns:
        The Google Search URL for that page.
    """
    uule = encode_location_for_uule(location)
    encoded_keyword = urllib.parse.quote_plus(keyword)
    start = page_idx * 10
    return (
        f"https://www.google.com/search?"
        f"q={encoded_keyword}"
        f"&uule={uule}"
        f"&start={start}"
        f"&num=10"
    )


def generate_all_search_urls(keyword: str, location: str, max_pages: int = 50) -> list:
    """
    Generate Google Search URLs for pages 1 through max_pages.

    Uses generate_google_search_urls() for pages 1-10, then extends
    with additional URLs for pages 11 through max_pages.

    Args:
        keyword: The search keyword.
        location: The location for geo-targeted results.
        max_pages: Total number of pages (default 50).

    Returns:
        A list of Google Search URLs for all requested pages.
    """
    # Pages 1-10 from the original function
    urls = generate_google_search_urls(keyword, location)

    # Pages 11 through max_pages (start values: 100, 110, ..., (max_pages-1)*10)
    if max_pages > 10:
        uule = encode_location_for_uule(location)
        encoded_keyword = urllib.parse.quote_plus(keyword)
        for page in range(10, max_pages):
            start = page * 10
            url = (
                f"https://www.google.com/search?"
                f"q={encoded_keyword}"
                f"&uule={uule}"
                f"&start={start}"
                f"&num=10"
            )
            urls.append(url)

    return urls

