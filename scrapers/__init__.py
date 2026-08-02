"""Scrapers package: site-specific scrapers built on BaseScraper."""

from scrapers.base_scraper import BaseScraper
from scrapers.playwright_scraper import PlaywrightScraper
from scrapers.aqar_scraper import AqarScraper
from scrapers.wasalt_scraper import WasaltScraper
from scrapers.bayut_scraper import BayutScraper
from scrapers.dealapp_scraper import DealappScraper
from scrapers.dealapp_map_scraper import DealappMapScraper
from scrapers.dealapp_playwright_scraper import DealappPlaywrightScraper

# The single list of every site scraper the "run all" command should execute.
# Adding a new site = write its scraper class, import it above, and append it
# here. Nothing else needs to change.
#
# DealappPlaywrightScraper is deliberately NOT in this list even though it's
# the one that can reach the whole site. It writes rows for the same
# site_name as DealappScraper, so running both in one pass would have them
# fight over the same rows, and it's a long browser crawl that's meant to be
# driven on its own (and resumed) rather than bundled into "run everything".
SCRAPERS = [
    AqarScraper,
    WasaltScraper,
    BayutScraper,
    DealappMapScraper,   # whole-city coverage in one request
    DealappScraper,      # then enriches those rows with area/title until capped
]

__all__ = [
    "BaseScraper",
    "PlaywrightScraper",
    "AqarScraper",
    "WasaltScraper",
    "BayutScraper",
    "DealappScraper",
    "DealappMapScraper",
    "DealappPlaywrightScraper",
    "SCRAPERS",
]
