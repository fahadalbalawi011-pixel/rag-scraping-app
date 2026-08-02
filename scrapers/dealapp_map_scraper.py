"""
Scraper for dealapp.sa, Jeddah, sale listings only — via the map endpoint.

This is the primary dealapp scraper. It gets the whole city in ONE request.

Why this exists
---------------
dealapp's list endpoint (/production/ad) caps a user at ~500 ads viewed and
then 403s, which stranded the API scraper at 222 of ~11.4k listings. The
obvious workaround — enumerate every ad from the public sitemaps and open each
one in a browser — turned out far worse in practice: the sitemaps carry 58,058
ads for the whole country, and at ~7 ads/min that was a 147-hour crawl to find
the ~11k Jeddah ones. Not viable.

The map endpoint sidesteps both problems. It answers with EVERY ad in a city in
a single response (11,355 for Jeddah on a test run), it ignores page/limit, and
it keeps working while /ad is capped — map data isn't "viewing an ad".

The catch, and why it's still the right trade
---------------------------------------------
Map records are a thin projection: no `area`, no `title`, no numeric `code`.
Area is the real loss, since it's one of the UI's two sliders and feeds
price_per_sqm.

What makes this workable is that /ar/ad-details/<id> accepts the Mongo `_id`,
not just the numeric code — so a working link can be built from map data alone.
(The old /ar/ad/<code> URL format 404'd, which is a separate bug.)

Rows written here therefore have price, type, district, city, bedrooms and a
live URL, but no area. That's deliberate and non-destructive: upsert_listing
COALESCEs, so running DealappScraper afterwards enriches these rows with area
and title instead of this scraper blanking them. The two are complementary —
run this for coverage, then the API scraper for depth on whatever it can reach
before the cap.
"""

import json

from config.settings import (
    DEALAPP_ALLOWED_CITIES,
    DEALAPP_CITY_ID,
    DEALAPP_LISTING_URL,
    DEALAPP_MAP_API_URL,
    DEALAPP_TOKEN,
    DEALAPP_TYPE_MAP,
)
from scrapers.base_scraper import BaseScraper

# Sanity floor for the response. Jeddah returned 11,355 records on a test run;
# anything under this means a truncated or changed response, and we'd rather
# fail loudly than write a fraction of the city and let the coverage guard
# retire the rest.
MIN_EXPECTED_RECORDS = 2000


class DealappMapScraper(BaseScraper):
    site_name = "dealapp.sa"        # same rows as the other dealapp scrapers
    request_delay = 2.0

    def __init__(self, city_id: str | None = None):
        super().__init__()
        self.city_id = city_id or DEALAPP_CITY_ID
        self.session.headers.update(
            {
                "authorization": DEALAPP_TOKEN,
                "accept": "application/json, text/plain, */*",
                "lang": "ar",
                "Origin": "https://dealapp.sa",
                "Referer": "https://dealapp.sa/",
            }
        )
        self._stats = {"total": 0, "rent": 0, "wrong_city": 0,
                       "untracked_type": 0, "no_id": 0, "kept": 0}

    def scrape_listings(self):
        records = self._fetch_map()
        self._stats["total"] = len(records)
        print(f"[{self.site_name}] map returned {len(records)} records for the city")

        for record in records:
            listing = self._map_record(record)
            if listing is not None:
                self._stats["kept"] += 1
                yield listing

        s = self._stats
        print(
            f"[{self.site_name}] kept {s['kept']} of {s['total']} — dropped "
            f"{s['rent']} rent, {s['untracked_type']} other property types, "
            f"{s['wrong_city']} other cities, {s['no_id']} without an id"
        )

    def _fetch_map(self) -> list:
        """
        Pull the city's full ad list. Deliberately NOT paginated: the endpoint
        ignores page/limit and answers with everything (~10 MB), so asking once
        is both correct and the whole point.
        """
        import time

        params = {"city": self.city_id, "limit": 10, "page": 1}
        last_error = None

        for attempt in range(1, 4):
            time.sleep(self.request_delay)
            try:
                response = self.session.get(
                    DEALAPP_MAP_API_URL, params=params, timeout=180
                )
                if response.status_code == 429:
                    wait = 30 * attempt
                    print(f"  rate-limited; waiting {wait}s (try {attempt}/3)")
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                last_error = str(exc)[:120]
                print(f"  map fetch failed (try {attempt}/3): {last_error}")
                continue

            records = self._unwrap(payload)
            # Validate before trusting it: a 200 carrying a changed or
            # truncated body must be a failure, not a tiny successful crawl.
            if records is None:
                last_error = (
                    f"unexpected response shape: "
                    f"{type(payload).__name__} "
                    f"{list(payload)[:8] if isinstance(payload, dict) else ''}"
                )
                print(f"  {last_error}")
                continue
            if len(records) < MIN_EXPECTED_RECORDS:
                last_error = (
                    f"only {len(records)} records returned, expected at least "
                    f"{MIN_EXPECTED_RECORDS}"
                )
                print(f"  {last_error}")
                continue
            return records

        raise RuntimeError(
            f"dealapp map endpoint gave nothing usable after 3 attempts "
            f"(last problem: {last_error}). Nothing was written and nothing "
            f"was deactivated."
        )

    @staticmethod
    def _unwrap(payload):
        """
        Return the record list from the response, or None if it isn't
        recognisable. The endpoint answered with a bare JSON array on a test
        run, but tolerate the common {"data": [...]} wrapper too.
        """
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "ads", "results"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return None

    def _map_record(self, record: dict):
        """Map one map record to a listing dict, or None to drop it."""
        if not isinstance(record, dict):
            return None

        if record.get("purpose") != "SALE":
            self._stats["rent"] += 1
            return None

        city = (record.get("city") or {}).get("name_ar")
        if DEALAPP_ALLOWED_CITIES and city not in DEALAPP_ALLOWED_CITIES:
            self._stats["wrong_city"] += 1
            return None

        english = (record.get("propertyType") or {}).get("propertyType")
        property_type = DEALAPP_TYPE_MAP.get(english)
        if property_type is None:
            self._stats["untracked_type"] += 1
            return None

        source_id = str(record.get("_id") or record.get("id") or "")
        if not source_id:
            self._stats["no_id"] += 1
            return None

        district = (record.get("district") or {}).get("name_ar")
        questions = record.get("relatedQuestions") or {}
        rega = record.get("regaRawData") or {}

        # Map records carry no title. Rather than leaving thousands of cards
        # reading "بدون عنوان", compose one from fields we actually have — the
        # same shape dealapp's own titles use ("فيلا للبيع في حي الغربية مدينة
        # جدة"). Flagged in extra_attributes so it's never mistaken for the
        # advertiser's own wording, and COALESCE means the API scraper's real
        # title replaces it if that ad is ever reached.
        title = f"{property_type} للبيع"
        if district:
            title += f" في {district}"
        if city:
            title += f"، {city}"

        return {
            "source_site": self.site_name,
            "source_id": source_id,
            # /ar/ad-details/ accepts the Mongo _id as well as the numeric
            # code, which is what makes map-only rows linkable at all.
            "source_url": DEALAPP_LISTING_URL.format(code=source_id),
            "property_type": property_type,
            "title": title,
            "price": _num(record.get("price")) or _num(rega.get("propertyPrice")),
            # No area in map records. Left as None ON PURPOSE — upsert_listing
            # COALESCEs, so this won't erase an area the API scraper found.
            "area_sqm": None,
            "city": city,
            "district": district,
            "bedrooms": _num(questions.get("roomsNum")),
            "bathrooms": _num(questions.get("toiletsNum")),
            "description": None,
            "extra_attributes": json.dumps(
                {
                    "source": "map",
                    "title_generated": True,
                    "coordinates": ((record.get("location") or {}).get("value") or {})
                    .get("coordinates"),
                    "created_at": record.get("createdAt"),
                },
                ensure_ascii=False,
            ),
        }


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
