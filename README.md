# Jeddah real-estate listing scraper

Scrapes for-sale property listings in Jeddah from four sites into one SQLite
database, and browses them in an Arabic (RTL) Streamlit UI.

Scope: **Jeddah only**, **sale only**, and four property types — شقة (apartment),
فيلا (villa), أرض (land), عمارة (building).

---

## Commands

Run everything from the project root (`c:\Users\ASUS\Desktop\rag_scraping`).

### Browse the data

```powershell
streamlit run ui/app.py
```

### Scrape one site (one terminal each)

```powershell
python -m pipeline.run_scrapers --site aqar
python -m pipeline.run_scrapers --site wasalt
python -m pipeline.run_scrapers --site bayut
python -m pipeline.run_scrapers --site dealapp
python -m pipeline.run_scrapers --site dealapp-api    # after dealapp: adds area
```

wasalt and bayut open a **visible browser** — solve the Cloudflare challenge by
hand if one appears; the cookie is saved and reused from then on.

### Scrape everything in one terminal

```powershell
python -m pipeline.run_all
```

### Housekeeping

```powershell
python -m pipeline.cleanup                      # delete listings inactive > 3 days
python -m pipeline.fix_dealapp_urls --dry-run   # repair dealapp links (already applied)
```

---

## Flags

| Flag | Applies to | What it does |
|---|---|---|
| `--max-pages N` | aqar, wasalt, bayut, dealapp | Cap pages per category. Use `1` to smoke-test. |
| `--start-page N` | bayut, dealapp | **Resume** a crawl that stopped partway. |
| `--max-ads N` | dealapp-browser | How many ad pages to open. Use `5` to smoke-test. |
| `--retry-failed` | dealapp-browser | Re-attempt ads that errored before. |
| `--refresh-codes` | dealapp-browser | Re-download the sitemaps (they change daily). |

Smoke-test a site before committing to a full crawl:

```powershell
python -m pipeline.run_scrapers --site aqar --max-pages 1
python -m pipeline.run_scrapers --site dealapp-browser --max-ads 5
```

**Resuming matters.** A crawl killed partway should be resumed with
`--start-page N`, not restarted — that flag also skips the "deactivate what I
didn't see" step, which would otherwise retire every listing the partial run
never looked at.

---

## Per-site notes

### aqar.fm — plain HTTP
Four category URLs, one per property type. Fastest and least fragile.

### wasalt.sa — Playwright
Cloudflare-protected and JS-rendered; data is read from the `__NEXT_DATA__`
JSON embedded in the page.

### bayut.sa — Playwright, **visible browser**
Aggressive Cloudflare challenge, so it runs `headless=False` with a saved
profile in `data/browser_profiles/`. **If a challenge appears, solve it by hand
in that window** and the run continues; the clearance cookie is reused on later
runs.

### dealapp.sa — three scrapers, use `--site dealapp`

dealapp's list endpoint enforces a **per-account cap of ~500 ads viewed**, then
returns `403 تم الوصول للعدد الاقصى من مشاهدة الاعلانات`. A quota can't be
retried or backed-off past, hence three approaches:

| Command | What it does | Reach |
|---|---|---|
| `--site dealapp` | **Use this.** Map endpoint: whole city in ONE request. | 6.5k listings, seconds |
| `--site dealapp-api` | Adds area + real titles. Capped at ~490 ads/run. | ~490 per run |
| `--site dealapp-enrich` | Reads each ad's public page. Walled after ~6-10. | ~10 per run |
| `--site dealapp-browser` | Every ad in the sitemaps. Not worth it. | ~147 hours |

Run `dealapp` first for coverage, then the enrichers to fill in area/titles.
They write the same rows and complement each other.

**Area is the permanent sore point.** Every route to it is rate-limited:

- The API (`dealapp-api`) has the area, but caps at ~490 ads viewed per run.
  Re-running it starts from page 1 and re-spends the quota on rows you already
  have — resume with `--start-page N` past where it stopped.
- The public ad page (`dealapp-enrich`) needs no auth, but dealapp serves its
  signup page instead of the ad after ~6-10 anonymous views. The limit is
  time-based; clearing cookies or starting a new session does **not** reset it
  (measured). The scraper detects the wall and exits cleanly, so it's safe to
  run on a schedule and let area accrue slowly.
- That page has no area field at all, either — the figure only exists inside
  the advertiser's Arabic description text, so it's parsed heuristically and
  tagged `area_from_text` in `extra_attributes`.

Practical consequence: most dealapp rows have **no area**, which is why the UI's
area filter has "أضف العقارات بمساحة غير معروفة" checked by default. Untick it
to see only listings with a known area.

**Why the map endpoint wins.** `/production/ad/map?city=<id>` answers with every
ad in a city in a single response (11,355 for Jeddah → 6,637 sale listings in
our four types), ignores `page`/`limit`, and keeps working while `/ad` is
capped — map data isn't "viewing an ad".

Its records are a thin projection though: **no area, no title, no numeric
code.** Two consequences worth knowing:

- **Rows from the map have no `area_sqm`.** They're excluded the moment you
  touch the area slider in the UI, since SQL comparisons drop NULLs.
- **Titles are generated** from type + district + city (e.g. "شقة للبيع في
  الفيصلية، جدة"), flagged as `title_generated` in `extra_attributes`. The API
  scraper replaces them with the advertiser's real wording where it reaches.

Nothing is lost by running the thin scraper after the rich one: `upsert_listing`
COALESCEs every enrichment field, so a NULL means "no new information" and can
never blank a value another scraper already found.

The map approach is only possible because **`/ar/ad-details/<id>` accepts the
Mongo `_id`**, not just the numeric `code` — otherwise map-only rows would have
no working link. (The original `/ar/ad/<code>` format 404'd for every listing.)

**Why not the browser scraper.** Its discovery half is genuinely free — the
public sitemaps list every ad with no auth and no cap. But they carry **58,058
ads for the whole country**, and at ~7 ads/min that's a 147-hour crawl to find
the ~11k Jeddah ones. It's kept for the case where area matters more than time.
It resumes cleanly (progress flushed every 25 ads to
`data/dealapp_visited.json`, and `--retry-failed` re-attempts errors), and it
aborts after 15 consecutive failures instead of grinding through 58k of them.

---

## Layout

```
config/settings.py    all per-site constants (URLs, type maps, tokens)
db/schema.py          tables: listings, custom_tags, listing_tags, favorites, scrape_runs
db/database.py        the only file that runs SQL
scrapers/             base_scraper (HTTP) + playwright_scraper (browser) + one per site
pipeline/             run_scrapers (one site), run_all, cleanup, fix_dealapp_urls
ui/app.py             Streamlit UI
data/listings.db      the database
```

A listing is keyed on `(source_site, source_id)`, so re-scraping updates a row
instead of duplicating it. Scrapers only ever mark vanished listings inactive;
`pipeline/cleanup.py` is what actually deletes them, and it **never** deletes a
listing you've favorited.

## UI features

- Filter by type, district, price, area, free text, and your own categories.
- **Favorites**: the heart on each card. Sidebar has "❤️ المفضلة فقط" to show
  only liked listings (including ones that have since left the source site,
  badged as such).
- **Categories**: type a label into any card's box and press Enter. Filter by
  it in the sidebar; remove one via the 🏷️ popover.

Price/area sliders are bounded at the 99th percentile, because a few listings
carry corrupt prices (the worst is 130 billion riyals for a 600 m² plot) that
would otherwise make the sliders unusable. Leave a slider at its maximum to
mean "no upper limit", which keeps those outliers reachable.

## Setup

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```
