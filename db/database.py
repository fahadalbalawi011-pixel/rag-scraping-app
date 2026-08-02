"""
The only file that should execute SQL directly. Scrapers and the Streamlit
UI call functions from here — they never write raw SQL themselves.

Uses plain sqlite3 (stdlib), no ORM: the schema is small (4 tables) and
single-user, so an ORM would just add indirection without real benefit.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from config.settings import DB_PATH, INACTIVE_RETENTION_DAYS
from db.schema import SCHEMA_SQL


@contextmanager
def get_connection():
    """
    Yields a sqlite3 connection configured the way the rest of this file
    expects, and guarantees it's closed afterward.

    - row_factory = sqlite3.Row lets us access columns by name (row["price"])
      instead of by fragile positional index.
    - foreign_keys = ON is required every connection, because SQLite has it
      off by default; without it, ON DELETE CASCADE on listing_tags silently
      does nothing.
    - Commits on clean exit, rolls back if an exception was raised, so
      callers don't have to remember to do it themselves.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """
    Creates the data/ folder and the database file + tables if they don't
    already exist. Safe to call every time the app or pipeline starts up —
    CREATE TABLE IF NOT EXISTS makes this idempotent.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)


import re as _re

# Arabic diacritics (tashkeel) — removed so vowelled/unvowelled spellings match.
_ARABIC_DIACRITICS = _re.compile(r"[ً-ْٰ]")


def normalize_district(district):
    """
    Make district names consistent across sites so the same place isn't listed
    twice in the UI filter. Sources write the same district differently:
      - with/without the "حي" (neighborhood) prefix: "حي الرحاب" vs "الرحاب"
      - different alef/hamza spellings: "الصفاء" vs "الصفا", "أبحر" vs "ابحر"
      - stray spaces or diacritics.
    We strip the prefix, remove diacritics, unify alef/hamza forms, and drop a
    trailing hamza, so all variants collapse to one canonical label.
    """
    if not district:
        return district
    d = " ".join(district.split())          # collapse repeated spaces
    for prefix in ("حي ", "حى ", "حيّ "):
        if d.startswith(prefix):
            d = d[len(prefix):].strip()
            break
    d = _ARABIC_DIACRITICS.sub("", d)
    # Unify letter variants that differ only by spelling.
    d = (d.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")  # alef forms
           .replace("ى", "ي")                                     # alef maksura
           .replace("ؤ", "و").replace("ئ", "ي"))                  # hamza carriers
    d = d.rstrip("ء").strip()               # trailing hamza (الصفاء -> الصفا)
    return d or None


def normalize_all_districts() -> int:
    """
    One-off maintenance: apply normalize_district() to every existing row, so
    data scraped before normalization existed gets merged too. Returns how many
    rows changed. Safe to run repeatedly (already-normalized rows are skipped).
    """
    changed = 0
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, district FROM listings WHERE district IS NOT NULL"
        ).fetchall()
        for row in rows:
            new = normalize_district(row["district"])
            if new != row["district"]:
                conn.execute(
                    "UPDATE listings SET district = ? WHERE id = ?", (new, row["id"])
                )
                changed += 1
    return changed


def upsert_listing(listing: dict) -> int:
    """
    Insert a new listing, or update it if (source_site, source_id) already
    exists. This is the single entry point scrapers use to save data.

    `listing` is expected to have normalized keys matching the columns in
    `listings` (source_site, source_id, source_url, property_type, title,
    price, area_sqm, city, district, bedrooms, bathrooms, description,
    extra_attributes). Missing keys default to None.

    Design choice: an upsert always sets is_active=1 and clears
    inactive_since. That means a listing that disappeared and later
    reappears on the source site is automatically "reactivated" just by
    being seen again in a scrape — no separate reactivation logic needed.
    """
    price = listing.get("price")
    area_sqm = listing.get("area_sqm")
    price_per_sqm = (price / area_sqm) if price and area_sqm else None

    extra_attributes = listing.get("extra_attributes")
    if isinstance(extra_attributes, dict):
        extra_attributes = json.dumps(extra_attributes)

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO listings (
                source_site, source_id, source_url, property_type, title,
                price, area_sqm, price_per_sqm, city, district,
                bedrooms, bathrooms, description, extra_attributes,
                last_seen_at, is_active, inactive_since
            ) VALUES (
                :source_site, :source_id, :source_url, :property_type, :title,
                :price, :area_sqm, :price_per_sqm, :city, :district,
                :bedrooms, :bathrooms, :description, :extra_attributes,
                CURRENT_TIMESTAMP, 1, NULL
            )
            ON CONFLICT (source_site, source_id) DO UPDATE SET
                source_url       = COALESCE(excluded.source_url, source_url),
                property_type    = COALESCE(excluded.property_type, property_type),
                city             = COALESCE(excluded.city, city),
                district         = COALESCE(excluded.district, district),
                extra_attributes = COALESCE(excluded.extra_attributes, extra_attributes),

                -- COALESCE, not plain assignment: a scraper that doesn't KNOW a
                -- field must not erase a value another scraper already found.
                -- Two scrapers write dealapp rows and they see different
                -- amounts of detail — the map endpoint returns every Jeddah ad
                -- but carries no title or area, so a plain overwrite would
                -- blank the area on rows the API scraper had filled in, and
                -- silently drop them out of the UI's area filter. NULL here
                -- means "no new information", never "the value is now empty".
                title            = COALESCE(excluded.title, title),
                price            = COALESCE(excluded.price, price),
                area_sqm         = COALESCE(excluded.area_sqm, area_sqm),
                bedrooms         = COALESCE(excluded.bedrooms, bedrooms),
                bathrooms        = COALESCE(excluded.bathrooms, bathrooms),
                description      = COALESCE(excluded.description, description),

                -- Recomputed from the merged values above, so it stays
                -- consistent with whatever price/area actually survived.
                price_per_sqm    = CASE
                    WHEN COALESCE(excluded.price, price) IS NOT NULL
                     AND COALESCE(excluded.area_sqm, area_sqm) > 0
                    THEN COALESCE(excluded.price, price)
                         / COALESCE(excluded.area_sqm, area_sqm)
                    ELSE NULL
                END,

                last_seen_at     = CURRENT_TIMESTAMP,
                is_active        = 1,
                inactive_since   = NULL
            """,
            {
                "source_site": listing.get("source_site"),
                "source_id": listing.get("source_id"),
                "source_url": listing.get("source_url"),
                "property_type": listing.get("property_type"),
                "title": listing.get("title"),
                "price": price,
                "area_sqm": area_sqm,
                "price_per_sqm": price_per_sqm,
                "city": listing.get("city"),
                "district": normalize_district(listing.get("district")),
                "bedrooms": listing.get("bedrooms"),
                "bathrooms": listing.get("bathrooms"),
                "description": listing.get("description"),
                "extra_attributes": extra_attributes,
            },
        )
        row = conn.execute(
            "SELECT id FROM listings WHERE source_site = ? AND source_id = ?",
            (listing.get("source_site"), listing.get("source_id")),
        ).fetchone()
        return row["id"]


def deactivate_missing(source_site: str, seen_source_ids: set) -> int:
    """
    Marks listings inactive if they belong to `source_site`, are currently
    active, but were NOT in `seen_source_ids` (the set of ids the latest
    scrape run actually found). This is the "listing removed from the
    source site" case.

    Caller's responsibility: only call this after a scrape that completed
    successfully. If a scrape run fails partway and returns an empty/partial
    set, calling this would incorrectly mark everything else inactive.
    """
    if not seen_source_ids:
        placeholder_clause = "1 = 1"  # no ids seen -> nothing to exclude
        params: list = [source_site]
    else:
        placeholder_clause = f"source_id NOT IN ({','.join('?' * len(seen_source_ids))})"
        params = [source_site, *seen_source_ids]

    with get_connection() as conn:
        cursor = conn.execute(
            f"""
            UPDATE listings
            SET is_active = 0,
                inactive_since = CURRENT_TIMESTAMP
            WHERE source_site = ?
              AND is_active = 1
              AND {placeholder_clause}
            """,
            params,
        )
        return cursor.rowcount


def get_source_ids_missing_area(source_site: str) -> list:
    """
    source_ids for a site's active listings that have no area_sqm yet.

    This is the enrichment worklist. The dealapp map endpoint gives complete
    coverage but carries no area, and the API endpoint has the area but is
    capped at ~490 ads per run — so the browser scraper is pointed at exactly
    the rows still missing it, instead of walking 58k sitemap entries most of
    which aren't even the right city.

    Ordered newest-first, so a run that gets interrupted has spent its effort
    on the listings most likely to matter.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT source_id
            FROM listings
            WHERE source_site = ?
              AND is_active = 1
              AND area_sqm IS NULL
            ORDER BY last_seen_at DESC
            """,
            (source_site,),
        ).fetchall()
        return [row["source_id"] for row in rows]


def count_active_listings(source_site: str) -> int:
    """
    How many listings are currently active for one site. Used by
    BaseScraper.run() to sanity-check a crawl's coverage before it's allowed
    to retire anything — see the guard there for why.
    """
    with get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM listings WHERE source_site = ? AND is_active = 1",
            (source_site,),
        ).fetchone()[0]


def reactivate_all(source_site: str) -> int:
    """
    Undo a bad deactivation: mark every inactive listing for a site active
    again. For when a crawl died partway and wrongly retired listings that are
    still live on the site (the symptom is a scrape_runs row with a tiny
    listings_found and a huge deactivated_count).
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE listings
            SET is_active = 1, inactive_since = NULL
            WHERE source_site = ? AND is_active = 0
            """,
            (source_site,),
        )
        return cursor.rowcount


def purge_old_inactive(retention_days: int = INACTIVE_RETENTION_DAYS) -> int:
    """
    Permanently deletes listings that have been inactive for longer than
    `retention_days`. This is what keeps the database from growing forever
    with dead listings (is_active=0 rows are kept around for a while first,
    in case the boss wants to look back at recently-delisted properties).

    Favorited listings are exempt: if you liked a property, it stays in the
    database even after it disappears from the source site, so your favorites
    list can't lose entries behind your back.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            DELETE FROM listings
            WHERE is_active = 0
              AND inactive_since IS NOT NULL
              AND inactive_since < datetime('now', ?)
              AND id NOT IN (SELECT listing_id FROM favorites)
            """,
            (f"-{retention_days} days",),
        )
        return cursor.rowcount


def add_tag(listing_id: int, tag_name: str):
    """Attaches a custom tag to a listing, creating the tag if it's new."""
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO custom_tags (name) VALUES (?)", (tag_name,))
        tag_row = conn.execute(
            "SELECT id FROM custom_tags WHERE name = ?", (tag_name,)
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO listing_tags (listing_id, tag_id) VALUES (?, ?)",
            (listing_id, tag_row["id"]),
        )


def remove_tag(listing_id: int, tag_name: str):
    """Detaches a tag from a listing (the tag itself stays in custom_tags)."""
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM listing_tags
            WHERE listing_id = ?
              AND tag_id = (SELECT id FROM custom_tags WHERE name = ?)
            """,
            (listing_id, tag_name),
        )


def get_tags_for_listing(listing_id: int) -> list:
    """Returns the list of tag names attached to a listing."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT ct.name
            FROM custom_tags ct
            JOIN listing_tags lt ON lt.tag_id = ct.id
            WHERE lt.listing_id = ?
            """,
            (listing_id,),
        ).fetchall()
        return [r["name"] for r in rows]


def get_tags_for_listings(listing_ids) -> dict:
    """
    Batch version of get_tags_for_listing: {listing_id: [tag names]} for many
    listings in ONE query.

    The UI renders 30 cards per page and needs each card's tags. Calling
    get_tags_for_listing per card would open 30 connections per rerun (and a
    Streamlit rerun happens on every widget interaction), so the page build
    does this instead. Listings with no tags are simply absent from the dict.
    """
    ids = list(listing_ids)
    if not ids:
        return {}

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT lt.listing_id, ct.name
            FROM listing_tags lt
            JOIN custom_tags ct ON ct.id = lt.tag_id
            WHERE lt.listing_id IN ({','.join('?' * len(ids))})
            ORDER BY ct.name
            """,
            ids,
        ).fetchall()

    result: dict = {}
    for row in rows:
        result.setdefault(row["listing_id"], []).append(row["name"])
    return result


def get_all_tags() -> list:
    """
    Every category name that's been used at least once, sorted — for the UI's
    "filter by category" dropdown.

    Only tags actually attached to a listing are returned. custom_tags can
    hold an orphaned name (remove_tag deletes the link, not the tag), and
    offering a category that matches nothing would just be a dead filter.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ct.name
            FROM custom_tags ct
            JOIN listing_tags lt ON lt.tag_id = ct.id
            ORDER BY ct.name
            """
        ).fetchall()
        return [row["name"] for row in rows]


def set_favorite(listing_id: int, favorite: bool):
    """Likes (favorite=True) or unlikes a listing. Idempotent either way."""
    with get_connection() as conn:
        if favorite:
            conn.execute(
                "INSERT OR IGNORE INTO favorites (listing_id) VALUES (?)",
                (listing_id,),
            )
        else:
            conn.execute("DELETE FROM favorites WHERE listing_id = ?", (listing_id,))


def toggle_favorite(listing_id: int) -> bool:
    """
    Flips a listing's liked state and returns what it became (True = now
    liked). One call per click from the UI's heart button.
    """
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM favorites WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM favorites WHERE listing_id = ?", (listing_id,))
            return False
        conn.execute("INSERT INTO favorites (listing_id) VALUES (?)", (listing_id,))
        return True


def get_favorite_ids() -> set:
    """
    The set of all liked listing ids. Returned as a set because the UI's only
    question per card is "is this one liked?" — an O(1) membership test, and
    one query for the whole page instead of one per card.
    """
    with get_connection() as conn:
        rows = conn.execute("SELECT listing_id FROM favorites").fetchall()
        return {row["listing_id"] for row in rows}


def count_favorites() -> int:
    """How many listings are liked (for the sidebar's counter)."""
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]


def query_listings(
    search: str = None,
    property_type: str = None,
    city: str = None,
    district: str = None,
    price_min: float = None,
    price_max: float = None,
    area_min: float = None,
    area_max: float = None,
    include_unknown_area: bool = False,
    tags: list = None,
    favorites_only: bool = False,
    active_only: bool = True,
) -> pd.DataFrame:
    """
    The main read path for the Streamlit UI. Builds a WHERE clause out of
    whichever filters are actually provided (None/omitted filters are
    skipped), so the UI can call this with only the filters the user has
    touched. Returns a DataFrame because that's what Streamlit and pandas
    both want directly.
    """
    where_clauses = []
    params: list = []

    if active_only:
        where_clauses.append("l.is_active = 1")
    if search:
        where_clauses.append("(l.title LIKE ? OR l.description LIKE ?)")
        like_term = f"%{search}%"
        params.extend([like_term, like_term])
    if property_type:
        where_clauses.append("l.property_type = ?")
        params.append(property_type)
    if city:
        where_clauses.append("l.city = ?")
        params.append(city)
    if district:
        where_clauses.append("l.district = ?")
        params.append(district)
    if price_min is not None:
        where_clauses.append("l.price >= ?")
        params.append(price_min)
    if price_max is not None:
        where_clauses.append("l.price <= ?")
        params.append(price_max)
    # Area filtering, with an escape hatch for listings whose area is unknown.
    #
    # A plain `area_sqm >= ?` silently drops every NULL, and that's a big deal
    # here: dealapp's map endpoint is the only way to get full coverage of that
    # site but carries no area, so most of its ~6.5k listings have none. Without
    # this option, nudging the area slider makes them all vanish with no
    # explanation. include_unknown_area keeps them in the results instead.
    area_bounds = []
    if area_min is not None:
        area_bounds.append("l.area_sqm >= ?")
        params.append(area_min)
    if area_max is not None:
        area_bounds.append("l.area_sqm <= ?")
        params.append(area_max)
    if area_bounds:
        clause = " AND ".join(area_bounds)
        if include_unknown_area:
            clause = f"(({clause}) OR l.area_sqm IS NULL)"
        where_clauses.append(clause)

    query = "SELECT DISTINCT l.* FROM listings l"

    # "Show only my liked listings" — an inner join drops everything unliked.
    if favorites_only:
        query += " JOIN favorites f ON f.listing_id = l.id"

    # Tag filtering needs a join; only added when tags are actually requested
    # so the common (untagged) query path stays a plain single-table scan.
    if tags:
        query += """
            JOIN listing_tags lt ON lt.listing_id = l.id
            JOIN custom_tags ct ON ct.id = lt.tag_id
        """
        where_clauses.append(f"ct.name IN ({','.join('?' * len(tags))})")
        params.extend(tags)

    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)

    query += " ORDER BY l.last_seen_at DESC"

    with get_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_distinct_values(column: str, active_only: bool = True) -> list:
    """
    Return the sorted distinct non-null values of one listings column, for
    populating the UI's filter dropdowns (e.g. property_type, city, district).

    `column` is validated against a fixed allow-list rather than interpolated
    blindly, because column names can't be passed as SQL parameters — the
    allow-list is what keeps this from being an injection hole.
    """
    allowed = {"property_type", "city", "district"}
    if column not in allowed:
        raise ValueError(f"column must be one of {allowed}, got {column!r}")

    sql = f"SELECT DISTINCT {column} FROM listings WHERE {column} IS NOT NULL"
    if active_only:
        sql += " AND is_active = 1"
    sql += f" ORDER BY {column}"

    with get_connection() as conn:
        rows = conn.execute(sql).fetchall()
        return [row[column] for row in rows]


def get_numeric_range(column: str, active_only: bool = True) -> tuple:
    """
    Return (min, max) of a numeric listings column, for setting the UI
    slider bounds from the real data instead of hardcoded guesses. Column
    is allow-listed (can't be a SQL parameter). Returns (0, 0) if there are
    no non-null values yet.
    """
    allowed = {"price", "area_sqm"}
    if column not in allowed:
        raise ValueError(f"column must be one of {allowed}, got {column!r}")

    sql = f"SELECT MIN({column}), MAX({column}) FROM listings WHERE {column} IS NOT NULL"
    if active_only:
        sql += " AND is_active = 1"

    with get_connection() as conn:
        low, high = conn.execute(sql).fetchone()
        return (low or 0, high or 0)


def get_numeric_percentile(column: str, percentile: float, active_only: bool = True):
    """
    Return the value at `percentile` (0-100) of a numeric column — a MAX that
    ignores absurd outliers.

    Why this exists: a handful of listings have corrupt prices (the worst is
    130 billion riyals for a 600 m² plot, ~5000x the 99.9th percentile). Using
    the true MAX as a slider bound made the price slider span 0 to 130e9 in
    50,000 steps — 2.6 million positions, so every real listing sat in the
    leftmost pixel and the control was unusable. Bounding by percentile keeps
    the slider proportioned to real data. The UI treats a maxed-out slider as
    "no upper limit", so the outliers are still reachable, just not in charge
    of the scale.

    Implemented with LIMIT/OFFSET rather than a percentile function because
    SQLite ships no PERCENTILE_CONT.
    """
    allowed = {"price", "area_sqm"}
    if column not in allowed:
        raise ValueError(f"column must be one of {allowed}, got {column!r}")
    if not 0 <= percentile <= 100:
        raise ValueError(f"percentile must be in 0..100, got {percentile!r}")

    where = f"{column} IS NOT NULL"
    if active_only:
        where += " AND is_active = 1"

    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM listings WHERE {where}"
        ).fetchone()[0]
        if not total:
            return 0
        # Clamp to the last row so percentile=100 doesn't run off the end.
        offset = min(int(total * percentile / 100), total - 1)
        row = conn.execute(
            f"SELECT {column} FROM listings WHERE {where} "
            f"ORDER BY {column} LIMIT 1 OFFSET ?",
            (offset,),
        ).fetchone()
        return row[0] if row else 0


def log_scrape_run(
    site: str,
    started_at: datetime,
    finished_at: datetime,
    listings_found: int,
    new_count: int,
    reactivated_count: int,
    deactivated_count: int,
):
    """Records a summary row for one scraper execution (used for debugging)."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO scrape_runs (
                site, started_at, finished_at, listings_found,
                new_count, reactivated_count, deactivated_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                site,
                started_at,
                finished_at,
                listings_found,
                new_count,
                reactivated_count,
                deactivated_count,
            ),
        )
