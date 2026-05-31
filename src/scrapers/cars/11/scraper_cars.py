"""
scraper_cars.py — parkers.co.uk → cars_raw.csv

Real URL hierarchy on parkers.co.uk
────────────────────────────────────
/make/                                         e.g. /bmw/
/make/model/                                   e.g. /bmw/x5/
/make/model/gen-YEAR/specs/                    ← LISTING page  (links to variants)
/make/model/gen-YEAR/<variant>/specs/          ← DATA page     (actual spec table)

Navigation strategy
───────────────────
1. /make/           → collect model URLs  (/make/model/)
2. /make/model/     → collect listing URLs (/make/model/gen-YEAR/specs/)
3. /make/model/gen-YEAR/specs/  → collect variant spec URLs (/make/model/gen-YEAR/variant/specs/)
4. /make/model/gen-YEAR/variant/specs/  → parse spec table  ← only these are scraped

The listing page (/gen-YEAR/specs/) contains NO spec table rows — it only lists
variants. The scraper must visit it and follow links to variant pages.

Install:   pip install cloudscraper lxml beautifulsoup4 pandas
Run:
    python scraper_cars.py --makes bmw volkswagen ford
    python scraper_cars.py --makes bmw --variants-per-gen 3   # default: 3 variants per generation
    python scraper_cars.py --makes bmw --all-variants         # scrape every variant
    python scraper_cars.py --makes bmw --gens-per-model 5     # default: 5 generations per model
    python scraper_cars.py --makes bmw --all-gens             # scrape every generation
    python scraper_cars.py --limit 50
    python scraper_cars.py --urls https://www.parkers.co.uk/bmw/x5/4x4-2018/xdrive40i-m-sport-5dr-step-auto/specs/
    python scraper_cars.py --list-makes
    python scraper_cars.py --makes bmw --debug   # saves HTML on parse failures
"""

import csv
import re
import time
import random
import logging
import argparse
import hashlib
import signal
import sys
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

# ── HTTP client ────────────────────────────────────────────────────────────────
try:
    import cloudscraper
    SCRAPER = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    _client_msg = "Using cloudscraper (Cloudflare bypass)"
except ImportError:
    import requests
    SCRAPER = requests.Session()
    SCRAPER.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    _client_msg = "cloudscraper not found — falling back to requests (may hit 403)"

OUTPUT_CSV       = "cars_raw.csv"
DEBUG_DIR        = Path("debug_html")   # HTML dumps land here when --debug is used
BASE             = "https://www.parkers.co.uk"
DEBUG_MODE       = False  # set to True via --debug flag
VARIANTS_PER_GEN = 3      # max variants per generation listing; 0 = unlimited (--all-variants)
GENS_PER_MODEL   = 5      # max generations per model; 0 = unlimited (--all-gens)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
log.info(_client_msg)

# ── Known real car makes ───────────────────────────────────────────────────────
KNOWN_MAKES: dict[str, str] = {
    "abarth": "Abarth", "ac": "AC", "aito": "Aito", "alfa-romeo": "Alfa Romeo",
    "alpina": "Alpina", "alpine": "Alpine", "ariel": "Ariel", "aston-martin": "Aston Martin",
    "audi": "Audi", "austin": "Austin", "bentley": "Bentley", "bmw": "BMW",
    "byd": "BYD", "bugatti": "Bugatti", "cadillac": "Cadillac", "caterham": "Caterham",
    "chevrolet": "Chevrolet", "chrysler": "Chrysler", "citroen": "Citroen",
    "cupra": "Cupra", "dacia": "Dacia", "daewoo": "Daewoo", "daihatsu": "Daihatsu",
    "ds": "DS", "ferrari": "Ferrari", "fiat": "Fiat", "fisker": "Fisker",
    "ford": "Ford", "genesis": "Genesis", "great-wall": "Great Wall",
    "honda": "Honda", "hummer": "Hummer", "hyundai": "Hyundai",
    "infiniti": "Infiniti", "isuzu": "Isuzu", "jaguar": "Jaguar", "jeep": "Jeep",
    "kia": "Kia", "lamborghini": "Lamborghini", "lancia": "Lancia",
    "land-rover": "Land Rover", "leapmotor": "Leapmotor", "lexus": "Lexus",
    "lincoln": "Lincoln", "lotus": "Lotus", "lucid": "Lucid", "lynk-co": "Lynk & Co",
    "maserati": "Maserati", "maybach": "Maybach", "mazda": "Mazda",
    "mclaren": "McLaren", "mercedes-benz": "Mercedes-Benz", "mg": "MG",
    "mini": "MINI", "mitsubishi": "Mitsubishi", "morgan": "Morgan",
    "nissan": "Nissan", "omoda": "Omoda", "opel": "Opel", "peugeot": "Peugeot",
    "polestar": "Polestar", "pontiac": "Pontiac", "porsche": "Porsche",
    "proton": "Proton", "renault": "Renault", "rolls-royce": "Rolls-Royce",
    "rover": "Rover", "saab": "Saab", "seat": "SEAT", "skoda": "Skoda",
    "smart": "Smart", "ssangyong": "SsangYong", "subaru": "Subaru",
    "suzuki": "Suzuki", "tesla": "Tesla", "toyota": "Toyota", "triumph": "Triumph",
    "vauxhall": "Vauxhall", "volkswagen": "Volkswagen", "volvo": "Volvo",
    "xpeng": "Xpeng", "zastava": "Zastava", "zeekr": "Zeekr",
}

_NOT_A_MODEL: frozenset[str] = frozenset({
    "specs", "used-prices", "car-leasing", "for-sale", "nearly-new",
    "owners-reviews", "owner-reviews", "reviews", "road-tests", "news",
    "advice", "compare", "leasing", "electric", "hybrid", "used", "new",
    "finance", "insurance", "shortlist", "valuations", "buying-guide",
    "cars-for-sale", "new-cars", "on-sale-soon",
})

_MAKE_BLOCK_WORDS: frozenset[str] = frozenset({
    "car", "sale", "award", "review", "test", "valuation", "finance",
    "insurance", "advice", "long", "owner", "electric", "used",
    "van", "bike", "truck", "motorhome", "sell", "short", "lease",
    "price", "buy", "news", "guide", "search", "compare",
})


# ── Helpers ───────────────────────────────────────────────────────────────────
def _sleep(lo: float = 1.5, hi: float = 3.5) -> None:
    time.sleep(random.uniform(lo, hi))


def fetch(url: str, referer: str = BASE + "/") -> BeautifulSoup | None:
    """GET url, return BeautifulSoup on success, None on permanent failure."""
    SCRAPER.headers.update({"Referer": referer})
    for attempt in range(1, 4):
        try:
            log.info(f"GET {url}")
            r = SCRAPER.get(url, timeout=20)
            if r.status_code == 403:
                wait = 15 * attempt
                log.warning(f"403 Forbidden — waiting {wait}s (retry {attempt}/3)")
                time.sleep(wait)
                continue
            if r.status_code in (404, 410):
                log.debug(f"Skip {r.status_code}: {url}")
                return None
            r.raise_for_status()
            _sleep()
            return BeautifulSoup(r.text, "lxml")
        except Exception as exc:
            log.warning(f"Attempt {attempt}/3 failed: {exc}")
            _sleep(4, 8)
    log.error(f"Giving up: {url}")
    return None


def _num(text: str | None, pat: str) -> float | None:
    if not text:
        return None
    m = re.search(pat, str(text).replace(",", ""))
    return float(m.group(1)) if m else None


def _save_debug_html(url: str, html: str) -> None:
    """Save raw HTML to debug_html/ for inspection when --debug is active."""
    if not DEBUG_MODE:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "_", url.replace(BASE, "").strip("/"))[:120]
    path = DEBUG_DIR / f"{safe}.html"
    path.write_text(html, encoding="utf-8")
    log.debug(f"Debug HTML saved: {path}")


# ── STEP 1: Collect makes ──────────────────────────────────────────────────────
def get_makes(makes_filter: list[str] | None = None) -> list[tuple[str, str]]:
    log.info("Warm-up: visiting homepage to obtain cookies...")
    try:
        SCRAPER.get(BASE + "/", timeout=20)
        _sleep(2, 4)
    except Exception as exc:
        log.warning(f"Warm-up failed: {exc}")

    makes: list[tuple[str, str]] = [
        (name, f"{BASE}/{slug}/") for slug, name in KNOWN_MAKES.items()
    ]
    known_slugs = set(KNOWN_MAKES.keys())

    soup = fetch(BASE + "/cars/")
    if soup:
        discovered = 0
        one_seg = re.compile(r"^/([a-z][a-z0-9-]{1,25})/$")
        for a in soup.select("a[href]"):
            m = one_seg.match(a.get("href", ""))
            if not m:
                continue
            slug = m.group(1)
            if slug in known_slugs:
                continue
            if any(w in slug.split("-") for w in _MAKE_BLOCK_WORDS):
                continue
            name = a.get_text(strip=True) or slug.replace("-", " ").title()
            if name and len(name) <= 40:
                makes.append((name, f"{BASE}/{slug}/"))
                known_slugs.add(slug)
                discovered += 1
        if discovered:
            log.info(f"Discovered {discovered} additional make(s) from /cars/")

    if makes_filter:
        fs = {x.lower().replace(" ", "-") for x in makes_filter}
        makes = [
            (n, u) for n, u in makes
            if u.rstrip("/").split("/")[-1] in fs
            or n.lower() in {x.lower() for x in makes_filter}
        ]

    log.info(f"Processing {len(makes)} make(s)")
    return makes


# ── STEP 2: Collect models ─────────────────────────────────────────────────────
def get_models(make_slug: str, make_url: str) -> list[tuple[str, str]]:
    """
    Return [(model_name, model_url)] for a make page.
    Also discovers sub-models that are only linked from model pages
    (e.g. /bmw/m3/ may only appear as a link on /bmw/3-series/, not on /bmw/).
    """
    soup = fetch(make_url, referer=BASE + "/cars/")
    if not soup:
        return []

    ms  = re.escape(make_slug)
    pat = re.compile(rf"^/{ms}/([a-z0-9][a-z0-9-]{{1,60}})/$")

    seen: set[str] = set()
    models: list[tuple[str, str]] = []

    def _add(href: str, text: str) -> None:
        m = pat.match(href)
        if not m or m.group(1) in _NOT_A_MODEL:
            return
        url = BASE + href
        if url not in seen:
            seen.add(url)
            name = text.strip() or m.group(1).replace("-", " ").title()
            models.append((name, url))

    for a in soup.select("a[href]"):
        _add(a.get("href", ""), a.get_text(strip=True))

    # Also check each model page for sibling-model links
    # e.g. /bmw/3-series/ may link to /bmw/m3/, /bmw/m3-touring/ etc.
    primary = list(models)           # snapshot — only scan first-level models
    for _name, model_url in primary:
        sub = fetch(model_url, referer=make_url)
        if not sub:
            continue
        for a in sub.select("a[href]"):
            _add(a.get("href", ""), a.get_text(strip=True))

    log.info(f"  {make_slug}: {len(models)} model(s) (incl. sub-models)")
    return models


# ── STEP 3: Collect all generation listing URLs for a model ───────────────────
#
#  Parkers URL hierarchy:
#
#  /make/model/                       model page
#    → /make/model/gen-YEAR/specs/    LISTING page (one per generation)
#        → /make/model/gen-YEAR/variant/specs/   DATA page (actual specs)
#
#  On the LISTING page there are also links to OTHER generations:
#    <a href="/make/model/older-gen-YEAR/specs/">  (cross-gen navigation)
#    <a href="/make/model/older-gen-YEAR/">        (without /specs/ suffix!)
#
#  Both forms must be followed to discover all generations.
#
#  GENS_PER_MODEL: keep only the N most-recently-linked generations (0 = all).
#  Generations are collected in discovery order (newest first, as parkers links them).
# ─────────────────────────────────────────────────────────────────────────────

def get_variant_spec_urls(make_slug: str, model_slug: str, model_url: str) -> list[str]:
    """
    Return DATA-level variant spec URLs for a model, across all discovered generations.

    URL hierarchy on parkers.co.uk:
      /make/model/                          model index page
      /make/model/<gen>/specs/              LISTING page  — links to variants + other gens
      /make/model/<gen>/<variant>/specs/    DATA page     — actual spec table

    Parkers only shows the newest generation on the model index page.
    Links to older generations appear in a dropdown/nav inside LISTING pages.
    Strategy:
      1. Fetch model index → seed listing_queue with any /gen/specs/ or /gen/ hrefs.
      2. Process listing_queue with an index-based while loop (so appends are seen).
         For each listing page:
           a. Collect variant DATA hrefs → apply VARIANTS_PER_GEN cap → save.
           b. Collect cross-gen hrefs (both /gen/specs/ and /gen/ formats) → enqueue.
           c. Fallback: if no data hrefs found, try one sub-level deeper.
         Stop when GENS_PER_MODEL visited (0 = unlimited).

    VARIANTS_PER_GEN cap is applied per listing page, not globally.
    """
    ms  = re.escape(make_slug)
    mds = re.escape(model_slug)

    # /make/model/<gen>/specs/               — LISTING page
    listing_re = re.compile(rf"^/{ms}/{mds}/([^/]+)/specs/$")
    # /make/model/<gen>/                     — gen index page (without /specs/)
    gen_re     = re.compile(rf"^/{ms}/{mds}/([^/]+)/$")
    # /make/model/<gen>/<variant>/specs/     — DATA page
    data_re    = re.compile(rf"^/{ms}/{mds}/([^/]+)/([^/]+)/specs/$")

    listing_queue: list[str] = []
    seen_listing:  set[str]  = set()   # /gen/specs/ hrefs already queued
    seen_gen:      set[str]  = set()   # /gen/ hrefs already converted+queued
    all_data:      list[str] = []
    seen_data:     set[str]  = set()

    def _enqueue_listing(href: str) -> None:
        if href not in seen_listing:
            seen_listing.add(href)
            listing_queue.append(href)

    def _enqueue_gen(href: str) -> None:
        """Convert a /gen/ href to /gen/specs/ and enqueue if new."""
        if href in seen_gen:
            return
        seen_gen.add(href)
        listing = href.rstrip("/") + "/specs/"
        if listing_re.match(listing):
            _enqueue_listing(listing)

    def _add_data(href: str) -> None:
        full = BASE + href
        if full not in seen_data:
            seen_data.add(full)
            all_data.append(full)

    def _pick(hrefs: list[str]) -> list[str]:
        """Apply VARIANTS_PER_GEN cap (0 = all)."""
        return hrefs if VARIANTS_PER_GEN == 0 else hrefs[:VARIANTS_PER_GEN]

    def _collect_gen_links(soup: BeautifulSoup) -> None:
        """Scan a page for cross-gen navigation links and enqueue them."""
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if listing_re.match(href):
                _enqueue_listing(href)
            elif gen_re.match(href):
                _enqueue_gen(href)

    # ── Step 1: seed from model index page ───────────────────────────────
    model_soup = fetch(model_url, referer=f"{BASE}/{make_slug}/")
    if not model_soup:
        log.info(f"    {make_slug}/{model_slug}: 0 spec page(s) (model page failed)")
        return []

    _collect_gen_links(model_soup)

    # Also follow any /gen/ links from the model page to discover older
    # generations that Parkers only exposes inside generation pages.
    # Take a snapshot so we don't infinite-loop on newly discovered gens.
    for gen_href in list(seen_gen):
        gen_soup = fetch(BASE + gen_href, referer=model_url)
        if gen_soup:
            _collect_gen_links(gen_soup)

    # Fallback A: if model page gave no seeds at all, try /make/model/specs/
    # (Parkers sometimes uses this as the entry point for single-gen models).
    if not listing_queue:
        alt_url  = model_url.rstrip("/") + "/specs/"
        alt_href = f"/{make_slug}/{model_slug}/specs/"
        alt_soup = fetch(alt_url, referer=model_url)
        if alt_soup:
            _enqueue_listing(alt_href)
            _collect_gen_links(alt_soup)

    log.debug(
        f"    {make_slug}/{model_slug}: {len(listing_queue)} generation(s) seeded"
    )

    # ── Step 2: BFS over listing_queue ────────────────────────────────────
    gens_visited = 0
    i = 0  # index-based so appends during iteration are visible

    while i < len(listing_queue):
        if GENS_PER_MODEL and gens_visited >= GENS_PER_MODEL:
            log.debug(f"    Hit GENS_PER_MODEL={GENS_PER_MODEL} cap, stopping.")
            break

        listing_href = listing_queue[i]
        i += 1

        listing_url  = BASE + listing_href
        listing_soup = fetch(listing_url, referer=model_url)
        if not listing_soup:
            continue
        gens_visited += 1

        # ── 2a: collect variant DATA hrefs from this listing page ─────
        data_hrefs: list[str] = []
        for a in listing_soup.select("a[href]"):
            href = a.get("href", "")
            if data_re.match(href) and BASE + href not in seen_data:
                data_hrefs.append(href)

        for h in _pick(data_hrefs):
            _add_data(h)

        log.debug(
            f"      {listing_href}: {len(data_hrefs)} variant(s) found, "
            f"{len(_pick(data_hrefs))} kept"
        )

        # ── 2b: discover more generations from cross-gen nav on this page ─
        _collect_gen_links(listing_soup)

        # ── 2c: fallback — no data links → try one sub-level deeper ──────
        if not data_hrefs:
            sub_re = re.compile(rf"^/{ms}/{mds}/[^/]+/([^/]+)/$")
            for a in listing_soup.select("a[href]"):
                href = a.get("href", "")
                if sub_re.match(href):
                    sub_soup = fetch(BASE + href, referer=listing_url)
                    if sub_soup:
                        sub_batch: list[str] = []
                        for sa in sub_soup.select("a[href]"):
                            sh = sa.get("href", "")
                            if data_re.match(sh) and BASE + sh not in seen_data:
                                sub_batch.append(sh)
                        for h in _pick(sub_batch):
                            _add_data(h)

    log.info(
        f"    {make_slug}/{model_slug}: {len(all_data)} variant spec page(s) "
        f"across {gens_visited} generation(s)"
    )
    return all_data


# ── STEP 4: Parse a variant spec DATA page ────────────────────────────────────
def scrape_spec_page(url: str) -> dict | None:
    """
    Fetch a DATA spec URL and return a row dict, or None if no spec table found.
    URL expected: BASE/make/model/gen-YEAR/variant/specs/
    """
    parts        = url.replace(BASE, "").strip("/").split("/")
    make_slug    = parts[0] if len(parts) > 0 else ""
    model_slug   = parts[1] if len(parts) > 1 else ""
    gen_slug     = parts[2] if len(parts) > 2 else ""
    variant_slug = parts[3] if len(parts) > 3 else ""

    soup = fetch(url, referer=f"{BASE}/{make_slug}/{model_slug}/{gen_slug}/specs/")
    if not soup:
        return None

    # Try multiple selector strategies to be robust against site redesigns
    raw: dict[str, str] = {}

    # Strategy 1 — original class names
    for item in soup.select("li.specs-detail-table__item"):
        lbl = item.select_one(".specs-detail-table__item__label")
        val = item.select_one(".specs-detail-table__item__value")
        if lbl and val:
            raw[lbl.get_text(strip=True).lower()] = val.get_text(strip=True)

    # Strategy 2 — any <dt>/<dd> definition list
    if not raw:
        for dt in soup.select("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                raw[dt.get_text(strip=True).lower()] = dd.get_text(strip=True)

    # Strategy 3 — any table with two columns
    if not raw:
        for row in soup.select("tr"):
            cells = row.select("th, td")
            if len(cells) == 2:
                raw[cells[0].get_text(strip=True).lower()] = cells[1].get_text(strip=True)

    # Strategy 4 — look for any element whose class contains "spec" and "label/value"
    if not raw:
        for item in soup.select("[class*='spec']"):
            lbl = item.select_one("[class*='label'], [class*='name'], [class*='title']")
            val = item.select_one("[class*='value'], [class*='data'], [class*='detail']")
            if lbl and val and lbl != val:
                raw[lbl.get_text(strip=True).lower()] = val.get_text(strip=True)

    if not raw:
        log.warning(f"No spec data found at {url}")
        if DEBUG_MODE:
            _save_debug_html(url, soup.prettify())
            log.info(f"  HTML saved to debug_html/ for inspection")
        return None

    h1    = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else url

    top_mph = _num(raw.get("top speed", ""),            r"(\d+(?:\.\d+)?)")
    zero60  = _num(raw.get("acceleration 0-60mph", ""), r"(\d+(?:\.\d+)?)")

    yrs = sorted(
        int(y)
        for y in re.findall(r"\b(20\d{2}|19\d{2})\b", gen_slug + " " + title)
    )
    year_from = yrs[0]  if yrs          else None
    year_to   = yrs[-1] if len(yrs) > 1 else None

    fuel = (raw.get("fuel type") or "").lower()
    tl   = title.lower()
    if "electric" in fuel:
        etype = "EV"
    elif "hybrid" in fuel or "hybrid" in tl:
        etype = "Hybrid"
    elif any(w in tl for w in ["tdi", "tdci", "cdi", "dci", "diesel"]):
        etype = "Diesel"
    elif any(w in tl for w in ["tsi", "tfsi", "turbo", "gti", "sti",
                                "wrx", "type r", "st ", "rs "]):
        etype = "Turbocharged"
    elif fuel in ("petrol", "gasoline"):
        etype = "Naturally Aspirated"
    else:
        etype = "Unknown"

    return {
        "car_id":             "CAR_" + hashlib.md5(url.encode()).hexdigest()[:8].upper(),
        "make":               make_slug.replace("-", " ").title(),
        "model":              model_slug.replace("-", " ").title(),
        "variant":            variant_slug.replace("-", " "),
        "raw_title":          title,
        "year_from":          year_from,
        "year_to":            year_to,
        "displacement_cc":    _num(raw.get("engine size", ""), r"(\d+)"),
        "cylinders":          _num(raw.get("cylinders", ""),   r"(\d+)"),
        "stock_hp":           _num(raw.get("horsepower", ""),  r"(\d+)"),
        "stock_torque_nm":    _num(raw.get("torque", ""),      r"(\d+)\s*[Nn][Mm]"),
        "weight_kg":          _num(raw.get("weight", ""),      r"(\d+)"),
        "drivetrain":         raw.get("drivetrain"),
        "transmission":       raw.get("transmission"),
        "engine_type":        etype,
        "fuel_type":          raw.get("fuel type"),
        "drag_coef":          _num(raw.get("drag coefficient", ""), r"(\d+\.\d+)"),
        "frontal_area_m2":    None,
        "top_speed_stock":    round(top_mph * 1.60934, 1) if top_mph else None,
        "top_speed_mph":      top_mph,
        "zero_hundred_stock": zero60,
        "co2_gkm":            _num(raw.get("co2", ""),         r"(\d+)"),
        "source_url":         url,
    }


# ── CSV — immediate, interrupt-safe writes ────────────────────────────────────
_CSV_FIELDS = [
    "car_id", "make", "model", "variant", "raw_title",
    "year_from", "year_to", "displacement_cc", "cylinders",
    "stock_hp", "stock_torque_nm", "weight_kg",
    "drivetrain", "transmission", "engine_type", "fuel_type",
    "drag_coef", "frontal_area_m2",
    "top_speed_stock", "top_speed_mph", "zero_hundred_stock",
    "co2_gkm", "source_url",
]


def _append_row(row: dict, path: Path) -> None:
    """Write one row to CSV immediately. Creates file + header on first call."""
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k) for k in _CSV_FIELDS})


def _load_done_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        df   = pd.read_csv(path, usecols=["source_url"])
        urls = set(df["source_url"].dropna())
        log.info(f"Resuming — {len(urls)} URL(s) already in {path}")
        return urls
    except Exception as exc:
        log.warning(f"Could not load existing CSV: {exc}")
        return set()


# ── Crawler ───────────────────────────────────────────────────────────────────
def crawl(makes_filter: list[str] | None = None, limit: int | None = None) -> pd.DataFrame:
    out   = Path(OUTPUT_CSV)
    done  = _load_done_urls(out)
    total = 0

    def _sigint(sig, frame):
        log.info(f"Interrupted — {total} car(s) saved to {out}. Exiting cleanly.")
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    makes = get_makes(makes_filter)
    if not makes:
        log.error("No makes to process.")
        return pd.DataFrame()

    for _make_name, make_url in makes:
        make_slug = make_url.rstrip("/").split("/")[-1]

        for _model_name, model_url in get_models(make_slug, make_url):
            model_slug = model_url.rstrip("/").split("/")[-1]

            for spec_url in get_variant_spec_urls(make_slug, model_slug, model_url):
                if spec_url in done:
                    log.debug(f"Skip (already scraped): {spec_url}")
                    continue

                row = scrape_spec_page(spec_url)
                if row:
                    _append_row(row, out)
                    done.add(spec_url)
                    total += 1
                    log.info(
                        f"[{total}] Saved: {row['make']} {row['model']} "
                        f"{row['variant']} ({row['year_from']}) — {row['stock_hp']} hp"
                    )
                else:
                    log.warning(f"No data: {spec_url}")

                if limit and total >= limit:
                    log.info(f"Reached limit of {limit} cars.")
                    return pd.read_csv(out) if out.exists() else pd.DataFrame()

    log.info(f"Crawl complete — {total} new car(s) saved to {out}")
    return pd.read_csv(out) if out.exists() else pd.DataFrame()


def scrape_urls(urls: list[str]) -> pd.DataFrame:
    """Scrape a list of explicit variant spec URLs."""
    out   = Path(OUTPUT_CSV)
    done  = _load_done_urls(out)
    total = 0
    for url in urls:
        if url in done:
            log.info(f"Skip (already scraped): {url}")
            continue
        row = scrape_spec_page(url)
        if row:
            _append_row(row, out)
            done.add(url)
            total += 1
            log.info(f"[{total}] Saved: {row['make']} {row['model']} {row['variant']}")
        else:
            log.warning(f"No data: {url}")
    log.info(f"Done — {total} new row(s) in {out}")
    return pd.read_csv(out) if out.exists() else pd.DataFrame()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Scrape car specs from parkers.co.uk → cars_raw.csv"
    )
    ap.add_argument("--makes", nargs="+", metavar="MAKE",
                    help="Make slug(s), e.g.: bmw volkswagen ford")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="Stop after N cars")
    ap.add_argument("--urls", nargs="+", metavar="URL",
                    help="Scrape specific variant /specs/ URLs directly")
    ap.add_argument("--list-makes", action="store_true",
                    help="Print all known makes and exit")
    ap.add_argument("--debug", action="store_true",
                    help="Save HTML to debug_html/ when no spec data is found")
    ap.add_argument("--variants-per-gen", type=int, default=3, metavar="N",
                    help="Max variants to scrape per generation listing (default: 3). "
                         "Variants are taken in page order (base→top trim). "
                         "Ignored when --all-variants is set.")
    ap.add_argument("--all-variants", action="store_true",
                    help="Scrape every variant for every generation (overrides --variants-per-gen).")
    ap.add_argument("--gens-per-model", type=int, default=5, metavar="N",
                    help="Max generations to scrape per model (default: 5, newest first). "
                         "Ignored when --all-gens is set.")
    ap.add_argument("--all-gens", action="store_true",
                    help="Scrape every generation for every model (overrides --gens-per-model).")
    args = ap.parse_args()

    if args.list_makes:
        for slug, name in sorted(KNOWN_MAKES.items()):
            print(f"  {slug:<25} {name}")
        sys.exit(0)

    if args.debug:
        DEBUG_MODE = True
        log.info("Debug mode ON — failed pages will be saved to debug_html/")

    if args.all_variants:
        VARIANTS_PER_GEN = 0
        log.info("All-variants mode — scraping every variant for every generation")
    else:
        VARIANTS_PER_GEN = max(1, args.variants_per_gen)
        log.info(f"Variant cap: {VARIANTS_PER_GEN} per generation listing")

    if args.all_gens:
        GENS_PER_MODEL = 0
        log.info("All-gens mode — scraping every generation for every model")
    else:
        GENS_PER_MODEL = max(1, args.gens_per_model)
        log.info(f"Generation cap: {GENS_PER_MODEL} per model")

    df = scrape_urls(args.urls) if args.urls else crawl(args.makes, args.limit)

    print(f"\nFinished. {len(df)} total row(s) in {OUTPUT_CSV}")
    if not df.empty:
        cols = [c for c in ["make", "model", "variant", "year_from", "stock_hp"]
                if c in df.columns]
        print(df[cols].head(10).to_string(index=False))
