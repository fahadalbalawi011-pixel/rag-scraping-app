"""
One-shot manual command: scrape every registered site, then clean up.

Meant to be run by hand whenever you want to refresh the data (no scheduler,
since the laptop isn't always on):

    python -m pipeline.run_all                 # full crawl of every site
    python -m pipeline.run_all --max-pages 20  # capped run of every site

It loops over scrapers.SCRAPERS, so once a new site's scraper is added to
that list it's automatically included here — no change to this file needed.
If one site fails, the others still run (the error is reported and we move
on), and cleanup runs at the end regardless.
"""

import argparse

from db.database import init_db
from scrapers import SCRAPERS
from db.database import purge_old_inactive
from config.settings import INACTIVE_RETENTION_DAYS


def main():
    parser = argparse.ArgumentParser(
        description="Scrape all registered sites, then purge old listings."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Max pages per category per site (default: all). Use e.g. 20 to cap.",
    )
    args = parser.parse_args()

    init_db()

    total = 0
    for scraper_cls in SCRAPERS:
        name = scraper_cls.__name__
        print(f"\n=== Running {name} ===")
        try:
            # Only pass max_pages to scrapers that accept it.
            try:
                scraper = scraper_cls(max_pages=args.max_pages)
            except TypeError:
                scraper = scraper_cls()
            total += scraper.run() or 0
        except Exception as exc:
            # One broken site shouldn't stop the rest of the crawl.
            print(f"!!! {name} failed: {exc}")

    print("\n=== Cleanup ===")
    deleted = purge_old_inactive(retention_days=INACTIVE_RETENTION_DAYS)
    print(f"Removed {deleted} listing(s) inactive > {INACTIVE_RETENTION_DAYS} days.")

    print(f"\nAll done. {total} listing(s) seen across {len(SCRAPERS)} site(s).")


if __name__ == "__main__":
    main()
