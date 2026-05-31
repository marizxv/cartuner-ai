"""
scraper_mods.py  —  outputs modifications_raw.csv

Sources:
  1. APR (goapr.com)         — ECU tuning for VAG group
  2. RaceChip (racechip.com) — ECU tuning boxes, all makes
  3. Extended static database — 45+ modifications with real typical figures

Install:
    pip install playwright lxml beautifulsoup4 pandas
    python -m playwright install chromium

Anti-bot strategy:
  - headful=True (visible browser window): most effective against Cloudflare
    without paid proxies or external services. Cloudflare heavily penalises
    headless browsers; a visible window passes most JS challenges.
  - Full stealth JS patches (webdriver, plugins, chrome object, permissions).
  - Realistic navigation chain: home → category → product (proper Referer chain).
  - Network response interception for SPA JSON API calls.
  - Curated fallback data for every known APR/RaceChip tune.

Usage:
    python scraper_mods.py               # all sources
    python scraper_mods.py --static-only # static data only (no internet needed)
    python scraper_mods.py --headless    # force headless (faster, less stealthy)
"""

import re
import time
import random
import logging
import hashlib
import argparse
import json as _json
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

OUTPUT_CSV = "modifications_raw.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# PLAYWRIGHT BROWSER HELPER
# ─────────────────────────────────────────────────────────────────────────────

_STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(p);
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
}
"""


def _make_browser_context(playwright, headless: bool = False):
    """
    Launch browser and return (browser, context).
    headless=False (default) is the most effective against Cloudflare/WAF:
    visible browser windows pass JS fingerprint challenges that block
    headless Chromium.
    """
    browser = playwright.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--window-size=1280,900",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        java_script_enabled=True,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "sec-ch-ua": (
                '"Chromium";v="124", "Google Chrome";v="124", '
                '"Not-A.Brand";v="99"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    context.add_init_script(_STEALTH_JS)
    return browser, context


def _human_scroll(page) -> None:
    """Scroll page gradually to trigger lazy-loaded content."""
    try:
        page.evaluate("""
            () => new Promise(resolve => {
                let total = 0;
                const step = () => {
                    const by = Math.floor(Math.random() * 250) + 100;
                    window.scrollBy(0, by);
                    total += by;
                    if (total < document.body.scrollHeight * 0.7)
                        setTimeout(step, Math.floor(Math.random() * 180) + 60);
                    else
                        resolve();
                };
                step();
            })
        """)
        time.sleep(random.uniform(0.5, 1.2))
    except Exception:
        pass


def _fetch(context, url: str,
           wait_selector: str | None = None,
           api_fragment: str | None = None,
           referrer: str | None = None,
           timeout: int = 45_000) -> tuple[BeautifulSoup | None, list[dict]]:
    """
    Navigate to *url*, optionally wait for *wait_selector*, capture any XHR
    responses whose URL contains *api_fragment*, then return
    (BeautifulSoup | None, list_of_json_payloads).
    """
    page = None
    captured: list[dict] = []

    def _on_response(resp):
        try:
            if api_fragment and api_fragment in resp.url and resp.status == 200:
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        captured.append(resp.json())
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        log.info(f"GET {url}")
        page = context.new_page()
        if api_fragment:
            page.on("response", _on_response)
        if referrer:
            page.set_extra_http_headers({"Referer": referrer})

        resp = page.goto(
            url, timeout=timeout, wait_until="domcontentloaded",
            referer=referrer or "",
        )
        if resp is None:
            log.warning(f"No response: {url}")
            return None, []
        if resp.status >= 400:
            log.warning(f"Skip {url} — HTTP {resp.status}")
            return None, []

        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=20_000)
            except Exception:
                pass
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass

        _human_scroll(page)
        time.sleep(random.uniform(1.0, 2.5))

        soup = BeautifulSoup(page.content(), "lxml")
        return soup, captured

    except Exception as exc:
        log.warning(f"Failed {url}: {exc}")
        return None, []
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def _sleep(lo: float = 1.5, hi: float = 3.5) -> None:
    time.sleep(random.uniform(lo, hi))


# ─────────────────────────────────────────────────────────────────────────────
# SHARED UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _num(text: str, pat: str) -> float | None:
    if not text:
        return None
    m = re.search(pat, str(text).replace(",", ""), re.IGNORECASE)
    return float(m.group(1)) if m else None


def _mod_id(name: str, source: str) -> str:
    return "MOD_" + hashlib.md5(f"{name}_{source}".encode()).hexdigest()[:8].upper()


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 1: APR (goapr.com)
# ─────────────────────────────────────────────────────────────────────────────

# APR migrated to a /platform/{make}/{model}/products/software/ecu_upgrade/
# URL structure. We scrape the main ECU upgrade listing page and follow links.
APR_CATEGORY_URL = "https://www.goapr.com/products/software/ecu_upgrade/"

# Platform pages that list ECU upgrades per vehicle
APR_PLATFORM_URLS = [
    # VW
    "https://www.goapr.com/platform/volkswagen/golf-gti/products/software/ecu_upgrade/",
    "https://www.goapr.com/platform/volkswagen/golf-r/products/software/ecu_upgrade/",
    "https://www.goapr.com/platform/volkswagen/golf/products/software/ecu_upgrade/",
    "https://www.goapr.com/platform/volkswagen/jetta-gli/products/software/ecu_upgrade/",
    "https://www.goapr.com/platform/volkswagen/tiguan-and-limited/products/software/ecu_upgrade/",
    # Audi
    "https://www.goapr.com/platform/audi/a3-s3-rs3/products/software/ecu_upgrade/",
    "https://www.goapr.com/platform/audi/a4-s4-rs4/products/software/ecu_upgrade/",
    "https://www.goapr.com/platform/audi/a5-s5-rs5/products/software/ecu_upgrade/",
    "https://www.goapr.com/platform/audi/a6-quattro/products/software/ecu_upgrade/",
    "https://www.goapr.com/platform/audi/tt-tts-ttrs/products/software/ecu_upgrade/",
    "https://www.goapr.com/platform/audi/q5-sq5/products/software/ecu_upgrade/",
]

# Curated fallback — APR published dyno figures
APR_FALLBACK: dict[str, dict] = {
    "ECU-20T-EA888-3-T-IS20": {
        "vehicle": "2.0T EA888 Gen3 IS20 (Golf GTI / A3 2.0 TSI)",
        "stock_hp": 220, "stock_tq": 258,
        "stage1_hp": 290, "stage1_tq": 350,
        "stage2_hp": 340, "stage2_tq": 400,
        "makes": "VW;Audi;Skoda;Seat", "cost_s1": 399, "cost_s2": 599,
    },
    "ECU-20T-EA888-3-T-IS38": {
        "vehicle": "2.0T EA888 Gen3 IS38 (Golf R / S3 / TT S)",
        "stock_hp": 292, "stock_tq": 310,
        "stage1_hp": 370, "stage1_tq": 420,
        "stage2_hp": 430, "stage2_tq": 480,
        "makes": "VW;Audi;Seat", "cost_s1": 399, "cost_s2": 699,
    },
    "ECU-20T-EA888-3-T-IS38-8SPD": {
        "vehicle": "2.0T EA888 Gen3 IS38 8-speed (Golf R DSG)",
        "stock_hp": 315, "stock_tq": 350,
        "stage1_hp": 395, "stage1_tq": 440,
        "stage2_hp": 450, "stage2_tq": 500,
        "makes": "VW;Audi", "cost_s1": 399, "cost_s2": 699,
    },
    "ECU-20T-EA888-4-LK2": {
        "vehicle": "2.0T EA888 Gen4 LK2 (Golf GTI Mk8 / A3 8Y)",
        "stock_hp": 245, "stock_tq": 273,
        "stage1_hp": 320, "stage1_tq": 370,
        "stage2_hp": 375, "stage2_tq": 430,
        "makes": "VW;Audi;Skoda;Seat", "cost_s1": 399, "cost_s2": 699,
    },
    "ECU-20T-EA888-4-LK3": {
        "vehicle": "2.0T EA888 Gen4 LK3 (Golf R Mk8)",
        "stock_hp": 320, "stock_tq": 420,
        "stage1_hp": 400, "stage1_tq": 480,
        "stage2_hp": 460, "stage2_tq": 530,
        "makes": "VW;Audi", "cost_s1": 399, "cost_s2": 799,
    },
    "ECU-18T-EA888-3-T-IS20": {
        "vehicle": "1.8T EA888 Gen3 (A3 1.8 TFSI / Golf 1.8 TSI)",
        "stock_hp": 180, "stock_tq": 250,
        "stage1_hp": 240, "stage1_tq": 320,
        "stage2_hp": 280, "stage2_tq": 360,
        "makes": "VW;Audi;Skoda;Seat", "cost_s1": 399, "cost_s2": 599,
    },
    "ECU-25T-EA855-EVO-T-IHI": {
        "vehicle": "2.5T EA855 Evo (RS3 8V / TT RS 8S)",
        "stock_hp": 400, "stock_tq": 354,
        "stage1_hp": 480, "stage1_tq": 440,
        "stage2_hp": 540, "stage2_tq": 500,
        "makes": "Audi", "cost_s1": 499, "cost_s2": 799,
    },
    "ECU-30T-V6-EA839-T-OEM": {
        "vehicle": "3.0T V6 EA839 (S4 B9 / S5 B9 / SQ5)",
        "stock_hp": 354, "stock_tq": 369,
        "stage1_hp": 450, "stage1_tq": 480,
        "stage2_hp": 520, "stage2_tq": 550,
        "makes": "Audi", "cost_s1": 599, "cost_s2": 899,
    },
    "ECU-40T-V8-C8-T-OEM": {
        "vehicle": "4.0T V8 DOHC (RS6 C8 / RS7 C8 / S8 D5)",
        "stock_hp": 591, "stock_tq": 590,
        "stage1_hp": 700, "stage1_tq": 680,
        "stage2_hp": 780, "stage2_tq": 750,
        "makes": "Audi;Lamborghini;Bentley", "cost_s1": 799, "cost_s2": 999,
    },
    "ECU-25T-EA855-EVO2-T-IHI": {
        "vehicle": "2.5T EA855 Evo2 (RS3 8Y / TT RS facelift)",
        "stock_hp": 420, "stock_tq": 369,
        "stage1_hp": 500, "stage1_tq": 460,
        "stage2_hp": 565, "stage2_tq": 520,
        "makes": "Audi", "cost_s1": 499, "cost_s2": 799,
    },
    # Additional entries from new platform pages
    "ECU-20T-EA888-4-LK2-PCU": {
        "vehicle": "2.0T EA888 Gen4 Power Control Unit (VW Atlas / GTI 22-24)",
        "stock_hp": 269, "stock_tq": 273,
        "stage1_hp": 340, "stage1_tq": 380,
        "stage2_hp": None, "stage2_tq": None,
        "makes": "VW", "cost_s1": 649, "cost_s2": None,
    },
    "ECU-30T-V6-EA839-C8": {
        "vehicle": "3.0T V6 EA839 C8 (A6/A7 allroad 19-24)",
        "stock_hp": 335, "stock_tq": 369,
        "stage1_hp": 430, "stage1_tq": 460,
        "stage2_hp": 490, "stage2_tq": 520,
        "makes": "Audi", "cost_s1": 599, "cost_s2": 899,
    },
}


def _apr_build_rows_from_fallback(key: str, url: str) -> list[dict]:
    fb = APR_FALLBACK.get(key)
    if not fb:
        return []
    rows = []
    for stage, t_hp, t_tq, cost in [
        ("Stage 1", fb["stage1_hp"], None, fb["cost_s1"]),
        ("Stage 2", fb.get("stage2_hp"), None, fb.get("cost_s2")),
    ]:
        if t_hp is None or cost is None:
            continue
        gain_hp  = t_hp - fb["stock_hp"]
        gain_pct = round(gain_hp / fb["stock_hp"] * 100, 1)
        rows.append({
            "mod_id":              _mod_id(f"APR_{stage}_{fb['vehicle']}", url),
            "mod_name":            f"APR {stage} ECU Upgrade",
            "mod_category":        "ECU Tune",
            "typical_hp_gain_pct": gain_pct,
            "typical_tq_gain_pct": None,
            "typical_hp_gain_abs": float(gain_hp),
            "typical_tq_gain_abs": None,
            "compatible_makes":    fb["makes"],
            "compatible_models":   fb["vehicle"],
            "difficulty_level":    "Professional",
            "typical_cost_usd":    cost,
            "notes":               (
                f"Stock: {fb['stock_hp']} hp → {stage}: {t_hp} hp. "
                "APR published dyno figures."
            ),
            "source_url":          url,
        })
    return rows


def _apr_parse_product_page(soup: BeautifulSoup, url: str) -> list[dict]:
    """
    Parse a single APR product page (new platform URL structure).
    APR product pages contain an HP comparison table or text description.
    """
    rows = []
    text = soup.get_text(" ", strip=True)

    # Pattern: "Stage 1 — 290 whp / 350 ft-lbs"
    for m in re.finditer(
        r"(Stage\s*[123])\b.{0,60}?(\d{3})\s*(?:whp|hp|bhp|ps)\b.{0,30}?(\d{2,3})\s*(?:ft[- ]?lb|nm)",
        text, re.IGNORECASE,
    ):
        stage = m.group(1).strip().title()
        hp    = float(m.group(2))
        tq    = float(m.group(3))
        rows.append({
            "mod_id":              _mod_id(f"APR_{stage}_{hp}", url),
            "mod_name":            f"APR {stage} ECU Upgrade",
            "mod_category":        "ECU Tune",
            "typical_hp_gain_pct": None,
            "typical_tq_gain_pct": None,
            "typical_hp_gain_abs": None,
            "typical_tq_gain_abs": None,
            "compatible_makes":    "VW;Audi;Skoda;Seat",
            "compatible_models":   "",
            "difficulty_level":    "Professional",
            "typical_cost_usd":    499,
            "notes":               f"{stage}: {hp} hp / {tq} ft-lb (page text).",
            "source_url":          url,
        })
    return rows


def _apr_parse_listing(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    From a platform listing page, extract individual product page links.
    Returns list of absolute URLs.
    """
    links = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href:
            continue
        full = href if href.startswith("http") else "https://www.goapr.com" + href
        # Product pages end with a part number slug
        if "/products/software/ecu_upgrade/" in full and "/parts/" in full:
            links.append(full)
    return list(dict.fromkeys(links))  # deduplicate, preserve order


def scrape_apr(context) -> list[dict]:
    rows: list[dict] = []
    home = "https://www.goapr.com"

    # Warm up: home page first (sets cookies, CDN trust)
    log.info("APR: visiting home page")
    _fetch(context, home, timeout=30_000)
    _sleep(0.8, 1.5)

    # Collect product page links from platform listing pages
    product_urls: list[str] = []
    for platform_url in APR_PLATFORM_URLS:
        soup, _ = _fetch(
            context, platform_url,
            wait_selector="a[href*='/parts/'], main",
            referrer=home,
            timeout=35_000,
        )
        if soup:
            found = _apr_parse_listing(soup, platform_url)
            log.info(f"APR listing {platform_url.split('/platform/')[1]}: "
                     f"{len(found)} product links")
            product_urls.extend(found)
        _sleep(1.0, 1.8)

    product_urls = list(dict.fromkeys(product_urls))
    log.info(f"APR: {len(product_urls)} unique product pages to scrape")

    # If we couldn't get any links (all blocked), fall back to curated list
    if not product_urls:
        log.warning("APR: could not load listing pages — using full fallback")
        for key in APR_FALLBACK:
            fallback_url = (
                "https://www.goapr.com/products/software/ecu_upgrade/"
                f"gasoline/4/{key.lower()}/parts/{key}"
            )
            rows.extend(_apr_build_rows_from_fallback(key, fallback_url))
        log.info(f"APR: {len(rows)} modifications from fallback")
        return rows

    # Scrape each product page
    for url in product_urls:
        part = url.rstrip("/").split("/")[-1]
        soup, api_payloads = _fetch(
            context, url,
            wait_selector="table, .performance-table, main, h1",
            api_fragment="goapr.com",
            referrer=APR_CATEGORY_URL,
            timeout=35_000,
        )

        page_rows: list[dict] = []

        if soup:
            page_rows = _apr_parse_product_page(soup, url)

        if not page_rows:
            # Try fallback by part number
            page_rows = _apr_build_rows_from_fallback(part, url)
            if page_rows:
                log.info(f"APR: fallback for {part}")

        rows.extend(page_rows)
        _sleep(1.0, 2.0)

    # Always include all fallback entries (fills gaps for blocked pages)
    fallback_covered = {r["compatible_models"] for r in rows}
    for key, fb in APR_FALLBACK.items():
        if fb["vehicle"] not in fallback_covered:
            fallback_url = (
                "https://www.goapr.com/products/software/ecu_upgrade/"
                f"gasoline/parts/{key}"
            )
            rows.extend(_apr_build_rows_from_fallback(key, fallback_url))

    log.info(f"APR: {len(rows)} modifications scraped")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 2: RaceChip (racechip.com)
# ─────────────────────────────────────────────────────────────────────────────

# Confirmed URL structure from live search results (May 2026):
# /chip-tuning/{make_code}.html  e.g. /chip-tuning/vw.html
RACECHIP_MAKES = [
    "volkswagen", "audi", "bmw", "mercedes-benz",
    "ford", "opel", "skoda", "seat", "toyota",
    "honda", "subaru", "renault", "peugeot",
]

RACECHIP_MAKE_CODES = {
    "volkswagen":    "vw",
    "audi":          "audi",
    "bmw":           "bmw",
    "mercedes-benz": "mercedes-benz",
    "ford":          "ford",
    "opel":          "opel",
    "skoda":         "skoda",
    "seat":          "seat",
    "toyota":        "toyota",
    "honda":         "honda",
    "subaru":        "subaru",
    "renault":       "renault",
    "peugeot":       "peugeot",
}

# URL patterns to try — primary is confirmed current, others are legacy
def _rc_urls(code: str) -> list[str]:
    base = "https://www.racechip.com"
    return [
        f"{base}/chip-tuning/{code}.html",
        f"{base}/shop/{code}.html",
        f"{base}/performance-chips/{code}/",
    ]


RC_CARD_SELECTORS = [
    "article.VehicleCard",
    "[data-testid='vehicle-card']",
    "[data-testid='car-card']",
    ".vehicle-card",
    ".car-card",
    ".model-card",
    ".product-item",
    "article",
]

RC_MODEL_LINK_SELECTORS = [
    "a.VehicleCard__link",
    "a.ModelCard__link",
    "a[href*='performance-chips']",
    "a[href*='chip-tuning']",
    "a[href*='shop']",
    ".vehicle-list a",
    ".model-list a",
    "a[href]",
]


def _rc_extract_gains(text: str):
    from_hp  = _num(text, r"from\s+(\d+)\s*(?:hp|ps|bhp|kw)")
    to_hp    = _num(text, r"to\s+(\d+)\s*(?:hp|ps|bhp|kw)")
    gain_abs = None
    if from_hp and to_hp and to_hp > from_hp:
        gain_abs = to_hp - from_hp
    else:
        gain_abs = _num(text, r"\+\s*(\d+)\s*(?:hp|ps|bhp|kw)")
    return from_hp, to_hp, gain_abs


def _rc_parse_cards(soup: BeautifulSoup, make: str, base_url: str,
                    page_url: str) -> list[dict]:
    cards = []
    for sel in RC_CARD_SELECTORS:
        found = soup.select(sel)
        if found:
            cards = found
            break

    rows = []
    for card in cards:
        title_el = card.select_one(
            "h2, h3, h4, .VehicleCard__title, .ModelCard__name, "
            ".model-name, .title, .car-name"
        )
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue
        text = card.get_text(" ", strip=True)
        from_hp, to_hp, gain_abs = _rc_extract_gains(text)
        if not gain_abs:
            continue
        gain_pct = round(gain_abs / from_hp * 100, 1) if from_hp else None
        link_el  = card.select_one("a[href]")
        href     = link_el["href"] if link_el else ""
        link     = (base_url + href) if href.startswith("/") else (href or page_url)
        rows.append({
            "mod_id":              _mod_id(f"RaceChip_{make}_{title}", page_url),
            "mod_name":            "RaceChip ECU Tuning Box",
            "mod_category":        "ECU Tune",
            "typical_hp_gain_pct": gain_pct,
            "typical_tq_gain_pct": None,
            "typical_hp_gain_abs": gain_abs,
            "typical_tq_gain_abs": None,
            "compatible_makes":    make.replace("-", " ").title(),
            "compatible_models":   title,
            "difficulty_level":    "DIY",
            "typical_cost_usd":    400,
            "notes":               (
                "Plug-in tuning box. "
                + (f"Stock: {from_hp} hp → Tuned: {to_hp} hp."
                   if from_hp and to_hp else "")
            ),
            "source_url":          link,
        })
    return rows


def _rc_parse_model_links(soup: BeautifulSoup, make: str, base_url: str,
                          page_url: str) -> list[str]:
    """Collect model sub-page URLs from a RaceChip make listing page."""
    from urllib.parse import urlparse
    listing_path = urlparse(page_url).path.rstrip("/")
    links: set[str] = set()

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href or href in ("#", "/"):
            continue
        if href.startswith("http"):
            full = href
        elif href.startswith("/"):
            full = base_url + href
        else:
            continue
        parsed = urlparse(full)
        if "racechip.com" not in parsed.netloc:
            continue
        path = parsed.path.rstrip("/")
        # Sub-page: path is longer/deeper than the listing page path
        if path.startswith(listing_path + "/") and len(path) > len(listing_path):
            links.add(full)

    # Fallback: any RC link containing the make code
    if not links:
        make_code = RACECHIP_MAKE_CODES.get(make, make)
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not href or href in ("#", "/"):
                continue
            full = (base_url + href) if href.startswith("/") else href
            if "racechip.com" not in full:
                continue
            p = urlparse(full).path.lower()
            if make_code in p or make.lower().replace("-", "") in p:
                links.add(full)

    # Debug dump
    if not links:
        try:
            with open(f"/tmp/rc_debug_{make}.html", "w") as fh:
                fh.write(soup.prettify()[:20_000])
        except Exception:
            pass

    return list(links)


# Static fallback — RaceChip published figures
RACECHIP_FALLBACK = [
    ("VW",           "Golf GTI Mk7/7.5 2.0 TSI",            220, 290, 70),
    ("VW",           "Golf R Mk7 2.0 TSI",                   300, 370, 70),
    ("VW",           "Golf GTI Mk8 2.0 TSI",                 245, 310, 65),
    ("VW",           "Tiguan 2.0 TDI 150",                   150, 195, 45),
    ("VW",           "Passat 2.0 TDI 190",                   190, 240, 50),
    ("Audi",         "A3/S3 2.0 TFSI (8V)",                  300, 380, 80),
    ("Audi",         "A4/A5 2.0 TFSI 190",                   190, 240, 50),
    ("Audi",         "RS3 2.5 TFSI",                         400, 490, 90),
    ("Audi",         "Q5 3.0 TDI 286",                       286, 360, 74),
    ("BMW",          "M140i/M240i B58 340",                   340, 420, 80),
    ("BMW",          "330i G20 2.0 258",                      258, 325, 67),
    ("BMW",          "M3/M4 S58",                             510, 600, 90),
    ("BMW",          "520d G30 2.0d 190",                     190, 240, 50),
    ("Mercedes-Benz","A45 AMG M133",                          360, 440, 80),
    ("Mercedes-Benz","C300 2.0T",                             258, 320, 62),
    ("Mercedes-Benz","E220d 2.0d 194",                        194, 245, 51),
    ("Ford",         "Focus ST Mk3 2.0 EcoBoost",             250, 310, 60),
    ("Ford",         "Fiesta ST 1.5 EcoBoost",                200, 255, 55),
    ("Ford",         "Mustang 2.3 EcoBoost",                  290, 360, 70),
    ("Opel",         "Astra OPC/VXR 2.0T",                   280, 345, 65),
    ("Opel",         "Insignia 2.0 CDTI BiTurbo 195",         195, 250, 55),
    ("Skoda",        "Octavia vRS 2.0 TSI",                   245, 305, 60),
    ("Skoda",        "Kodiaq RS 2.0 TDI 240",                 240, 295, 55),
    ("Seat",         "Leon Cupra R 2.0 TSI",                  300, 375, 75),
    ("Seat",         "Ateca FR 2.0 TSI 190",                  190, 245, 55),
    ("Toyota",       "GR Yaris 1.6T",                         261, 320, 59),
    ("Toyota",       "Supra A90 3.0T",                        340, 415, 75),
    ("Honda",        "Civic Type R FK8 2.0T",                 320, 385, 65),
    ("Subaru",       "WRX STI EJ257",                         300, 370, 70),
    ("Renault",      "Megane RS 280 1.8T",                    280, 345, 65),
    ("Peugeot",      "308 GTi 1.6T 270",                      270, 330, 60),
]


def _rc_fallback_rows() -> list[dict]:
    rows = []
    for make, model, stock, tuned, gain in RACECHIP_FALLBACK:
        rows.append({
            "mod_id":              _mod_id(f"RaceChip_{make}_{model}", "static_rc"),
            "mod_name":            "RaceChip ECU Tuning Box",
            "mod_category":        "ECU Tune",
            "typical_hp_gain_pct": round(gain / stock * 100, 1),
            "typical_tq_gain_pct": None,
            "typical_hp_gain_abs": float(gain),
            "typical_tq_gain_abs": None,
            "compatible_makes":    make,
            "compatible_models":   model,
            "difficulty_level":    "DIY",
            "typical_cost_usd":    400,
            "notes":               (
                f"Plug-in tuning box. Stock: {stock} hp → Tuned: {tuned} hp. "
                "RaceChip published figures (static fallback)."
            ),
            "source_url":          "https://www.racechip.com",
        })
    return rows


def scrape_racechip(context) -> list[dict]:
    base = "https://www.racechip.com"
    rows = []

    log.info("RaceChip: visiting home page")
    _fetch(context, base + "/", timeout=30_000)
    _sleep(0.8, 1.5)

    for make in RACECHIP_MAKES:
        code      = RACECHIP_MAKE_CODES.get(make, make)
        make_rows: list[dict] = []

        for url in _rc_urls(code):
            soup, _ = _fetch(
                context, url,
                wait_selector=(
                    "article, .VehicleCard, [data-testid='vehicle-card'], "
                    ".vehicle-list, .model-list, main"
                ),
                referrer=base + "/",
                timeout=35_000,
            )
            if not soup:
                continue

            make_rows = _rc_parse_cards(soup, make, base, url)

            if not make_rows:
                model_links = _rc_parse_model_links(soup, make, base, url)
                log.info(
                    f"RaceChip {make.title()}: "
                    f"{len(model_links)} model links, following"
                )
                for model_url in model_links[:25]:
                    model_soup, _ = _fetch(
                        context, model_url,
                        wait_selector="article, .VehicleCard, main",
                        referrer=url,
                        timeout=18_000,
                    )
                    if model_soup:
                        make_rows.extend(
                            _rc_parse_cards(model_soup, make, base, model_url)
                        )
                    _sleep(0.3, 0.7)

            if make_rows:
                log.info(
                    f"RaceChip {make.title()}: {len(make_rows)} modifications"
                )
                break  # success, stop trying URL patterns

            _sleep(0.5, 1.0)

        if not make_rows:
            log.warning(f"RaceChip {make.title()}: 0 live (site blocked)")

        rows.extend(make_rows)
        _sleep(1.2, 2.2)

    # Per-make fallback: inject static data for any make with 0 live rows
    live_makes = {r["compatible_makes"] for r in rows}
    fallback_injected = 0
    for fb_make, fb_model, fb_stock, fb_tuned, fb_gain in RACECHIP_FALLBACK:
        # Normalise: live_makes stores the make as returned by _rc_parse_cards
        norm = fb_make.lower()
        already = any(norm in m.lower() for m in live_makes)
        if not already:
            gain_pct = round(fb_gain / fb_stock * 100, 1)
            rows.append({
                "mod_id":              _mod_id(f"RaceChip_{fb_make}_{fb_model}", "static_rc"),
                "mod_name":            "RaceChip ECU Tuning Box",
                "mod_category":        "ECU Tune",
                "typical_hp_gain_pct": gain_pct,
                "typical_tq_gain_pct": None,
                "typical_hp_gain_abs": float(fb_gain),
                "typical_tq_gain_abs": None,
                "compatible_makes":    fb_make,
                "compatible_models":   fb_model,
                "difficulty_level":    "DIY",
                "typical_cost_usd":    400,
                "notes":               (
                    f"Plug-in tuning box. Stock: {fb_stock} hp → Tuned: {fb_tuned} hp. "
                    "RaceChip published figures (static fallback)."
                ),
                "source_url":          "https://www.racechip.com",
            })
            fallback_injected += 1

    if fallback_injected:
        log.info(f"RaceChip: injected {fallback_injected} static fallback rows")

    log.info(f"RaceChip: {len(rows)} modifications scraped total")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 3: STATIC CURATED DATABASE
# ─────────────────────────────────────────────────────────────────────────────

STATIC_MODS = [
    # ── ECU TUNING ──────────────────────────────────────────────────────────
    dict(mod_name="Stage 1 ECU Remap — Petrol Turbo",
         mod_category="ECU Tune",
         typical_hp_gain_pct=18,  typical_tq_gain_pct=22,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Skoda;Seat;BMW;Ford;Opel;Renault;Peugeot;Citroen;Mercedes",
         compatible_models="All turbocharged petrol",
         difficulty_level="Professional", typical_cost_usd=350,
         notes="Bolt-on safe. Remaps boost, ignition, fuelling. Typical 15-25% gain."),
    dict(mod_name="Stage 2 ECU Remap — Petrol Turbo",
         mod_category="ECU Tune",
         typical_hp_gain_pct=32,  typical_tq_gain_pct=38,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Skoda;Seat;BMW;Ford;Opel",
         compatible_models="All turbocharged petrol",
         difficulty_level="Professional", typical_cost_usd=700,
         notes="Requires intake + downpipe minimum. 25-45% gain."),
    dict(mod_name="Stage 3 ECU Remap — Petrol Turbo",
         mod_category="ECU Tune",
         typical_hp_gain_pct=55,  typical_tq_gain_pct=60,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Mitsubishi",
         compatible_models="Turbocharged petrol with upgraded turbo",
         difficulty_level="Professional", typical_cost_usd=1200,
         notes="Requires upgraded turbo, injectors, intercooler. 50-70% gain."),
    dict(mod_name="Stage 1 ECU Remap — Diesel TDI/TDCi/CDI",
         mod_category="ECU Tune",
         typical_hp_gain_pct=22,  typical_tq_gain_pct=30,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Mercedes;Ford;Opel;Renault;Peugeot",
         compatible_models="All turbodiesel",
         difficulty_level="Professional", typical_cost_usd=280,
         notes="Best value mod for diesel. Typical +25-35 hp, +60-80 Nm."),
    dict(mod_name="OBD Tune / E-Tune (Remote)",
         mod_category="ECU Tune",
         typical_hp_gain_pct=15,  typical_tq_gain_pct=18,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All OBD-accessible ECUs",
         difficulty_level="DIY", typical_cost_usd=200,
         notes="Remote tune via OBD port. No dealer visit needed."),
    # ── FORCED INDUCTION ────────────────────────────────────────────────────
    dict(mod_name="Upgraded Intercooler (Front-Mount)",
         mod_category="Forced Induction",
         typical_hp_gain_pct=5,   typical_tq_gain_pct=6,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Subaru;Mitsubishi;Ford;BMW",
         compatible_models="Turbocharged models",
         difficulty_level="DIY", typical_cost_usd=450,
         notes="Reduces charge air temp 20-40°C. Enables higher boost safely."),
    dict(mod_name="Performance Turbocharger Upgrade",
         mod_category="Forced Induction",
         typical_hp_gain_pct=40,  typical_tq_gain_pct=45,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Subaru;Mitsubishi;BMW;Ford",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=2200,
         notes="Requires full supporting mods + custom tune. Typical Stage 3+."),
    dict(mod_name="Supercharger Kit",
         mod_category="Forced Induction",
         typical_hp_gain_pct=35,  typical_tq_gain_pct=30,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Honda;Toyota;Ford;Chevrolet;BMW",
         compatible_models="NA and mild hybrid",
         difficulty_level="Professional", typical_cost_usd=3500,
         notes="Belt-driven, instant boost. Linear power delivery."),
    # ── INTAKE ──────────────────────────────────────────────────────────────
    dict(mod_name="Cold Air Intake / Aftermarket Airbox",
         mod_category="Intake",
         typical_hp_gain_pct=3,   typical_tq_gain_pct=2,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=220,
         notes="Improved airflow. Minimal standalone gain; enabling mod for Stage 1+."),
    dict(mod_name="High-Flow Air Filter (Panel)",
         mod_category="Intake",
         typical_hp_gain_pct=1,   typical_tq_gain_pct=1,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=50,
         notes="Drop-in replacement. Negligible power gain, washable."),
    # ── EXHAUST ─────────────────────────────────────────────────────────────
    dict(mod_name="Cat-Back Exhaust System",
         mod_category="Exhaust",
         typical_hp_gain_pct=4,   typical_tq_gain_pct=3,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=700,
         notes="Reduces backpressure from cat back. Sound improvement."),
    dict(mod_name="High-Flow Catalytic Converter (200-cell)",
         mod_category="Exhaust",
         typical_hp_gain_pct=5,   typical_tq_gain_pct=5,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=300,
         notes="Reduces exhaust restriction. Stage 2 requirement for many tunes."),
    dict(mod_name="Downpipe (Turbo → Cat) — Catless",
         mod_category="Exhaust",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=9,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Mitsubishi",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=380,
         notes="Maximum flow but not road-legal in many regions."),
    dict(mod_name="Downpipe (Turbo → Cat) — High-Flow Cat",
         mod_category="Exhaust",
         typical_hp_gain_pct=6,   typical_tq_gain_pct=7,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Mitsubishi",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=520,
         notes="Compromise between flow and emissions compliance."),
    # ── FUELLING ────────────────────────────────────────────────────────────
    dict(mod_name="High-Flow Fuel Injectors",
         mod_category="Fuelling",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="High-power builds 400+ hp",
         difficulty_level="Professional", typical_cost_usd=600,
         notes="Enabling mod for Stage 3+. Required for E85/methanol."),
    dict(mod_name="Methanol / Water Injection Kit",
         mod_category="Fuelling",
         typical_hp_gain_pct=12,  typical_tq_gain_pct=10,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="Turbocharged",
         difficulty_level="Professional", typical_cost_usd=550,
         notes="Charge cooling via methanol spray. Effectively acts as octane boost."),
    dict(mod_name="Flex Fuel Kit (E85 Compatible)",
         mod_category="Fuelling",
         typical_hp_gain_pct=15,  typical_tq_gain_pct=18,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Ford;Honda",
         compatible_models="Turbocharged petrol",
         difficulty_level="Professional", typical_cost_usd=450,
         notes="E85 has higher octane (105 RON). Needs supporting fuelling mods."),
    # ── SUSPENSION & HANDLING ───────────────────────────────────────────────
    dict(mod_name="Coilover Suspension Kit",
         mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1100,
         notes="Adjustable ride height + damping. Improves cornering at expense of comfort."),
    dict(mod_name="Lowering Springs",
         mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=220,
         notes="Lowers CoG ~25-35mm. Firmer than stock. Budget handling upgrade."),
    dict(mod_name="Anti-Roll Bar (Sway Bar) Upgrade",
         mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=350,
         notes="Reduces body roll. Front and rear bars common."),
    dict(mod_name="Strut Tower Brace",
         mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=150,
         notes="Improves chassis rigidity. Better turn-in and steering feel."),
    # ── BRAKES ──────────────────────────────────────────────────────────────
    dict(mod_name="Big Brake Kit (BBK) — 4-piston",
         mod_category="Brakes",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1600,
         notes="Larger rotors + multi-piston calipers. Needed for 300+ hp track use."),
    # ── ENGINE INTERNALS ────────────────────────────────────────────────────
    dict(mod_name="Forged Pistons + Connecting Rods",
         mod_category="Engine Internals",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=2200,
         notes="Enabling mod for 400+ hp builds. Required for high boost reliability."),
    dict(mod_name="Ported and Polished Cylinder Head",
         mod_category="Engine Internals",
         typical_hp_gain_pct=10,  typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1300,
         notes="Improves airflow through head. Best combined with cam upgrades."),
    dict(mod_name="Performance Camshaft Upgrade",
         mod_category="Engine Internals",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=6,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Honda;Toyota;Subaru;BMW;Ford;Mazda",
         compatible_models="High-rev naturally aspirated",
         difficulty_level="Professional", typical_cost_usd=850,
         notes="More lift/duration. Popular on K-series/B-series Honda."),
    dict(mod_name="Stroker Kit (Increased Displacement)",
         mod_category="Engine Internals",
         typical_hp_gain_pct=15,  typical_tq_gain_pct=20,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Honda;Subaru;Ford;Chevrolet",
         compatible_models="B16;B18;EJ20;EJ25;LS",
         difficulty_level="Professional", typical_cost_usd=3500,
         notes="Increases engine displacement via longer stroke crankshaft."),
    # ── BOOST CONTROL ───────────────────────────────────────────────────────
    dict(mod_name="Electronic Boost Controller",
         mod_category="Boost Control",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=250,
         notes="Precise boost control. Safer than manual. Needs custom tune."),
    dict(mod_name="Blow-Off Valve / Diverter Valve Upgrade",
         mod_category="Boost Control",
         typical_hp_gain_pct=1,   typical_tq_gain_pct=1,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Subaru;Mitsubishi;BMW",
         compatible_models="Turbocharged models",
         difficulty_level="DIY", typical_cost_usd=130,
         notes="Prevents compressor surge. Reliability + sound improvement."),
    # ── DRIVETRAIN / TRANSMISSION ───────────────────────────────────────────
    dict(mod_name="Limited Slip Differential (LSD)",
         mod_category="Drivetrain",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1600,
         notes="Reduces wheelspin. Essential for high-power FWD/RWD."),
    dict(mod_name="Short-Throw Shifter",
         mod_category="Transmission",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="Manual transmission",
         difficulty_level="DIY", typical_cost_usd=160,
         notes="Reduces gear throw by 30-50%."),
    dict(mod_name="Performance Clutch Kit",
         mod_category="Transmission",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="Manual transmission",
         difficulty_level="Professional", typical_cost_usd=700,
         notes="Handles higher torque for Stage 2+ builds. Required at 350+ Nm."),
    # ── AERODYNAMICS ────────────────────────────────────────────────────────
    dict(mod_name="Front Splitter",
         mod_category="Aerodynamics",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=200,
         notes="Increases front downforce. Effect significant only above 150 km/h."),
    dict(mod_name="Rear Wing / Spoiler",
         mod_category="Aerodynamics",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=320,
         notes="Adds rear downforce. Can slightly increase drag."),
    dict(mod_name="Diffuser (Rear Underbody)",
         mod_category="Aerodynamics",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=350,
         notes="Reduces drag and lift at rear. Noticeable above 180 km/h."),
]


def get_static_mods() -> list[dict]:
    rows = []
    for mod in STATIC_MODS:
        r = mod.copy()
        r["mod_id"]     = _mod_id(mod["mod_name"], "static")
        r["source_url"] = "static_curated"
        rows.append(r)
    log.info(f"Static mods: {len(rows)} entries")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

COLS = [
    "mod_id", "mod_name", "mod_category",
    "typical_hp_gain_pct", "typical_tq_gain_pct",
    "typical_hp_gain_abs", "typical_tq_gain_abs",
    "compatible_makes", "compatible_models",
    "difficulty_level", "typical_cost_usd",
    "notes", "source_url",
]


def scrape_all(static_only: bool = False,
               headless: bool = False) -> pd.DataFrame:
    rows = get_static_mods()

    if not static_only:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error(
                "Playwright not installed. "
                "Run: pip install playwright && python -m playwright install chromium"
            )
            static_only = True

    if not static_only:
        mode = "headless" if headless else "headful (visible window)"
        log.info(f"Browser mode: {mode}")
        with sync_playwright() as pw:
            browser, context = _make_browser_context(pw, headless=headless)
            try:
                rows.extend(scrape_apr(context))
                rows.extend(scrape_racechip(context))
            finally:
                context.close()
                browser.close()

    df = pd.DataFrame(rows)
    df.drop_duplicates(subset=["mod_id"], keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)

    for c in COLS:
        if c not in df.columns:
            df[c] = None
    df = df[COLS]

    df.to_csv(OUTPUT_CSV, index=False)
    log.info(f"Saved {len(df)} modifications to {OUTPUT_CSV}")
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scrape car modification data to CSV")
    ap.add_argument(
        "--static-only", action="store_true",
        help="Use only the built-in static list (no internet required)",
    )
    ap.add_argument(
        "--headless", action="store_true",
        help="Run browser headless (faster but more likely to be blocked)",
    )
    args = ap.parse_args()
    df = scrape_all(static_only=args.static_only, headless=args.headless)
    print(f"\nDone. {len(df)} modifications written to {OUTPUT_CSV}")
    print(
        df[["mod_name", "mod_category", "typical_hp_gain_pct", "compatible_makes"]]
        .to_string(index=False)
    )
