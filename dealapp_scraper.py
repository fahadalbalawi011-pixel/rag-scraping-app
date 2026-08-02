"""
Scraper for dealapp.sa, Jeddah, sale listings only.

Dealapp has a clean JSON API, so this is a plain requests scraper (no browser).
The listings endpoint (DEALAPP_API_URL) is paginated and returns rich JSON per
ad; it requires a JWT Authorization header (DEALAPP_TOKEN). The city page mixes
sale and rent, so we filter to purpose == "SALE" and keep only the four
property types in DEALAPP_TYPE_MAP.
"""

from config.settings import (
    DEALAPP_API_URL,
    DEALAPP_CITY_NAME,
    DEALAPP_TOKEN,
    DEALAPP_TYPE_MAP,
    DEALAPP_LISTING_URL,
)
from scrapers.base_scraper import BaseScraper


class DealappScraper(BaseScraper):
    site_name = "dealapp.sa"
    request_delay = 2.0     # dealapp rate-limits (429); go gently
    page_size = 10          # dealapp's API rejects limit > 10

    def __init__(self, max_pages: int | None = None, start_page: int = 1):
        """
        max_pages caps pages fetched (page_size each). None = all pages.
        start_page resumes a crawl that stopped partway (e.g. after a 429 or
        interruption). When start_page > 1 we skip the deactivate step, since a
        resumed run only covers part of the site.
        """
        super().__init__()
        self.max_pages = max_pages
        self.start_page = start_page
        self.skip_deactivate = start_page > 1
        # Dealapp's API needs the auth token + Arabic language on every call.
        self.session.headers.update(
            {
                "authorization": DEALAPP_TOKEN,
                "accept": "application/json, text/plain, */*",
                "lang": "ar",
                "Origin": "https://dealapp.sa",
                "Referer": "https://dealapp.sa/",
            }
        )

    def scrape_listings(self):
        page = self.start_page
        while True:
            if self.max_pages is not None and page > self.max_pages:
                break

            data = self._fetch_page(page)
            if data is None:
                break
            ads = data.get("data") or []
            if not ads:
                break

            for ad in ads:
                listing = self._map_ad(ad)
                if listing:
                    yield listing

            print(f"[{self.site_name}] page {page}/{data.get('totalPages','?')} done")
            if not data.get("hasNextPage"):
                break
            page += 1

    def _fetch_page(self, page: int):
        import time

        params = {
            "page": page,
            "limit": self.page_size,
            "cityName": DEALAPP_CITY_NAME,
        }
        # Retry with growing backoff, mainly to ride out 429 rate-limiting.
        for attempt in range(1, 6):
            time.sleep(self.request_delay)
            try:
                resp = self.session.get(DEALAPP_API_URL, params=params, timeout=30)
                if resp.status_code == 429:
                    wait = 20 * attempt      # 20s, 40s, 60s, ...
                    print(f"  rate-limited on page {page}; waiting {wait}s "
                          f"(try {attempt}/5)")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                print(f"  fetch failed for page {page} "
                      f"(try {attempt}/5): {str(exc)[:70]}")
                time.sleep(self.request_delay * attempt)
        return None

    def _map_ad(self, ad: dict) -> dict | None:
        # Sale only.
        if ad.get("purpose") != "SALE":
            return None

        # propertyType is a nested object; map its English enum to our Arabic.
        ptype_obj = ad.get("propertyType") or {}
        english = ptype_obj.get("propertyType")
        property_type = DEALAPP_TYPE_MAP.get(english)
        if property_type is None:
            return None

        code = ad.get("code") or ad.get("id") or ad.get("_id")
        city = (ad.get("city") or {}).get("name_ar")
        district = (ad.get("district") or {}).get("name_ar")

        # Room counts live under relatedQuestions, NOT as top-level
        # bedrooms/bathrooms keys — those don't exist in dealapp's payload, so
        # reading them stored None for all 222 rows. area falls back to
        # regaRawData.propertyArea (the official REGA figure) when the
        # convenience `area` field is absent.
        questions = ad.get("relatedQuestions") or {}
        rega = ad.get("regaRawData") or {}

        return {
            "source_site": self.site_name,
            "source_id": str(ad.get("id") or ad.get("_id")),
            "source_url": DEALAPP_LISTING_URL.format(code=code),
            "property_type": property_type,
            "title": ad.get("title"),
            "price": _num(ad.get("price")) or _num(rega.get("propertyPrice")),
            "area_sqm": _num(ad.get("area")) or _num(rega.get("propertyArea")),
            "city": city,
            "district": district,
            "bedrooms": _num(questions.get("roomsNum")) or _num(rega.get("numberOfRooms")),
            "bathrooms": _num(questions.get("toiletsNum")),
            "description": ad.get("title"),
            "extra_attributes": {"code": code},
        }


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
