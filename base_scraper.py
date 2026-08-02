"""
Shared skeleton for all site scrapers.

The idea (a "template method"): everything that's the SAME for every site
lives here — the HTTP session, the polite delay, and the run() loop that
saves listings and updates the database. The only thing that differs per
site is how HTML turns into listing dicts, so subclasses fill in exactly
one method (scrape_listings). See scrapers/aqar_scraper.py for a concrete
implementation.
"""

import time

import requests

from db.database import (
    count_active_listings,
    deactivate_missing,
    log_scrape_run,
    upsert_listing,
)


class BaseScraper:
    # Subclasses MUST set these two.
    site_name = None          # stored in listings.source_site, e.g. "aqar.fm"
    request_delay = 1.0       # seconds to sleep between page requests

    # Coverage floor for the deactivation step: a run that finds less than this
    # fraction of what's currently active is treated as incomplete, and is not
    # allowed to retire anything.
    #
    # This exists because of a real incident: the wasalt crawl stopped on page
    # 3 of 364 (a page came back unparseable, which the loop read as "past the
    # last page"), reported success with 62 listings, and then deactivated
    # 10,500 live ones — putting them 3 days from permanent deletion. Ending
    # early without raising is indistinguishable from finishing, so the
    # scraper-level fix can't be the only defence. 0.5 is deliberately loose:
    # it ignores normal churn and only catches a collapse.
    min_coverage_ratio = 0.5

    def __init__(self):
        if not self.site_name:
            raise ValueError("Subclass must set a site_name")

        # One reusable session for the whole crawl: it keeps the TCP
        # connection alive across pages and sends the same headers every
        # time. The browser-like User-Agent matters — aqar.fm refuses
        # requests that don't look like a real browser.
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                ),
                "Accept-Language": "ar,en;q=0.9",
            }
        )

    def fetch(self, url: str, retries: int = 3) -> str | None:
        """
        Download one page and return its HTML text.

        Sleeps `request_delay` seconds BEFORE each attempt so we never fire
        requests back-to-back. On a network error it retries a few times
        (waiting a bit longer each time) instead of letting one blip kill
        the whole crawl. Returns None only if every retry failed — the
        caller decides what to do with that.
        """
        for attempt in range(1, retries + 1):
            time.sleep(self.request_delay)
            try:
                response = self.session.get(url, timeout=20)
                response.raise_for_status()
                return response.text
            except requests.RequestException as exc:
                print(f"  fetch failed ({attempt}/{retries}) for {url}: {exc}")
                time.sleep(self.request_delay * attempt)  # back off a little
        return None

    def scrape_listings(self):
        """
        The one site-specific piece. A subclass overrides this to walk the
        site's pages and `yield` one listing dict at a time, with keys
        matching the columns upsert_listing expects (source_site, source_id,
        source_url, property_type, title, price, area_sqm, city, district,
        bedrooms, bathrooms, description, extra_attributes).

        It's a generator (yield, not return) so run() can save each listing
        as it's found instead of holding thousands in memory or waiting for
        the entire crawl to finish.
        """
        raise NotImplementedError("Subclasses must implement scrape_listings()")

    def run(self):
        """
        Drive a full scrape: fetch + parse every listing, save each one, then
        mark anything we DIDN'T see this run as inactive, and log a summary.

        Deactivation happens only AFTER the loop finishes successfully. If
        scrape_listings raises partway through, the exception propagates and
        we never reach deactivate_missing — so a half-finished crawl can't
        wrongly mark the rest of the city's listings as gone. (See the
        warning in db/database.py:deactivate_missing.)
        """
        from datetime import datetime, timezone

        started_at = datetime.now(timezone.utc)
        seen_ids: set = set()
        found = 0

        print(f"[{self.site_name}] starting scrape...")
        for listing in self.scrape_listings():
            listing.setdefault("source_site", self.site_name)
            upsert_listing(listing)
            seen_ids.add(listing["source_id"])
            found += 1
            if found % 50 == 0:
                print(f"[{self.site_name}] saved {found} listings so far...")

        # skip_deactivate is set for partial/resumed crawls: we only saw SOME
        # of the site's pages, so we must NOT mark the unseen ones inactive
        # (that step is only valid after a complete pass over the whole site).
        if getattr(self, "skip_deactivate", False):
            deactivated = 0
        else:
            deactivated = self._deactivate_if_complete(seen_ids, found)
        finished_at = datetime.now(timezone.utc)

        log_scrape_run(
            site=self.site_name,
            started_at=started_at,
            finished_at=finished_at,
            listings_found=found,
            new_count=None,          # (not tracked separately for now)
            reactivated_count=None,
            deactivated_count=deactivated,
        )
        print(
            f"[{self.site_name}] done: {found} listings seen, "
            f"{deactivated} marked inactive."
        )
        return found

    def _deactivate_if_complete(self, seen_ids: set, found: int) -> int:
        """
        Retire the listings this run didn't see — but only if the run actually
        looks like a full pass over the site.

        A crawl that ends early without raising reaches this point looking
        exactly like a successful one, and deactivate_missing() would then
        retire everything it never got to. So we compare what we found against
        what's already active and refuse the step when the shortfall is too big
        to be real churn. Nothing is lost by refusing: the listings simply stay
        active until a complete run confirms otherwise.
        """
        active = count_active_listings(self.site_name)
        floor = active * self.min_coverage_ratio

        if active and found < floor:
            print(
                f"\n[{self.site_name}] ⚠ SKIPPING deactivation — this run looks "
                f"incomplete.\n"
                f"    found {found} listings, but {active} are currently active "
                f"(needed {floor:.0f}+).\n"
                f"    Deactivating now would retire ~{active - found} live "
                f"listings and queue them for deletion.\n"
                f"    Nothing was changed. Check why the crawl stopped early, "
                f"then rerun a full pass."
            )
            return 0

        return deactivate_missing(self.site_name, seen_ids)
