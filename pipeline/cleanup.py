"""
Housekeeping entry point: permanently delete listings that have been
inactive (gone from the source site) longer than the retention window.

The scrapers only ever *hide* vanished listings (is_active = 0) so recently
removed ones stay visible for a few days. This script is what actually frees
the space afterwards. Run it on a schedule (e.g. daily) alongside scraping:

    python -m pipeline.cleanup                 # use the default retention
    python -m pipeline.cleanup --days 7        # override the window

The real work lives in db.database.purge_old_inactive; this file just wires
it to a command and reports how many rows were removed.
"""

import argparse

from config.settings import INACTIVE_RETENTION_DAYS
from db.database import purge_old_inactive


def main():
    parser = argparse.ArgumentParser(
        description="Delete listings inactive longer than the retention window."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=INACTIVE_RETENTION_DAYS,
        help=f"Retention window in days (default: {INACTIVE_RETENTION_DAYS}).",
    )
    args = parser.parse_args()

    deleted = purge_old_inactive(retention_days=args.days)
    print(f"Cleanup done: {deleted} listing(s) older than {args.days} days removed.")


if __name__ == "__main__":
    main()
