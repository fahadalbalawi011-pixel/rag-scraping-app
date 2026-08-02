"""
Scraper for bayut.sa, Jeddah, sale listings only.

Bayut has an aggressive Cloudflare challenge, so we drive a VISIBLE browser
(headless=False) using a saved profile (use_profile=True): you solve the
challenge by hand once and the clearance cookie is reused on later runs. If a
challenge reappears mid-run, wait_until_unblocked() pauses for you to solve it.

Listings are plain HTML tagged with stable aria-label attributes, so we select
cards by [aria-label="Listing"] and read each field by its aria-label. Page 1
is the base URL; later pages append صفحة-N/. We keep only the four sale
categories in BAYUT_ALLOWED_TYPES.
"""

import re

from config.settings import BAYUT_JEDDAH_SALE_URL, BAYUT_ALLOWED_TYPES
from scrapers.playwright_scraper import PlaywrightScraper

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None


class BayutScraper(PlaywrightScraper):
    site_name = "bayut.sa"
    headless = False        # must be visible so a challenge can be solved
    use_profile = True      # reuse the saved Cloudflare clearance cookie

    def __init__(self, max_pages: int | None = None, start_page: int = 1):
        """
        max_pages caps how many pages to fetch (25 listings each). None = keep
        going until a page has no listings. Use a small number to test.

        start_page lets you RESUME a crawl that stopped partway (e.g. laptop
        slept): start where you left off instead of from page 1. When
        start_page > 1 we skip the deactivate step, because a resumed run only
        covers part of the site and must not mark the earlier pages inactive.
        """
        super().__init__()
        self.max_pages = max_pages
        self.start_page = start_page
        self.skip_deactivate = start_page > 1

    def _page_url(self, page: int) -> str:
        # Page 1 is the base URL; later pages append صفحة-N/.
        return BAYUT_JEDDAH_SALE_URL if page == 1 else f"{BAYUT_JEDDAH_SALE_URL}صفحة-{page}/"

    def scrape_listings(self):
        page = self.start_page
        while True:
            if self.max_pages is not None and page > self.max_pages:
                break

            if not self.goto(self._page_url(page), wait_ms=5000):
                print(f"[{self.site_name}] could not load page {page}, stopping")
                break

            # First page of a session may show a Cloudflare challenge; give you
            # a chance to solve it (the saved profile usually avoids this).
            if "كلمة التحقق" in self.safe_content():
                if not self.wait_until_unblocked():
                    print(f"[{self.site_name}] still blocked, stopping")
                    break

            cards = self._parse_cards(self.safe_content())
            if not cards:
                break                       # no listings -> past the last page

            for listing in cards:
                yield listing

            print(f"[{self.site_name}] page {page} done ({len(cards)} kept)")
            page += 1

    def _parse_cards(self, html: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        listings = []
        for card in soup.select('[aria-label="Listing"]'):
            listing = self._parse_card(card)
            if listing:
                listings.append(listing)
        return listings

    def _parse_card(self, card) -> dict | None:
        property_type = _text(card, "Type")
        if property_type not in BAYUT_ALLOWED_TYPES:
            return None

        link = card.select_one('a[aria-label="Listing link"]')
        href = link.get("href") if link else None
        if not href:
            return None
        # source_id is the number in .../تفاصيل-87925788.html
        m = re.search(r"-(\d+)\.html", href)
        if not m:
            return None
        source_id = m.group(1)
        source_url = href if href.startswith("http") else f"https://www.bayut.sa{href}"

        location = _text(card, "Location") or ""
        parts = [p.strip() for p in location.split("،") if p.strip()]
        district = next((p for p in parts if p.startswith("حي")), None)
        city = parts[-1] if parts else None

        return {
            "source_site": self.site_name,
            "source_id": source_id,
            "source_url": source_url,
            "property_type": property_type,
            "title": _text(card, "Title"),
            "price": _num(_text(card, "Price")),
            "area_sqm": _num(_text(card, "Area")),
            "city": city,
            "district": district,
            "bedrooms": _num(_text(card, "Beds")),
            "bathrooms": _num(_text(card, "Baths")),
            "description": _text(card, "Title"),
            "extra_attributes": {"location": location},
        }


def _text(card, label: str):
    el = card.select_one(f'[aria-label="{label}"]')
    return el.get_text(" ", strip=True) if el else None


def _num(text):
    """
    Parse the leading number, ignoring any unit suffix. Drops thousands commas
    first, then takes only the leading digits/decimal. This matters for area:
    '819 م2' must be 819, NOT 8192 (the '2' in 'م2' is a real ASCII digit).
    '750,000' -> 750000, '157 م2' -> 157, '4' -> 4.
    """
    if not text:
        return None
    cleaned = text.replace(",", "").strip()
    m = re.match(r"[\d.]+", cleaned)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None
