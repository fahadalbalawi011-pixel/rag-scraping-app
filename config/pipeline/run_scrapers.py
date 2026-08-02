"""
Run ONE site's scraper, so each site can own a terminal.

Run from the project root so the `config`, `db`, and `scrapers` packages
import correctly:

    python -m pipeline.run_scrapers --site aqar
    python -m pipeline.run_scrapers --site wasalt
    python -m pipeline.run_scrapers --site bayut
    python -m pipeline.run_scrapers --site dealapp
    python -m pipeline.run_scrapers --site dealapp-browser

Useful flags (only the ones a given scraper accepts are passed to it):

    --max-pages N    cap pages per category — a quick sanity check before
                     committing to a full crawl. Use 1 to smoke-test.
    --start-page N   RESUME a crawl that stopped partway (laptop slept, 429,
                     Ctrl-C). Skips the deactivate step, since a partial pass
                     must not retire the listings it never looked at.
    --max-ads N      dealapp-browser only: how many ad pages to open.
    --retry-failed   dealapp-browser only: re-attempt ads that errored before.
    --refresh-codes  dealapp-browser only: re-download the sitemaps.

Use pipeline.run_all instead to crawl every site back-to-back in one terminal.
"""

import argparse

from db.database import init_db
from scrapers.aqar_scraper import AqarScraper
from scrapers.bayut_scraper import BayutScraper
from scrapers.dealapp_detail_scraper import DealappDetailScraper
from scrapers.dealapp_map_scraper import DealappMapScraper
from scrapers.dealapp_playwright_scraper import (
    DealappEnrichScraper,
    DealappPlaywrightScraper,
)
from scrapers.dealapp_scraper import DealappScraper
from scrapers.wasalt_scraper import WasaltScraper

# --site value -> scraper class. Keys are short because they get typed a lot.
SITES = {
    "aqar": AqarScraper,
    "wasalt": WasaltScraper,
    "bayut": BayutScraper,
    "dealapp": DealappMapScraper,           # the one to use: whole city, 1 request
    "dealapp-api": DealappScraper,          # enriches with area/title until capped
    # Plain HTTP, no cap, no browser — reads each ad's public detail page.
    "dealapp-enrich": DealappDetailScraper,
    # Browser variants. Kept for the case where the detail page stops carrying
    # its schema.org block, but both are far slower for no extra data.
    "dealapp-enrich-browser": DealappEnrichScraper,
    "dealapp-browser": DealappPlaywrightScraper,   # every ad in the sitemaps: slow
}


def main():
    parser = argparse.ArgumentParser(
        description="Run one site's scraper.",
        epilog="Sites: " + ", ".join(SITES),
    )
    parser.add_argument(
        "--site", required=True, choices=sorted(SITES),
        help="Which site to scrape.",
    )
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--start-page", type=int, default=None)
    parser.add_argument("--max-ads", type=int, default=None)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--refresh-codes", action="store_true")
    args = parser.parse_args()

    # Make sure the database file and tables exist before we write to them.
    # Safe to call every run (CREATE TABLE IF NOT EXISTS).
    init_db()

    scraper_cls = SITES[args.site]

    # Build kwargs from whatever the user actually passed, then drop any this
    # scraper doesn't accept — that keeps one flag list working across
    # scrapers with different signatures, without a table of which-takes-what.
    wanted = {
        "max_pages": args.max_pages,
        "start_page": args.start_page,
        "max_ads": args.max_ads,
        "retry_failed": args.retry_failed or None,
        "refresh_codes": args.refresh_codes or None,
    }
    import inspect

    accepted = inspect.signature(scraper_cls.__init__).parameters
    kwargs = {
        k: v for k, v in wanted.items()
        if v is not None and k in accepted
    }

    ignored = [
        k for k, v in wanted.items()
        if v is not None and k not in accepted
    ]
    if ignored:
        print(f"note: {scraper_cls.__name__} ignores {', '.join(ignored)}")

    if args.site == "bayut":
        print("bayut opens a VISIBLE browser — if a Cloudflare challenge "
              "appears, solve it in that window and the run continues.")

    print(f"=== {scraper_cls.__name__} {kwargs or ''} ===")
    scraper_cls(**kwargs).run()


if __name__ == "__main__":
    main()
