"""
Defines the shape of the database: tables + indexes only, no data logic.
database.py is the only file that should ever import and run this.

Design notes:
- `listings` is the core table. One row = one listing from one site.
  (source_site, source_id) is unique, so re-scraping the same listing
  updates it instead of creating a duplicate.
- `extra_attributes` is a JSON text column: a catch-all for site-specific
  fields we don't want to model as real columns yet (e.g. "has_maid_room",
  "furnished"). Keeps the schema from needing a migration every time a new
  site exposes a slightly different field.
- `custom_tags` / `listing_tags` is a many-to-many pair so you (or your boss)
  can label listings with your own categories (e.g. "good layout",
  "overpriced") that the source sites don't provide.
- `favorites` is the "liked" listings. It's a separate table rather than an
  is_favorite column on listings because it's *your* data, not scraped data:
  keeping it out of the listings row means a re-scrape (which overwrites
  every scraped column) can never clobber it. One row per liked listing.
- `scrape_runs` is just a log of each scrape, useful for debugging and for
  seeing at a glance how many listings went active/inactive per run.
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS listings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identity: which site this came from and that site's own listing id.
    -- This pair is what lets us upsert instead of duplicating rows.
    source_site       TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    source_url        TEXT,

    -- Core searchable/filterable fields (the ones the plan calls out:
    -- villas, price, area). Kept as real typed columns because these are
    -- what the Streamlit filters will query directly.
    property_type     TEXT,
    title             TEXT,
    price             REAL,
    area_sqm          REAL,
    price_per_sqm     REAL,     -- computed at insert/update time, not scraped
    city              TEXT,
    district          TEXT,
    bedrooms          INTEGER,
    bathrooms         INTEGER,
    description       TEXT,

    -- Flexible bucket for anything else a scraper picks up that doesn't
    -- have a dedicated column. Stored as a JSON string, e.g.
    -- '{"furnished": true, "floor": 3}'.
    extra_attributes  TEXT,

    -- Lifecycle tracking, used by pipeline/cleanup.py to know what's still
    -- live on the source site vs. what disappeared.
    first_seen_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_active         INTEGER NOT NULL DEFAULT 1,   -- 0/1 boolean
    inactive_since    TIMESTAMP,

    UNIQUE (source_site, source_id)
);

-- Speeds up the filters the Streamlit UI will run constantly.
CREATE INDEX IF NOT EXISTS idx_listings_active        ON listings (is_active);
CREATE INDEX IF NOT EXISTS idx_listings_price          ON listings (price);
CREATE INDEX IF NOT EXISTS idx_listings_area           ON listings (area_sqm);
CREATE INDEX IF NOT EXISTS idx_listings_site_active    ON listings (source_site, is_active);

-- User-defined categories (not scraped, added manually via the UI later).
CREATE TABLE IF NOT EXISTS custom_tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE
);

-- Many-to-many link between listings and tags.
CREATE TABLE IF NOT EXISTS listing_tags (
    listing_id  INTEGER NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES custom_tags (id) ON DELETE CASCADE,
    PRIMARY KEY (listing_id, tag_id)
);

-- Listings you've "liked" in the UI. listing_id is the PRIMARY KEY, so
-- liking the same listing twice is a no-op instead of a duplicate row.
-- ON DELETE CASCADE keeps this table from accumulating rows pointing at
-- listings that no longer exist — but note purge_old_inactive() deliberately
-- refuses to delete a favorited listing in the first place, so a listing you
-- liked won't silently disappear just because it went off-market.
CREATE TABLE IF NOT EXISTS favorites (
    listing_id  INTEGER PRIMARY KEY REFERENCES listings (id) ON DELETE CASCADE,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One row per scraper execution, for debugging/monitoring cleanup behavior.
CREATE TABLE IF NOT EXISTS scrape_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    site                TEXT NOT NULL,
    started_at          TIMESTAMP NOT NULL,
    finished_at         TIMESTAMP,
    listings_found      INTEGER,
    new_count           INTEGER,
    reactivated_count   INTEGER,
    deactivated_count   INTEGER
);
"""
