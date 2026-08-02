"""
Enriches dealapp listings by reading each ad's public detail page.

Third piece of the dealapp story:

  DealappMapScraper     every Jeddah ad in one request, but no title/area
  DealappScraper        full API data, but capped at ~490 ads per run
  this one              fills in the gaps, with no cap and no browser

How it works, and why this is the cheap path
--------------------------------------------
/ar/ad-details/<id> is server-rendered and embeds a schema.org
`RealEstateListing` block in the HTML. That block needs no auth token, isn't
behind the /production/ad view cap (dealapp's own server renders it), and comes
back to plain `requests` — so this walks the worklist over HTTP at a few ads a
second instead of driving Chromium through 58k page loads.

The page also accepts the Mongo `_id`, not just the numeric code, which is what
lets map-sourced rows be enriched at all — they have no code.

What it can and can't recover
-----------------------------
From the JSON-LD, reliably: the advertiser's real title, the full description,
price, and room count.

Area is the awkward one. There is NO area/floorSize field anywhere in the
markup — the only place it appears is inside the Arabic description text
("مساحة الارض 240 متر"). So area here is *parsed from free text an advertiser
typed*, and will not be found for every ad. Rows that yield no area keep
area_sqm = None rather than getting a guess, and every parsed value is tagged
`area_from_text` in extra_attributes so it's never mistaken for the figure the
API reports.

Everything this scraper doesn't learn is left as None, and upsert_listing
COALESCEs, so it can only ever add to a row — never blank a field another
scraper already filled.
"""

import json
import re
import time

from config.settings import DEALAPP_LISTING_URL
from db.database import get_source_ids_missing_area
from scrapers.base_scraper import BaseScraper

# The ad's schema.org block. Matched by its marker attribute rather than by
# position, since the page carries several unrelated ld+json blocks (website,
# organization, breadcrumbs) that must not be picked up by mistake.
_LISTING_LD = re.compile(
    r'<script[^>]*data-schema-markup-id="real-estate-listing-schema-[^"]*"[^>]*>'
    r"(.*?)</script>",
    re.S,
)

# "مساحة الارض 240 متر", "مساحة الأرض ٢٤٠", "المساحة 240 م2", "مساحه 240متر".
# Land/plot area first, because that's the figure the other sites report.
_AREA_LAND = re.compile(
    r"مساح[ةه]\s*(?:ال)?(?:ارض|أرض)\s*[:\-]?\s*([\d٠-٩][\d٠-٩.,]*)"
)
# Generic "مساحة 240" — but NOT "مساحة البناء" (built-up area), which is a
# different, usually larger number and would overstate a plot.
_AREA_ANY = re.compile(
    r"مساح[ةه]\s*(?!ال?بناء)(?:ال)?\s*[:\-]?\s*([\d٠-٩][\d٠-٩.,]*)"
)

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# dealapp answers 200 with its registration page instead of the ad once you've
# viewed a handful anonymously. Measured behaviour: ~6-10 ads render, then every
# request comes back walled until the window rolls over. Clearing cookies does
# NOT reset it (tested: a cookie-cleared batch fared worse than one that kept
# them, and a walled batch recovered on its own mid-run), so it's time/IP based
# and there's no client-side trick around it.
_WALL_MARKERS = ("صفحة التسجيل", "login?logout=true")


class DealappDetailScraper(BaseScraper):
    site_name = "dealapp.sa"
    request_delay = 0.4          # polite but brisk; these are plain HTML gets

    def __init__(self, max_ads: int | None = None):
        """
        max_ads limits how many detail pages to fetch this run (None = every
        listing still missing an area). Use a small number to check it first.
        """
        super().__init__()
        self.max_ads = max_ads
        # Enrichment only ever touches a subset of the site, so it must never
        # be the basis for retiring listings.
        self.skip_deactivate = True
        self._hit_wall = False
        self._stats = {"fetched": 0, "with_area": 0, "with_title": 0,
                       "failed": 0, "walled": 0}

    # How many walled pages to see before concluding the quota is spent. More
    # than one so a single odd response doesn't end an otherwise fine run.
    wall_tolerance = 3

    def scrape_listings(self):
        todo = get_source_ids_missing_area(self.site_name)
        print(f"[{self.site_name}] {len(todo)} listings have no area yet")
        if self.max_ads is not None:
            todo = todo[: self.max_ads]
            print(f"[{self.site_name}] limited to {len(todo)} this run")

        started = time.time()
        consecutive_walls = 0
        consecutive_failures = 0

        for i, source_id in enumerate(todo, 1):
            listing = self._scrape_one(source_id)

            if self._hit_wall:
                # Quota exhausted. Stop straight away rather than spending
                # requests on pages that will all come back as the signup page:
                # the window is time-based, so the only thing that helps is
                # coming back later.
                consecutive_walls += 1
                if consecutive_walls >= self.wall_tolerance:
                    print(
                        f"\n[{self.site_name}] view quota reached after "
                        f"{self._stats['fetched']} ads this run.\n"
                        f"  dealapp serves its signup page instead of the ad once "
                        f"you've viewed ~6-10 anonymously, and the limit is "
                        f"time-based — cookies and sessions don't reset it.\n"
                        f"  {self._stats['with_area']} listing(s) gained an area "
                        f"before the wall. Everything is saved; rerun later and it "
                        f"picks up with what's still missing."
                    )
                    break
                continue

            consecutive_walls = 0
            if listing is None:
                consecutive_failures += 1
                if consecutive_failures >= 15:
                    print(f"[{self.site_name}] ABORTING: 15 consecutive failures "
                          f"that aren't the view wall — see reasons above.")
                    break
            else:
                consecutive_failures = 0
                yield listing

            if i % 50 == 0 or i == len(todo):
                rate = i / max(time.time() - started, 1e-9)
                eta = (len(todo) - i) / rate / 60 if rate else 0
                s = self._stats
                print(f"[{self.site_name}] {i}/{len(todo)} | "
                      f"area found {s['with_area']} · titles {s['with_title']} · "
                      f"failed {s['failed']} | "
                      f"{rate * 60:.0f}/min · ETA {eta:.0f}m")

        s = self._stats
        print(f"[{self.site_name}] enriched {s['fetched']} pages: "
              f"{s['with_area']} gained an area, {s['with_title']} a real title, "
              f"{s['failed']} failed")

    def _scrape_one(self, source_id: str):
        self._hit_wall = False
        url = DEALAPP_LISTING_URL.format(code=source_id)
        html = self.fetch(url)
        if not html:
            self._stats["failed"] += 1
            return None

        # Distinguish "you've been walled" from "this page is shaped oddly".
        # These were indistinguishable at first — both just counted as failures
        # reading "no RealEstateListing block", which made a spent quota look
        # like a parser bug.
        if any(marker in html for marker in _WALL_MARKERS):
            self._hit_wall = True
            self._stats["walled"] += 1
            return None

        data = self._extract_listing_ld(html)
        if data is None:
            self._stats["failed"] += 1
            if self._stats["failed"] <= 3:
                print(f"  [fail] {source_id}: no RealEstateListing block in page")
            return None

        self._stats["fetched"] += 1

        item = data.get("itemOffered") or {}
        offers = data.get("offers") or {}
        description = data.get("description")

        title = data.get("name")
        if title:
            self._stats["with_title"] += 1

        area = _parse_area(description)
        if area is not None:
            self._stats["with_area"] += 1

        # Only fields we actually learned. Everything else stays None so
        # upsert_listing's COALESCE leaves the existing value alone — notably
        # district: the map scraper stored it in Arabic ("الغربية") while this
        # markup gives a transliteration ("ALGHARBIAH"), and overwriting would
        # fragment the UI's district filter.
        return {
            "source_site": self.site_name,
            "source_id": str(source_id),
            "source_url": url,
            "property_type": None,
            "title": title,
            "price": _num(offers.get("price")),
            "area_sqm": area,
            "city": None,
            "district": None,
            "bedrooms": _num(item.get("numberOfRooms")),
            "bathrooms": None,
            "description": description,
            "extra_attributes": json.dumps(
                {
                    "source": "detail-page",
                    # Flagged so a text-derived area is never confused with the
                    # authoritative figure from the API.
                    "area_from_text": area is not None,
                    "advertiser": (offers.get("seller") or {}).get("name"),
                    "license_number": _additional(item, "licenseNumber"),
                    "street_width": _additional(item, "streetWidth"),
                    "date_posted": data.get("datePosted"),
                },
                ensure_ascii=False,
            ),
        }

    @staticmethod
    def _extract_listing_ld(html: str):
        """Pull and parse the ad's RealEstateListing JSON-LD, or None."""
        match = _LISTING_LD.search(html)
        if not match:
            return None
        try:
            data = json.loads(match.group(1))
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("@type") != "RealEstateListing":
            return None
        return data


def _additional(item: dict, name: str):
    """Read one value out of schema.org's additionalProperty list."""
    for prop in item.get("additionalProperty") or []:
        if isinstance(prop, dict) and prop.get("name") == name:
            return prop.get("value")
    return None


def _parse_area(description) -> float | None:
    """
    Best-effort area from the advertiser's Arabic description.

    There is no structured area on the page, so this is genuinely heuristic:
    it prefers an explicit land/plot area, falls back to a generic "مساحة N",
    and refuses anything outside a plausible range so a phone number or a
    price fragment can't land in area_sqm.
    """
    if not description:
        return None
    text = str(description).translate(_ARABIC_DIGITS)

    for pattern in (_AREA_LAND, _AREA_ANY):
        for raw in pattern.findall(text):
            value = _num(raw.replace(",", "").rstrip("."))
            # Sanity window: the smallest real listing in the DB is 30 m² and
            # anything past ~100k m² is a parse artefact, not a property.
            if value is not None and 20 <= value <= 100_000:
                return value
    return None


def _num(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None
