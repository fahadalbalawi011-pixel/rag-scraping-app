"""
Browser-based scraper skeleton for sites that block plain HTTP requests
(Cloudflare challenges, JS-rendered data, DNS only a real browser resolves).

This is a sibling to BaseScraper: it reuses BaseScraper's run() loop (save
each listing, deactivate the ones we didn't see, log the run) unchanged, but
wraps it so a real Chromium browser is open for the whole scrape. Subclasses
implement scrape_listings() using self.page (a Playwright page) instead of
the requests-based self.fetch().

Sites that need this: wasalt, bayut. Simple sites keep using BaseScraper.

Requires: pip install playwright  +  python -m playwright install chromium
"""

from playwright.sync_api import sync_playwright

from config.settings import BASE_DIR
from scrapers.base_scraper import BaseScraper

# Flags that make automated Chromium look less like a bot. Not enough on their
# own for the strictest sites (bayut), which is why those also use a saved
# profile + a one-time manual challenge solve.
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]


class PlaywrightScraper(BaseScraper):
    # Subclasses set site_name (from BaseScraper). Browser-specific knobs:
    headless = True          # set False to watch the browser (needed to solve
                             # a Cloudflare challenge by hand)
    locale = "ar-SA"
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    # When True, use a persistent on-disk browser profile so cookies (incl.
    # Cloudflare's cf_clearance) survive between runs. Sites needing a manual
    # challenge solve set this so you only solve it occasionally, not every run.
    use_profile = False

    def __init__(self):
        super().__init__()
        # These are created for the duration of run() and torn down after.
        self.browser = None
        self.context = None
        self.page = None

    @property
    def profile_dir(self) -> str:
        """On-disk folder for this site's persistent browser profile."""
        return str(BASE_DIR / "data" / "browser_profiles" / self.site_name)

    def run(self):
        """
        Launch a browser, then delegate to BaseScraper.run() (the normal
        save/deactivate/log loop). The browser stays open the whole time so
        scrape_listings() can drive it, and is always closed afterwards even
        if something fails. Uses a persistent profile when use_profile is set.
        """
        with sync_playwright() as pw:
            if self.use_profile:
                import os
                os.makedirs(self.profile_dir, exist_ok=True)
                self.context = pw.chromium.launch_persistent_context(
                    self.profile_dir,
                    headless=self.headless,
                    args=_STEALTH_ARGS,
                    locale=self.locale,
                    user_agent=self.user_agent,
                    viewport={"width": 1366, "height": 900},
                )
                self.browser = None
            else:
                self.browser = pw.chromium.launch(
                    headless=self.headless, args=_STEALTH_ARGS
                )
                self.context = self.browser.new_context(
                    locale=self.locale,
                    user_agent=self.user_agent,
                    viewport={"width": 1400, "height": 1000},
                )
            self.context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            try:
                return super().run()
            finally:
                # Swallow teardown errors. If the browser already died, close()
                # raises TargetClosedError from this finally block and REPLACES
                # whatever actually went wrong — you get "Connection closed
                # while reading from the driver" instead of the real cause.
                try:
                    self.context.close()
                except Exception as exc:
                    print(f"  (browser teardown failed, ignoring: {str(exc)[:70]})")
                self.browser = self.context = self.page = None

    def safe_content(self) -> str:
        """
        Return the page HTML, tolerating the "page is navigating" race that
        happens while a Cloudflare challenge auto-reloads. Returns "" if the
        content genuinely can't be read right now.
        """
        for _ in range(3):
            try:
                return self.page.content()
            except Exception:
                self.page.wait_for_timeout(1500)
        return ""

    def wait_until_unblocked(
        self, challenge_marker: str = "كلمة التحقق", timeout_s: int = 240
    ) -> bool:
        """
        Wait for a Cloudflare challenge to clear on the current page. If it
        doesn't auto-clear, the (visible) browser is left open so you can solve
        it by hand; we poll until the challenge marker disappears or we time
        out. Returns True once the page is past the challenge.
        """
        waited = 0
        prompted = False
        while waited < timeout_s:
            content = self.safe_content()
            if content and challenge_marker not in content:
                return True
            if not prompted:
                print(f"\n[{self.site_name}] ⚠ Cloudflare challenge detected.")
                print("  Solve it in the browser window that opened, then wait.")
                prompted = True
            self.page.wait_for_timeout(3000)
            waited += 3
        return challenge_marker not in self.safe_content()

    def goto(self, url: str, wait_ms: int = 6000, retries: int = 3) -> bool:
        """
        Navigate to url, waiting a bit for JS/Cloudflare to settle. Retries a
        few times because loads occasionally time out (slow network) or come
        back as a challenge page. Returns True if navigation succeeded.
        """
        for attempt in range(1, retries + 1):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
                self.page.wait_for_timeout(wait_ms)
                return True
            except Exception as exc:
                print(f"  goto failed ({attempt}/{retries}) {url[:80]}: {str(exc)[:60]}")
                self.page.wait_for_timeout(3000)
        return False

    def scroll_to_bottom(self, rounds: int = 8, pause_ms: int = 2500):
        """
        Scroll down repeatedly to trigger infinite-scroll loading. Each round
        scrolls a big step and waits for new content to load. Used by sites
        (like wasalt) that load more listings as you scroll rather than via
        numbered pages.
        """
        for _ in range(rounds):
            self.page.mouse.wheel(0, 25000)
            self.page.wait_for_timeout(pause_ms)
