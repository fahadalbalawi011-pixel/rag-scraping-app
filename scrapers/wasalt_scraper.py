"""
Scraper for wasalt.sa, Jeddah, sale listings only.

Wasalt blocks plain HTTP (Cloudflare) and renders listings with Next.js, so
we drive a real browser via PlaywrightScraper. We use the server-rendered
search endpoint (WASALT_SEARCH_URL) which respects a &page=N param: each page
returns 32 listings embedded in the page as __NEXT_DATA__ JSON. We walk pages
1..totalPages, parse that JSON, map the fields we need, and keep only the four
sale categories in WASALT_ALLOWED_TYPES.
"""

import json
import re
import time

from config.settings import WASALT_SEARCH_URL, WASALT_ALLOWED_TYPES
from scrapers.playwright_scraper import PlaywrightScraper


class WasaltScraper(PlaywrightScraper):
    site_name = "wasalt.sa"

    # Cloudflare, handled the same way bayut already does it successfully.
    #
    # The symptom was every page past the second failing with "no
    # __NEXT_DATA__" — a Cloudflare interstitial, not a rate limit. Running
    # headless with a throwaway context means we arrive as an unrecognised
    # visitor on every single navigation and get challenged each time. bayut
    # does full document loads per page too and crawls 7,873 listings fine,
    # because it keeps a profile: you clear the challenge once and the
    # cf_clearance cookie is reused from then on.
    headless = False        # must be visible so a challenge can be solved
    use_profile = True      # persist cf_clearance between pages AND runs

    def __init__(self, max_pages: int | None = None, start_page: int | None = None):
        """
        max_pages caps how many pages to fetch (~32 listings each). None =
        all pages (full crawl, ~364 pages). Use a small number to test.

        start_page jumps straight to a given page — both to RESUME a crawl that
        died partway and to reproduce a single failing page without walking
        there first. As elsewhere, starting past page 1 means the run only
        covers part of the site, so the deactivate step is skipped.
        """
        super().__init__()
        self.max_pages = max_pages
        self.start_page = start_page or 1
        self.skip_deactivate = self.start_page > 1

    # How many times to reload a page whose __NEXT_DATA__ we couldn't parse
    # before treating it as a hard failure.
    page_retries = 4

    # Seconds between page loads. Modest politeness spacing — with a valid
    # clearance cookie the pages come back fine, so this no longer has to be
    # long enough to outlast a challenge.
    page_delay = 2.0

    # Backoff after a page fails, used only when waiting for the challenge to
    # clear didn't already fix it.
    retry_waits = (15, 45, 90)

    # How long to wait for a Cloudflare challenge to clear (seconds), either on
    # its own or by you solving it in the visible browser window.
    challenge_timeout = 180

    def scrape_listings(self):
        # wasalt's `page` param is 1-INDEXED, despite __NEXT_DATA__ on the slug
        # landing page showing {"page": 0}. That 0 is that page's internal
        # default, not this route's indexing: requesting page=0 returns a
        # different, much smaller result set (it reported totalPages=12, versus
        # 364 for page=1). Don't "fix" this to 0-based again — the giveaway is a
        # first page whose totalPages disagrees with every later page.
        page_num = self.start_page
        total_pages = None
        first = True

        while True:
            if self.max_pages is not None and page_num > self.max_pages:
                break
            if total_pages is not None and page_num > total_pages:
                break                      # past the last page: a clean finish

            if not first:
                time.sleep(self.page_delay)
            first = False

            props, seen_total = self._load_page(page_num)
            if seen_total:
                total_pages = seen_total

            if not props:
                # Reaching here means every retry failed on a page that should
                # exist. Raising is what protects the data: run() never reaches
                # the deactivation step when scrape_listings throws. The old
                # code `break`-ed instead, which made a failed page 3 of 364
                # look exactly like a clean finish — and that's what retired
                # 10,500 live listings.
                raise RuntimeError(
                    f"wasalt page {page_num}"
                    f"{f' of {total_pages}' if total_pages else ''} failed after "
                    f"{self.page_retries} attempts.\n"
                    f"  cause: {getattr(self, '_last_failure_reason', 'unknown')}\n"
                    f"  The failing page was saved to data/failures/ for "
                    f"inspection.\n"
                    f"  Nothing was deactivated — the listings already in the "
                    f"database are untouched.\n"
                    f"  Resume from here with: "
                    f"--site wasalt --start-page {page_num}"
                )

            for prop in props:
                listing = self._map_property(prop)
                if listing:
                    yield listing

            print(f"[{self.site_name}] page {page_num}"
                  f"{('/' + str(total_pages)) if total_pages else ''} done")

            if total_pages is not None and page_num >= total_pages:
                break
            page_num += 1

    def _page_url(self, page_num: int) -> str:
        """
        URL for one results page. Split out from _load_page so the page
        arithmetic can be tested without touching the network — an off-by-one
        here is exactly the kind of bug that's invisible in a passing crawl.
        """
        return f"{WASALT_SEARCH_URL}&page={page_num}"

    def _load_page(self, page_num: int):
        """
        Load one results page and parse it, retrying a few times. Returns
        (properties, total_pages) — empty properties means every attempt
        failed, which the caller treats as an error rather than an ending.
        """
        url = self._page_url(page_num)
        reason = "not attempted"
        for attempt in range(1, self.page_retries + 1):
            if self.goto(url, wait_ms=4000):
                props, total_pages, reason = self._extract_page()
                if props:
                    return props, total_pages
            else:
                reason = "navigation failed"

            if attempt < self.page_retries:
                # A Cloudflare block is handled by WAITING for clearance, never
                # by clearing cookies: cf_clearance is the proof we already
                # passed a challenge, so dropping it guarantees the next
                # request is challenged again. (An earlier version of this
                # reset the session here, which was actively making things
                # worse — page 3 only recovered because the long sleep let the
                # challenge lapse, not because of the reset.)
                if "Cloudflare" in reason and self._wait_for_clearance(page_num):
                    continue          # cleared: retry immediately, no backoff

                wait_s = self.retry_waits[min(attempt - 1, len(self.retry_waits) - 1)]
                print(f"  page {page_num}: {reason} "
                      f"(attempt {attempt}/{self.page_retries}), "
                      f"retrying in {wait_s}s")
                time.sleep(wait_s)

        # Every attempt failed. Save what the page actually returned so the
        # cause can be diagnosed from disk instead of by guessing and
        # re-requesting — the last attempt's body is the evidence.
        self._dump_failure(page_num, reason)
        self._last_failure_reason = reason
        return [], None

    def _extract_page(self):
        """
        Parse the page's __NEXT_DATA__.

        Returns (properties, total_pages, reason). `reason` names WHICH step
        failed — these look identical in the output otherwise, but mean very
        different things: a missing script tag is a block or a redirect, a
        missing searchResult key is a layout change, and an empty properties
        list on a page within range is genuine pagination trouble. Collapsing
        them into one "no listings" message is what made the page-3 failure
        impossible to diagnose from the log.
        """
        html = self.safe_content()   # tolerates the Cloudflare reload race
        if not html:
            return [], None, "page content unreadable"

        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        if not m:
            blocked = any(
                marker in html
                for marker in ("cf-browser-verification", "Just a moment",
                               "challenge-platform", "كلمة التحقق")
            )
            return [], None, (
                "blocked by Cloudflare (no __NEXT_DATA__)" if blocked
                else "no __NEXT_DATA__ script in page"
            )

        try:
            data = json.loads(m.group(1))
        except ValueError as exc:
            return [], None, f"__NEXT_DATA__ is not valid JSON ({str(exc)[:40]})"

        page_props = (data.get("props") or {}).get("pageProps")
        if not isinstance(page_props, dict):
            return [], None, "__NEXT_DATA__ has no props.pageProps"

        sr = page_props.get("searchResult")
        if not isinstance(sr, dict):
            # SSG pages can render without their data payload; that's a very
            # different problem from "this page has no results".
            return [], None, (
                f"pageProps has no searchResult "
                f"(keys: {sorted(page_props)[:6]})"
            )

        props = _find_properties(sr)
        if not props:
            return [], None, (
                f"searchResult present but empty "
                f"(count={sr.get('count')}, totalPages={sr.get('totalPages')}, "
                f"cached={sr.get('cached')})"
            )
        return props, sr.get("totalPages"), "ok"

    def _wait_for_clearance(self, page_num: int) -> bool:
        """
        Sit on the challenge page until it clears, then report whether the
        real content arrived.

        Most Cloudflare interstitials resolve themselves in a few seconds. If
        one needs a click, the browser is visible (headless = False) so it can
        be solved by hand — and because use_profile is on, the resulting
        cf_clearance cookie is reused for the rest of the crawl and for later
        runs, so this should happen once rather than per page.
        """
        print(f"    waiting for Cloudflare to clear on page {page_num} "
              f"(solve it in the browser window if it asks)...")
        waited = 0
        while waited < self.challenge_timeout:
            self.page.wait_for_timeout(3000)
            waited += 3
            props, _total, reason = self._extract_page()
            if props:
                print(f"    cleared after {waited}s")
                return True
        print(f"    still blocked after {waited}s")
        return False

    def _dump_failure(self, page_num: int, reason: str):
        """Write the failing page's HTML and a screenshot to data/failures/."""
        from datetime import datetime

        from config.settings import BASE_DIR

        out_dir = BASE_DIR / "data" / "failures"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = out_dir / f"wasalt_page{page_num}_{stamp}"
        try:
            base.with_suffix(".html").write_text(
                f"<!-- reason: {reason} -->\n{self.safe_content()}",
                encoding="utf-8",
            )
            self.page.screenshot(path=str(base.with_suffix(".png")))
            print(f"  saved failing page to {base}.html / .png")
        except Exception as exc:
            print(f"  (could not save failure dump: {str(exc)[:60]})")

    def _map_property(self, prop: dict) -> dict | None:
        """
        Map a raw wasalt property object to our listing schema. Returns None
        for anything outside the four sale categories we keep.
        """
        info = prop.get("propertyInfo") or {}
        property_type = info.get("propertySubType")
        if property_type not in WASALT_ALLOWED_TYPES:
            return None

        # attributes is a list of {key, value, ...}; index it by key.
        attrs = {a.get("key"): a.get("value") for a in (prop.get("attributes") or [])}

        area = attrs.get("carpetArea") or prop.get("floorSize")
        slug = info.get("slug")
        source_url = f"https://wasalt.sa/property/sale/{slug}" if slug else None

        return {
            "source_site": self.site_name,
            "source_id": str(prop.get("id")),
            "source_url": source_url,
            "property_type": property_type,
            "title": info.get("title"),
            "price": _num(info.get("salePrice")),
            "area_sqm": _num(area),
            "city": info.get("city"),
            "district": info.get("zone") or info.get("territory"),
            "bedrooms": _num(attrs.get("noOfBedrooms")),
            "bathrooms": _num(attrs.get("noOfBathrooms")),
            "description": info.get("address") or info.get("title"),
            "extra_attributes": {
                "territory": info.get("territory"),
                "address": info.get("address"),
                "furnishing": info.get("furnishingType"),
            },
        }


def _num(value):
    """Convert a value (int, or a string like '200') to a number, else None."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _find_properties(obj):
    """
    Recursively collect property dicts (those with both 'id' and
    'propertyInfo') wherever they sit, ignoring anything that isn't one.
    Tolerant of the JSON shape varying between page loads.
    """
    results = []
    if isinstance(obj, dict):
        if "propertyInfo" in obj and "id" in obj:
            results.append(obj)
        else:
            for v in obj.values():
                results += _find_properties(v)
    elif isinstance(obj, list):
        for v in obj:
            results += _find_properties(v)
    return results
