"""Auto-register the per-site scraper classes into SITE_REGISTRY.

Active targets: Google Maps, Yelp, BBB, Angi, HomeAdvisor.
"""
from .google_maps import GoogleMapsReviews
from .yelp import YelpReviews
from .bbb import BBBRating
from .angi import AngiReviews
from .homeadvisor import HomeAdvisorReviews


SITE_REGISTRY = [
    GoogleMapsReviews(),
    YelpReviews(),
    BBBRating(),
    AngiReviews(),
    HomeAdvisorReviews(),
]

ALL_SITE_IDS = [s.SITE_ID for s in SITE_REGISTRY]


def get_scraper(site_id):
    for s in SITE_REGISTRY:
        if s.SITE_ID == site_id:
            return s
    return None
