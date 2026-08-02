"""
One-off repair: rewrite dealapp listing URLs to the format the site actually
serves.

The dealapp scraper originally built links as https://dealapp.sa/ar/ad/<code>,
which was a guess and is wrong — every one of those returns dealapp's 404 page,
which is why the "عرض الإعلان" links in the Streamlit UI all read "page does
not exist". The real pattern, confirmed against the site's own sitemaps, is
https://dealapp.sa/ar/ad-details/<code>.

Rescraping would fix the URLs as a side effect, but the API scraper's view cap
means a rescrape can't reach most rows — and it isn't needed anyway: the ad
code was already stored in extra_attributes, so the correct URL can be rebuilt
locally with no requests at all.

    python -m pipeline.fix_dealapp_urls --dry-run   # show what would change
    python -m pipeline.fix_dealapp_urls            # apply
"""

import argparse
import json

from config.settings import DEALAPP_LISTING_URL
from db.database import get_connection


def fix_urls(dry_run: bool = False) -> tuple:
    """
    Rebuild source_url for every dealapp row that has a stored ad code.
    Returns (fixed, skipped, already_ok).
    """
    fixed = skipped = already_ok = 0

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, source_url, extra_attributes
            FROM listings
            WHERE source_site = 'dealapp.sa'
            """
        ).fetchall()

        for row in rows:
            try:
                extra = json.loads(row["extra_attributes"] or "{}")
            except json.JSONDecodeError:
                extra = {}
            code = extra.get("code")

            if not code:
                # No code stored, so the URL can't be rebuilt offline. These
                # need a rescrape; report rather than guess.
                skipped += 1
                continue

            correct = DEALAPP_LISTING_URL.format(code=code)
            if row["source_url"] == correct:
                already_ok += 1
                continue

            if dry_run:
                print(f"  {row['source_url']}  ->  {correct}")
            else:
                conn.execute(
                    "UPDATE listings SET source_url = ? WHERE id = ?",
                    (correct, row["id"]),
                )
            fixed += 1

        if dry_run:
            # get_connection() commits on a clean exit; make sure a dry run
            # leaves the database untouched.
            conn.rollback()

    return fixed, skipped, already_ok


def main():
    parser = argparse.ArgumentParser(description="Repair dealapp listing URLs.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the changes without writing them.",
    )
    args = parser.parse_args()

    fixed, skipped, already_ok = fix_urls(dry_run=args.dry_run)
    verb = "would fix" if args.dry_run else "fixed"
    print(
        f"\ndealapp URLs: {verb} {fixed}, already correct {already_ok}, "
        f"skipped (no stored code) {skipped}."
    )
    if skipped:
        print("Rows without a stored code need a rescrape to get a working link.")


if __name__ == "__main__":
    main()
