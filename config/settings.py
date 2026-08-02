"""
Central place for project-wide constants.
Scrapers, the pipeline, and the Streamlit UI should all import from here
instead of hardcoding paths/numbers, so there's one spot to change them.
"""

from pathlib import Path

# Project root = two levels up from this file (config/settings.py -> project root)
BASE_DIR = Path(__file__).resolve().parent.parent

# Where the SQLite database file lives. Kept under data/ so it's easy to
# .gitignore and easy to find/delete if you want to start fresh.
DB_PATH = BASE_DIR / "data" / "listings.db"

# How many days a listing can sit "inactive" (no longer found on the source
# site) before cleanup.py permanently deletes it instead of just hiding it.
INACTIVE_RETENTION_DAYS = 3


# --- AQAR (sa.aqar.fm) scraper settings ---------------------------------

# Base URL of the site. All scraper URLs are built from this, so there's one
# spot to change if the domain ever moves.
AQAR_BASE_URL = "https://sa.aqar.fm"

# The city segment used in AQAR listing URLs. Scoped to Jeddah for now;
# adding another city later is just another entry here.
AQAR_CITY_SLUG = "جدة"

# The listing categories we care about (for sale only — no rentals).
#   key   = the URL slug AQAR uses for that category
#   value = the property_type we store in the listings table
# Stored in Arabic so the Streamlit UI can display it directly without a
# translation step (titles/districts/city are already Arabic too).
AQAR_CATEGORIES = {
    "شقق-للبيع": "شقة",     # apartment
    "فلل-للبيع": "فيلا",    # villa
    "أراضي-للبيع": "أرض",   # land
    "عمائر-للبيع": "عمارة",  # building
}

# Seconds to wait between page requests, so a full multi-page crawl doesn't
# hammer the server.
AQAR_REQUEST_DELAY = 1.5


# --- WASALT (wasalt.sa) scraper settings --------------------------------
# Wasalt blocks plain requests (Cloudflare) and renders listings via a
# JS/Next.js app, so it's scraped with Playwright (a real browser) reading
# the __NEXT_DATA__ JSON embedded in the page. This URL is the Jeddah
# "for sale" results page (all residential sale types).
# The server-rendered search endpoint. Unlike the pretty slug URL, this one
# respects a &page=N query param (1-indexed, 32 per page), so we paginate by
# incrementing it. cityId=274 is Jeddah; type=residential + propertyFor=sale
# keeps it to residential sale listings.
WASALT_SEARCH_URL = (
    "https://wasalt.sa/sale/search"
    "?type=residential&countryId=1&cityId=274&propertyFor=sale&pageSize=32"
)

# We only keep these property types (Arabic, matching wasalt's
# propertySubType values). Same four categories as AQAR, sale only.
WASALT_ALLOWED_TYPES = {"شقة", "فيلا", "أرض", "عمارة"}


# --- BAYUT (bayut.sa) scraper settings ----------------------------------
# Bayut has an aggressive Cloudflare challenge, so it's scraped with a real
# (visible) browser using a saved profile — solve the challenge by hand once
# and the clearance cookie is reused. Listings are in the HTML, tagged with
# stable aria-label attributes (Title/Price/Area/Beds/Baths/Type/Location).
# Page 1 is the base URL; later pages append صفحة-N/ (صفحة = "page").
BAYUT_JEDDAH_SALE_URL = "https://www.bayut.sa/للبيع/العقارات/جدة/"

# Same four sale categories. Bayut's Type values use these Arabic words.
BAYUT_ALLOWED_TYPES = {"شقة", "فيلا", "أرض", "عمارة"}


# --- DEALAPP (dealapp.sa) scraper settings ------------------------------
# Dealapp exposes a clean JSON API, so it's a plain requests scraper — no
# browser needed. The API requires a JWT auth token (sent as the
# Authorization header). This is a personal account token with no expiry
# claim; if the API ever starts returning 401, grab a fresh token from the
# browser's Network tab (the `ad?...` request's authorization header).
DEALAPP_API_URL = "https://api.dealapp.sa/production/ad"
DEALAPP_CITY_NAME = "جدة"
DEALAPP_TOKEN = (
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJfaWQiOiI2YTRmODViZTU1ZDQ0MzgwMDljZmQyNmQiLCJyb2xlIjoiQ0xJRU5UIiwic3RhdHVzIjoi"
    "QUNUSVZFIiwicGhvbmUiOiIrOTY2NTgwMzA2NzMwIiwibmFtZSI6ItmB2YfYryDYudio2K_Yp9mE2YTZhyIs"
    "ImlhdCI6MTc4MzU5NjQ4Nn0.M2F0kDJgJOSqteWr5SLzSpjzmZqbUYjIWP8mj5Tg6aE"
)

# Map dealapp's English propertyType enum -> our standard Arabic type. Only
# these four are kept; anything else (shops, rest houses, floors...) is dropped.
#
# CAREFUL: dealapp has no plain "LAND" or single "BUILDING" enum — it spells
# them out by usage ("RESIDENTIAL-LAND", "RESIDENTIAL-BUILDING", ...). The
# original map listed only LAND/BUILDING, so every land listing silently
# failed the lookup and was dropped: 1,875 Jeddah sale plots, and the DB had
# zero أرض rows for dealapp while the other three types came through fine.
# A missing key here is invisible in the output, so keep every spelling.
DEALAPP_TYPE_MAP = {
    # apartments
    "APARTMENT": "شقة",
    # villas
    "VILLA": "فيلا",
    # land (usage varies, it's still a plot)
    "LAND": "أرض",
    "RESIDENTIAL-LAND": "أرض",
    "COMMERCIAL-LAND": "أرض",
    "RESIDENTIAL COMMERCIAL LAND": "أرض",
    # whole buildings (usage varies, it's still a عمارة)
    "BUILDING": "عمارة",
    "RESIDENTIAL-BUILDING": "عمارة",
    "COMMERCIAL-BUILDING": "عمارة",
    "RES-COMM-BUILDING": "عمارة",
}

# Template for a listing's public URL. Verified against the site's sitemap:
# the pattern is /ar/ad-details/<numeric code>, e.g.
# https://dealapp.sa/ar/ad-details/513526. The previous guess (/ar/ad/{code})
# returned dealapp's 404 page for every listing, which is why the links in the
# Streamlit UI all read "page does not exist".
DEALAPP_LISTING_URL = "https://dealapp.sa/ar/ad-details/{code}"

# --- DEALAPP browser (Playwright) scraper --------------------------------
# The JSON list endpoint (/production/ad) enforces a per-account cap of ~500
# ads viewed and then answers 403 "تم الوصول للعدد الاقصى من مشاهدة الاعلانات",
# which is what limited the API scraper to 222 of ~11.4k listings. The browser
# scraper works around the *discovery* half of that problem: dealapp publishes
# every ad URL in its public sitemaps, which need no auth and are not capped.
DEALAPP_SITEMAP_INDEX = "https://dealapp.sa/sitemap.xml"

# Cached list of ad codes discovered from the sitemaps, so re-runs and resumes
# don't re-download 18 sitemap files.
DEALAPP_CODES_CACHE = BASE_DIR / "data" / "dealapp_codes.json"

# The map endpoint returns every Jeddah ad in ONE response (11,355 on a test
# run) and keeps working while /ad is capped, but its records are a thin
# projection with no area/title/code — useful as a cross-check, not as the
# primary source. cityId for Jeddah:
DEALAPP_MAP_API_URL = "https://api.dealapp.sa/production/ad/map"
DEALAPP_CITY_ID = "6009d942950ada00061eeeac"

# Substrings that mean "you've hit the per-user ad-view cap". Seen in the 403
# body from the API. The browser scraper watches for these to know it should
# rotate to a fresh session rather than treat the ad as broken.
DEALAPP_VIEW_CAP_MARKERS = (
    "تم الوصول للعدد الاقصى",
    "العدد الاقصى من مشاهدة",
)

# We only store Jeddah. The sitemaps cover the whole country, so each ad's
# city is checked after fetching it (the code alone doesn't reveal the city).
DEALAPP_ALLOWED_CITIES = {"جدة"}
