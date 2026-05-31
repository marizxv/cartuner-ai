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

OUTPUT_CSV  = "cars_raw.csv"
DEBUG_DIR   = Path("debug_html")   # HTML dumps land here when --debug is used
BASE        = "https://www.parkers.co.uk"
DEBUG_MODE  = False  # set to True via --debug flag

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
    """Return [(model_name, model_url)] for a make."""
    soup = fetch(make_url, referer=BASE + "/cars/")
    if not soup:
        return []

    pat   = re.compile(rf"^/{re.escape(make_slug)}/([a-z0-9][a-z0-9-]{{1,60}})/$")
    seen: set[str] = set()
    models: list[tuple[str, str]] = []

    for a in soup.select("a[href]"):
        m = pat.match(a.get("href", ""))
        if not m or m.group(1) in _NOT_A_MODEL:
            continue
        url = BASE + a["href"]
        if url not in seen:
            seen.add(url)
            name = a.get_text(strip=True) or m.group(1).replace("-", " ").title()
            models.append((name, url))

    log.info(f"  {make_slug}: {len(models)} model(s)")
    return models


# ── STEP 3: Collect variant spec URLs for a model ─────────────────────────────
#
#  Page types observed on parkers.co.uk:
#
#  LISTING  /make/model/gen-YEAR/specs/
#           → contains links like <a href="/make/model/gen-YEAR/variant/specs/">
#           → has NO spec-table rows (wrong to try to parse these)
#
#  DATA     /make/model/gen-YEAR/variant/specs/
#           → contains the actual <li class="specs-detail-table__item"> rows
#
#  The model page (/make/model/) links directly to LISTING pages.
#  We must visit each LISTING page and collect DATA page URLs from it.
# ─────────────────────────────────────────────────────────────────────────────

def get_variant_spec_urls(make_slug: str, model_slug: str, model_url: str) -> list[str]:
    """
    Returns a list of DATA-level spec URLs:
        /make/model/gen-YEAR/variant/specs/

    Walk:
        model page  →  listing pages (/gen-YEAR/specs/)
                    →  variant data pages (/gen-YEAR/variant/specs/)
    """
    ms  = re.escape(make_slug)
    mds = re.escape(model_slug)

    # LISTING page pattern: /make/model/<anything>/specs/  (depth 4, last = specs)
    listing_re = re.compile(rf"^/{ms}/{mds}/([^/]+)/specs/$")

    # DATA page pattern: /make/model/<gen>/<variant>/specs/  (depth 5, last = specs)
    data_re = re.compile(rf"^/{ms}/{mds}/([^/]+)/([^/]+)/specs/$")

    found:        list[str] = []
    seen_data:    set[str]  = set()
    seen_listing: set[str]  = set()

    def _add_data(href: str) -> None:
        full = BASE + href
        if full not in seen_data:
            seen_data.add(full)
            found.append(full)

    # ── Visit model page ──────────────────────────────────────────────────
    model_soup = fetch(model_url, referer=f"{BASE}/{make_slug}/")
    if not model_soup:
        log.info(f"    {make_slug}/{model_slug}: 0 spec page(s) (could not load model page)")
        return []

    # Collect listing URLs from model page
    listing_hrefs: list[str] = []
    for a in model_soup.select("a[href]"):
        href = a.get("href", "")
        if listing_re.match(href) and href not in seen_listing:
            seen_listing.add(href)
            listing_hrefs.append(href)
        elif data_re.match(href):          # sometimes data links appear directly
            _add_data(href)

    # ── Visit each listing page → collect data URLs ───────────────────────
    for listing_href in listing_hrefs:
        listing_url  = BASE + listing_href
        listing_soup = fetch(listing_url, referer=model_url)
        if not listing_soup:
            continue

        added_from_this = 0
        for a in listing_soup.select("a[href]"):
            href = a.get("href", "")
            if data_re.match(href):
                _add_data(href)
                added_from_this += 1

        log.debug(f"      listing {listing_href} → {added_from_this} variant(s)")

        # If still no data links, try one more level (some sites nest deeper)
        if added_from_this == 0:
            sub_listing_re = re.compile(
                rf"^/{ms}/{mds}/[^/]+/([^/]+)/$"
            )
            for a in listing_soup.select("a[href]"):
                href = a.get("href", "")
                if sub_listing_re.match(href):
                    sub_url  = BASE + href
                    sub_soup = fetch(sub_url, referer=listing_url)
                    if sub_soup:
                        for sa in sub_soup.select("a[href]"):
                            sh = sa.get("href", "")
                            if data_re.match(sh):
                                _add_data(sh)

    log.info(f"    {make_slug}/{model_slug}: {len(found)} variant spec page(s)")
    return found


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
    args = ap.parse_args()

    if args.list_makes:
        for slug, name in sorted(KNOWN_MAKES.items()):
            print(f"  {slug:<25} {name}")
        sys.exit(0)

    if args.debug:
        DEBUG_MODE = True
        log.info("Debug mode ON — failed pages will be saved to debug_html/")

    df = scrape_urls(args.urls) if args.urls else crawl(args.makes, args.limit)

    print(f"\nFinished. {len(df)} total row(s) in {OUTPUT_CSV}")
    if not df.empty:
        cols = [c for c in ["make", "model", "variant", "year_from", "stock_hp"]
                if c in df.columns]
        print(df[cols].head(10).to_string(index=False))
