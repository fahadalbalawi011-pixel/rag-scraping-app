"""
Browser-based scraper for dealapp.sa, Jeddah, sale listings only.

Why a browser scraper exists alongside the API one
--------------------------------------------------
dealapp's JSON list endpoint (/production/ad, used by DealappScraper) enforces
a per-account cap: after roughly 500 ads it answers 403 "تم الوصول للعدد
الاقصى من مشاهدة الاعلانات". That's what stopped the API scraper at 222 of
~11.4k listings, and no amount of retrying or backing off defeats a quota.

This scraper attacks the problem in two independent halves:

1. DISCOVERY is free. dealapp publishes every ad URL in its public sitemaps
   (sitemap.xml -> 18 child sitemaps -> ~18k /ar/ad-details/<code> URLs). No
   auth, no cap, and plain HTTP is enough. So we never have to page through a
   capped list endpoint to find out what exists.

2. DETAIL is the capped part. Each ad page is opened in a real browser and we
   read the JSON the page itself fetches. When the cap trips we rotate the
   browser's guest identity (clear cookies + storage) and carry on rather than
   dying. If dealapp turns out to bind the cap to IP rather than to the guest
   session, rotation won't help — the log will say so plainly instead of
   silently under-collecting.

Two things follow from the sitemaps covering the whole country, not just
Jeddah: most codes we visit are discarded, and the code alone doesn't reveal
the city — so the filter can only run *after* fetching. That's why resume
state lives in its own visited-log rather than being derived from the
database: the database can't tell us about the ~7k codes we fetched, rejected,
and must not fetch again on the next run.

The run is designed to be killed and restarted freely: progress is flushed to
disk periodically and every already-attempted code is skipped on restart.
"""

import json
import re
import time

from config.settings import (
    DEALAPP_ALLOWED_CITIES,
    DEALAPP_CODES_CACHE,
    DEALAPP_LISTING_URL,
    DEALAPP_SITEMAP_INDEX,
    DEALAPP_TYPE_MAP,
    DEALAPP_VIEW_CAP_MARKERS,
    BASE_DIR,
)
from scrapers.playwright_scraper import PlaywrightScraper

# Where the resume log lives. Maps "code" -> outcome, so a restart knows both
# what it saved AND what it already rejected (see module docstring).
VISITED_PATH = BASE_DIR / "data" / "dealapp_visited.json"


class DealappPlaywrightScraper(PlaywrightScraper):
    site_name = "dealapp.sa"        # same site as DealappScraper: one row per ad
    headless = True                 # no Cloudflare challenge here, so headless is fine
    use_profile = True              # keep the guest session between runs
    request_delay = 0.0             # pacing is done per-ad by ad_delay instead

    # Seconds to wait between ad pages. dealapp answers 429 if pushed, and this
    # is a long crawl, so be unhurried by default.
    ad_delay = 1.5

    # How many consecutive cap-hits to tolerate before giving up on the run.
    # Each one triggers an identity rotation; if rotation isn't working, this
    # is what stops us from spinning forever against a wall.
    max_consecutive_caps = 6

    # Flush the visited log to disk every N ads, so a kill loses little.
    flush_every = 25

    def __init__(self, max_ads: int | None = None, retry_failed: bool = False,
                 refresh_codes: bool = False):
        """
        max_ads limits how many ad pages to open this run (None = all of them).
        Use a small number for a first check: DealappPlaywrightScraper(max_ads=5).

        retry_failed re-attempts codes that previously errored. Codes that were
        fetched and legitimately rejected (wrong city/type/purpose) are never
        retried — that's the whole point of the visited log.

        refresh_codes re-downloads the sitemaps instead of using the cached
        code list. Worth doing when resuming days later, since dealapp
        regenerates the sitemaps daily.
        """
        super().__init__()
        self.max_ads = max_ads
        self.retry_failed = retry_failed
        self.refresh_codes = refresh_codes

        # A browser crawl of ~18k ads is partial by nature — it gets killed, it
        # gets capped, it gets resumed. Deactivating "everything I didn't see"
        # after such a run would wrongly retire most of the site, so it stays
        # off unless we actually consume the entire discovered code list in one
        # run (set at the end of scrape_listings).
        self.skip_deactivate = True

        # One resume log per target mode: the two modes key by different things
        # (sitemap codes vs Mongo ids), so sharing a file would have each one's
        # entries look like unvisited ads to the other.
        self.visited_path = (
            VISITED_PATH if self.targets == "sitemap"
            else BASE_DIR / "data" / f"dealapp_visited_{self.targets}.json"
        )
        self.visited: dict = _load_json(self.visited_path, default={})
        self._pending_writes = 0
        self._consecutive_caps = 0
        self._consecutive_failures = 0
        self._stats = {"saved": 0, "rejected": 0, "failed": 0, "caps": 0}

    # Stop after this many failures in a row. Without it a broken browser or a
    # changed page just grinds through the entire code list marking everything
    # failed — 58k ads at 7/min is 147 hours of achieving nothing.
    max_consecutive_failures = 15

    # --- phase 1: discovery (plain HTTP, uncapped) ------------------------

    # Where the list of ads to visit comes from:
    #   "sitemap"      - every ad dealapp publishes (58,058, whole country)
    #   "missing-area" - only rows already in our DB that still lack an area
    # See DealappEnrichScraper for why "missing-area" is almost always right.
    targets = "sitemap"

    def discover_codes(self) -> list:
        """
        Return the list of ad ids/codes to visit, per `targets`.

        The sitemap route is complete but wildly inefficient: 58,058 ads for the
        whole country to find ~11k Jeddah ones, which measured out at ~147
        hours. The map scraper has since given us the exact Jeddah set, so
        "missing-area" walks only the rows that still need something — an order
        of magnitude less work for strictly more useful output.
        """
        if self.targets == "missing-area":
            from db.database import get_source_ids_missing_area

            ids = get_source_ids_missing_area(self.site_name)
            print(f"[{self.site_name}] {len(ids)} active listings still have no "
                  f"area — visiting only those")
            return ids

        if not self.refresh_codes:
            cached = _load_json(DEALAPP_CODES_CACHE, default=None)
            if cached:
                print(f"[{self.site_name}] using {len(cached)} cached ad codes "
                      f"(refresh_codes=True to re-download)")
                return cached

        print(f"[{self.site_name}] discovering ad codes from sitemaps...")
        index_xml = self.fetch(DEALAPP_SITEMAP_INDEX)
        if not index_xml:
            raise RuntimeError("could not download the dealapp sitemap index")

        child_sitemaps = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", index_xml)
        print(f"  sitemap index lists {len(child_sitemaps)} child sitemaps")

        codes: list = []
        seen = set()
        for url in child_sitemaps:
            xml = self.fetch(url)
            if not xml:
                print(f"  WARNING: could not download {url}")
                continue
            # Ad detail URLs look like https://dealapp.sa/ar/ad-details/513526
            # (an /en/ variant exists too; the code is the same ad).
            found = re.findall(r"/ad-details/(\d+)", xml)
            new = [c for c in found if c not in seen]
            seen.update(new)
            codes.extend(new)
            print(f"  {url.rsplit('/', 1)[-1]}: {len(found)} ad urls "
                  f"({len(new)} new, {len(codes)} total)")

        if not codes:
            raise RuntimeError(
                "no /ad-details/<code> URLs found in any sitemap — the sitemap "
                "layout has probably changed; inspect sitemap.xml by hand"
            )

        _save_json(DEALAPP_CODES_CACHE, codes)
        print(f"[{self.site_name}] discovered {len(codes)} ad codes")
        return codes

    # --- phase 2: detail (browser, capped) --------------------------------

    def scrape_listings(self):
        codes = self.discover_codes()

        todo = [c for c in codes if self._should_visit(c)]
        print(f"[{self.site_name}] {len(todo)} of {len(codes)} codes to visit "
              f"({len(self.visited)} already attempted)")
        if self.max_ads is not None:
            todo = todo[: self.max_ads]
            print(f"[{self.site_name}] limited to {len(todo)} ads this run")

        started = time.time()
        for i, code in enumerate(todo, 1):
            if self._consecutive_failures >= self.max_consecutive_failures:
                print(f"[{self.site_name}] ABORTING: {self._consecutive_failures} "
                      f"ads failed in a row — see the [fail] reasons above.\n"
                      f"  Nothing is being collected, so there's no point "
                      f"continuing through {len(todo) - i} more.\n"
                      f"  Progress is saved; --retry-failed re-attempts these "
                      f"once the cause is fixed.")
                break

            if self._consecutive_caps >= self.max_consecutive_caps:
                print(f"[{self.site_name}] ABORTING: hit the view cap "
                      f"{self._consecutive_caps}x in a row and rotating the "
                      f"guest session didn't clear it.\n"
                      f"  The cap is likely bound to your IP, not the session. "
                      f"Wait a few hours (or change network) and rerun — "
                      f"progress is saved, the rerun resumes here.")
                break

            listing = self._scrape_one(code)
            if listing is not None:
                yield listing

            if i % 20 == 0 or i == len(todo):
                rate = i / max(time.time() - started, 1e-9)
                remaining = len(todo) - i
                eta_min = (remaining / rate / 60) if rate else 0
                print(f"[{self.site_name}] {i}/{len(todo)} visited | "
                      f"saved {self._stats['saved']} · "
                      f"rejected {self._stats['rejected']} · "
                      f"failed {self._stats['failed']} · "
                      f"caps {self._stats['caps']} | "
                      f"{rate * 60:.0f} ads/min · ETA {eta_min:.0f}m")

            if self._pending_writes >= self.flush_every:
                self._flush()

        self._flush()

        # Only a run that consumed the whole discovered list is a complete pass
        # over the site, and only then is "deactivate what I didn't see" valid.
        consumed_everything = (
            self.max_ads is None
            and self._consecutive_caps < self.max_consecutive_caps
            and all(not self._should_visit(c) for c in codes)
        )
        self.skip_deactivate = not consumed_everything
        print(f"[{self.site_name}] {'full pass complete' if consumed_everything else 'partial pass'}"
              f" — deactivation {'enabled' if consumed_everything else 'skipped'}")

    def _should_visit(self, code: str) -> bool:
        outcome = self.visited.get(str(code))
        if outcome is None:
            return True
        if outcome == "failed" and self.retry_failed:
            return True
        return False

    def _scrape_one(self, code: str):
        """
        Open one ad page, read the ad JSON the page fetched, and return a
        listing dict — or None if the ad was rejected, failed, or capped.
        Records the outcome in the visited log either way.
        """
        url = DEALAPP_LISTING_URL.format(code=code)
        ad, problem = self._fetch_ad_json(url)

        if problem == "cap":
            self._stats["caps"] += 1
            self._consecutive_caps += 1
            print(f"  [cap] view limit hit on {code} "
                  f"({self._consecutive_caps}/{self.max_consecutive_caps}) "
                  f"— rotating guest session")
            self._rotate_identity()
            return None                      # not marked visited: retry later

        if problem or ad is None:
            self._stats["failed"] += 1
            self._consecutive_failures += 1
            # Print the reason. The first version counted failures but never
            # said why, so a run that failed 289 ads in a row looked identical
            # to one doing useful work — the counter just ticked up. Show the
            # first few in full, then only occasionally, so a genuinely broken
            # run is obvious immediately without flooding a healthy one.
            if self._consecutive_failures <= 3 or self._stats["failed"] % 25 == 0:
                print(f"  [fail] {code}: {problem}")
            self._mark(code, "failed")
            return None

        self._consecutive_caps = 0            # a clean fetch clears the streak
        self._consecutive_failures = 0

        listing = self._map_ad(ad, code)
        if listing is None:
            self._stats["rejected"] += 1
            self._mark(code, "rejected")
            return None

        self._stats["saved"] += 1
        self._mark(code, "saved")
        return listing

    def _fetch_ad_json(self, url: str):
        """
        Navigate to an ad page and capture the ad object.

        We don't hardcode dealapp's detail endpoint — we listen to what the
        page actually calls and keep any api.dealapp.sa response that looks
        like a single ad. That way a change to the API path doesn't break this,
        and we get the same rich fields the API scraper sees (title, area,
        rooms) rather than re-parsing rendered HTML.

        Returns (ad_dict_or_None, problem) where problem is None, "cap", or an
        error string.
        """
        captured: dict = {}
        cap_seen: list = []

        def on_response(response):
            if "api.dealapp.sa" not in response.url:
                return
            try:
                if response.status == 403:
                    body = response.text()
                    if any(m in body for m in DEALAPP_VIEW_CAP_MARKERS):
                        cap_seen.append(True)
                    return
                if response.status != 200:
                    return
                payload = response.json()
            except Exception:
                return
            # The ad may arrive bare or wrapped in {"data": {...}}.
            for candidate in (payload, payload.get("data") if isinstance(payload, dict) else None):
                if _looks_like_ad(candidate):
                    captured["ad"] = candidate
                    return

        self.page.on("response", on_response)
        try:
            time.sleep(self.ad_delay)
            if not self.goto(url, wait_ms=2500, retries=2):
                return None, "navigation failed"

            # Give the page a moment more if the ad call hasn't landed yet.
            for _ in range(8):
                if captured or cap_seen:
                    break
                self.page.wait_for_timeout(500)

            if cap_seen and not captured:
                return None, "cap"
            if captured:
                return captured["ad"], None

            # Fallback: some renders inline the ad in the HTML instead of
            # fetching it (Next.js hydration payload).
            inline = _ad_from_html(self.safe_content())
            if inline is not None:
                return inline, None

            html = self.safe_content()
            if any(m in html for m in DEALAPP_VIEW_CAP_MARKERS):
                return None, "cap"
            if "404" in html and "الذهاب للصفحة الرئيسية" in html:
                return None, "ad no longer exists (404)"
            return None, "no ad json captured"
        except Exception as exc:
            return None, f"error: {str(exc)[:80]}"
        finally:
            # Always detach: leaking one handler per ad would make the browser
            # slower and slower over an 18k-ad crawl.
            try:
                self.page.remove_listener("response", on_response)
            except Exception:
                pass

    def _rotate_identity(self):
        """
        Drop the guest identity so dealapp issues a fresh one, resetting the
        per-user view counter. Cheaper and less fragile than tearing the whole
        browser context down and rebuilding it mid-crawl.
        """
        try:
            self.context.clear_cookies()
            self.page.evaluate(
                "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
            )
        except Exception as exc:
            print(f"  (identity rotation failed: {str(exc)[:60]})")
        # A short pause: hammering straight through a 403 tends to extend it.
        time.sleep(20)

    # --- mapping + validation --------------------------------------------

    def _map_ad(self, ad: dict, code: str):
        """
        Turn a dealapp ad into our listing dict, or None if it doesn't belong
        in the database (wrong city, rent, or a property type we don't track).

        Same shape and same filters as DealappScraper._map_ad, so both scrapers
        can write rows for this site interchangeably.
        """
        if ad.get("purpose") != "SALE":
            return None

        city = (ad.get("city") or {}).get("name_ar")
        if DEALAPP_ALLOWED_CITIES and city not in DEALAPP_ALLOWED_CITIES:
            return None

        english = (ad.get("propertyType") or {}).get("propertyType")
        property_type = DEALAPP_TYPE_MAP.get(english)
        if property_type is None:
            return None

        source_id = str(ad.get("id") or ad.get("_id") or "")
        if not source_id:
            return None                       # no stable key -> can't upsert safely

        questions = ad.get("relatedQuestions") or {}
        rega = ad.get("regaRawData") or {}

        price = _num(ad.get("price")) or _num(rega.get("propertyPrice"))
        area = _num(ad.get("area")) or _num(rega.get("propertyArea"))

        # A sale listing with no price and no area carries nothing worth
        # filtering or comparing on, so treat it as a failed parse rather than
        # quietly storing an empty row.
        if price is None and area is None:
            return None

        return {
            "source_site": self.site_name,
            "source_id": source_id,
            "source_url": DEALAPP_LISTING_URL.format(code=ad.get("code") or code),
            "property_type": property_type,
            "title": ad.get("title"),
            "price": price,
            "area_sqm": area,
            "city": city,
            "district": (ad.get("district") or {}).get("name_ar"),
            "bedrooms": _num(questions.get("roomsNum")) or _num(rega.get("numberOfRooms")),
            "bathrooms": _num(questions.get("toiletsNum")),
            "description": ad.get("title"),
            "extra_attributes": {
                "code": ad.get("code") or code,
                "advertiser": (ad.get("advertiser") or {}).get("name"),
                "property_age": rega.get("propertyAge"),
                "street_width": rega.get("streetWidth"),
                "source": "playwright",
            },
        }

    # --- resume log -------------------------------------------------------

    def _mark(self, code: str, outcome: str):
        self.visited[str(code)] = outcome
        self._pending_writes += 1

    def _flush(self):
        if self._pending_writes:
            _save_json(self.visited_path, self.visited)
            self._pending_writes = 0


class DealappEnrichScraper(DealappPlaywrightScraper):
    """
    Fill in the area (and real titles) that the map endpoint can't provide.

    The three dealapp scrapers divide up like this:
      - DealappMapScraper    complete coverage, one request, but no area
      - DealappScraper       has area, but the API caps out at ~490 ads per run
      - this one             visits only the rows still missing an area

    That last point is what makes it practical. Pointed at the sitemaps it had
    58,058 ads to chew through; pointed at "what's actually still missing" it
    has however many rows are left — and every page it loads produces something
    the database didn't already have.

    Because the worklist comes from the database, progress is inherently
    resumable: once a row gets its area it drops off the list, so a killed run
    simply picks up with whatever is still outstanding.
    """

    targets = "missing-area"


# --- helpers -------------------------------------------------------------

def _looks_like_ad(obj) -> bool:
    """
    True if this JSON object is a single property ad. Used to pick the ad call
    out of every other request the page makes (config, categories, banners...).
    Deliberately structural rather than URL-based, so it survives an API path
    change.
    """
    if not isinstance(obj, dict):
        return False
    if not isinstance(obj.get("propertyType"), dict):
        return False
    return "purpose" in obj and ("price" in obj or "regaRawData" in obj)


def _ad_from_html(html: str):
    """
    Last-resort extraction: pull the ad object out of a hydration payload
    embedded in the page (e.g. __NEXT_DATA__) when no API call was observed.
    Returns None if nothing ad-shaped is found.
    """
    if not html:
        return None
    for match in re.finditer(r'<script[^>]*>\s*(\{.*?\})\s*</script>', html, re.S):
        try:
            data = json.loads(match.group(1))
        except Exception:
            continue
        found = _find_ad(data)
        if found is not None:
            return found
    return None


def _find_ad(node, depth: int = 0):
    """Depth-limited search for the first ad-shaped dict in nested JSON."""
    if depth > 8:
        return None
    if _looks_like_ad(node):
        return node
    if isinstance(node, dict):
        for value in node.values():
            found = _find_ad(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node[:50]:
            found = _find_ad(value, depth + 1)
            if found is not None:
                return found
    return None


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    # Atomic replace, so a kill mid-write can't leave a truncated resume log.
    import os

    os.replace(tmp, path)
