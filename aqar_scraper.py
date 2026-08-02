"""
Scraper for sa.aqar.fm (AQAR), scoped to Jeddah, sale listings only.

AQAR gives each property-type + sale/rent combo its own URL, so we don't
filter rentals in code — we simply only visit the "for sale" category URLs
listed in config.settings.AQAR_CATEGORIES:

    apartments -> /شقق-للبيع/جدة
    villas     -> /فلل-للبيع/جدة
    land       -> /أراضي-للبيع/جدة
    buildings  -> /عمائر-للبيع/جدة

Each page shows 20 listings; pages are /<slug>/<city> (page 1) then
/<slug>/<city>/2, /3, ... We keep fetching pages until one comes back
with no listings, which is our signal we've reached the end.

Data lives inside the HTML (no JSON API), so we parse it with BeautifulSoup.
"""

from config.settings import (
    AQAR_BASE_URL,
    AQAR_CITY_SLUG,
    AQAR_CATEGORIES,
    AQAR_REQUEST_DELAY,
)
from scrapers.base_scraper import BaseScraper


class AqarScraper(BaseScraper):
    site_name = "aqar.fm"
    request_delay = AQAR_REQUEST_DELAY

    def __init__(self, max_pages: int | None = None):
        """
        max_pages caps how many pages to fetch PER CATEGORY. Leave it None
        for a full crawl; set it to a small number (e.g. 1) for a quick test
        run so you can check the parser without downloading ~1000 pages.
        """
        super().__init__()
        self.max_pages = max_pages

    def scrape_listings(self):
        """
        Walk every sale category, page by page, and yield one listing dict
        per property. Stops a category as soon as a page returns no
        listings (the end of that category's results) or once max_pages is
        reached.
        """
        for slug, property_type in AQAR_CATEGORIES.items():
            print(f"[{self.site_name}] category: {slug} ({property_type})")
            page = 1
            while True:
                if self.max_pages is not None and page > self.max_pages:
                    break
                url = self._page_url(slug, page)
                html = self.fetch(url)
                if html is None:
                    # Every retry failed for this page. Stop this category
                    # rather than risk an infinite loop on a dead URL.
                    print(f"  giving up on {slug} at page {page}")
                    break

                listings = self._parse_page(html, property_type)
                if not listings:
                    # Empty page = we've gone past the last real page.
                    break

                for listing in listings:
                    yield listing

                page += 1

    def _page_url(self, slug: str, page: int) -> str:
        """
        Build the URL for one category page. Page 1 has no number; later
        pages append /<n>. Arabic characters are left as-is — requests
        percent-encodes them when it sends the request.
        """
        base = f"{AQAR_BASE_URL}/{slug}/{AQAR_CITY_SLUG}"
        return base if page == 1 else f"{base}/{page}"

    def _parse_page(self, html: str, property_type: str) -> list:
        """
        Find every listing card on one page and parse each into a dict.

        The real listing links are <a class="no-underline"> tags that carry
        an `index` attribute (index="0" ... "19"). We match on that to avoid
        picking up the "featured" strip at the top or unrelated nav links,
        which don't have it. Cards that fail to parse (missing link/id) are
        skipped so one bad card can't stop the page.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("a.no-underline[index]")

        listings = []
        for card in cards:
            listing = self._parse_card(card, property_type)
            if listing is not None:
                listings.append(listing)
        return listings

    def _parse_card(self, card, property_type: str) -> dict | None:
        """
        Turn one listing <a> card into a dict shaped for upsert_listing.

        Returns None (skip the card) if there's no usable link or id — a
        listing we can't link back to is useless for the RAG, so we'd
        rather drop it than store a broken row.
        """
        # --- link + id (the non-negotiable part) ---------------------------
        href = card.get("href")
        if not href:
            return None
        source_url = AQAR_BASE_URL + href
        source_id = self._id_from_href(href)
        if not source_id:
            return None

        # --- title, and location parsed out of it --------------------------
        title_el = card.select_one("span.line-clamp-1")
        title = title_el.get_text(strip=True) if title_el else None
        street, district, city = self._split_location(title)

        # --- price ---------------------------------------------------------
        price = None
        price_el = card.select_one("p.text-brand span")
        if price_el:
            price = self._to_number(price_el.get_text(strip=True))

        # --- the icon list: area / bedrooms / bathrooms / extras -----------
        area_sqm = None
        bedrooms = None
        bathrooms = None
        extras = {}
        for li in card.select("ul li"):
            img = li.find("img")
            if not img:
                continue
            icon = (img.get("src") or "").rsplit("/", 1)[-1]  # e.g. "area.svg"
            # The <img> carries no text, so the li's own text is the value
            # (e.g. "149م²", "4"). Reading it directly avoids relying on a
            # specific inner <span> structure that can vary between cards.
            value = li.get_text(strip=True)
            if not value:
                continue

            if icon == "area.svg":
                area_sqm = self._to_number(value)
            elif icon == "bed-king.svg":
                bedrooms = int(self._to_number(value) or 0) or None
            elif icon == "bath.svg":
                bathrooms = int(self._to_number(value) or 0) or None
            elif icon == "couch.svg":
                extras["living_rooms"] = value
            elif icon == "street.svg":
                extras["street_width"] = value      # land: frontage
            elif icon == "pinned-note.svg":
                extras["land_use"] = value          # land: e.g. "سكني"

        return {
            "source_site": self.site_name,
            "source_id": source_id,
            "source_url": source_url,
            "property_type": property_type,
            "title": title,
            "price": price,
            "area_sqm": area_sqm,
            "city": city,
            "district": district,
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "description": title,   # card has no separate description text
            "extra_attributes": {**extras, "street": street} if street else extras,
        }

    # --- small text helpers -----------------------------------------------

    @staticmethod
    def _id_from_href(href: str) -> str | None:
        """
        AQAR listing URLs end in the listing's numeric id, e.g.
        '.../حي-السلامة-6685959'. Take the last '-' piece and keep it only
        if it's all digits.
        """
        last = href.rstrip("/").rsplit("-", 1)[-1]
        return last if last.isdigit() else None

    @staticmethod
    def _split_location(title: str | None):
        """
        Pull (street, district, city) out of a title like
        'شقة للبيع في شارع فؤاد بك حمزه, حي السلامة, مدينة جدة, منطقة مكة المكرمة'.

        We match parts by their Arabic keyword (شارع / حي / مدينة) instead of
        by position, so a missing piece doesn't shift the others. Returns
        Arabic strings (kept as-is for display). Any part not found is None.
        """
        if not title:
            return None, None, None

        street = district = city = None
        for part in title.split(","):
            part = part.strip()
            if part.startswith("شارع") and street is None:
                street = part
            elif part.startswith("حي") and district is None:
                district = part
            elif part.startswith("مدينة") and city is None:
                city = part.replace("مدينة", "").strip()
        return street, district, city

    @staticmethod
    def _to_number(text: str | None):
        """
        Convert a display string like '1,050,000', '149م²', or '535,000 §'
        into a float by keeping only digits and a decimal point. Returns
        None if there's nothing numeric to parse.
        """
        if not text:
            return None
        # Keep only ASCII 0-9 and the decimal point. Note we can't use
        # ch.isdigit() here: the "²" in "م²" is a Unicode digit, so isdigit()
        # would keep it and then float("149²") would blow up.
        cleaned = "".join(ch for ch in text if ch in "0123456789.")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
