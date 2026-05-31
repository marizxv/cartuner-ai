"""
scraper_cars.py — parkers.co.uk → cars_raw.csv

Target URL structure:
  /make/model/generation/variant/specs/
  e.g. /bmw/3-series/m3-coupe-2001/2d/specs/

Every scraped row is written to CSV immediately — safe to interrupt at any time.

Install:  pip install cloudscraper lxml beautifulsoup4 pandas
Run:
    python scraper_cars.py --makes volkswagen ford bmw
    python scraper_cars.py --limit 100
    python scraper_cars.py --urls https://www.parkers.co.uk/bmw/3-series/m3-coupe-2001/2d/specs/
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

OUTPUT_CSV = "cars_raw.csv"
BASE = "https://www.parkers.co.uk"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
log.info(_client_msg)

# ── Known real car makes on parkers.co.uk (slug → display name) ───────────────
# This is the ground-truth list. Using a hardcoded allowlist is far more
# reliable than trying to scrape the makes page, which returns many false
# positives (car-awards, cars-for-sale, etc.).
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

# Slugs that are never a model — only listing/navigation pages
_NOT_A_MODEL = frozenset({
    "for-sale", "nearly-new", "owners-reviews", "owner-reviews", "reviews",
    "road-tests", "news", "advice", "compare", "leasing", "electric",
    "hybrid", "used", "new", "finance", "insurance", "shortlist",
    "type-small-city", "type-hatchback", "type-saloon", "type-suv",
    "type-estate", "type-coupe", "type-convertible", "type-mpv",
    "type-pickup", "type-small-van", "type-large-van",
})

# Slugs that are never a generation — only listing/navigation pages
_NOT_A_GEN = frozenset({
    "for-sale", "nearly-new", "reviews", "owners-reviews", "owner-reviews",
    "news", "advice", "shortlist", "finance", "leasing",
})


# ── Helpers ───────────────────────────────────────────────────────────────────
def _sleep(lo: float = 1.5, hi: float = 3.5) -> None:
    time.sleep(random.uniform(lo, hi))


def fetch(url: str, referer: str = BASE + "/") -> BeautifulSoup | None:
    """Fetch a URL and return a BeautifulSoup object, with up to 3 retries."""
    SCRAPER.headers.update({"Referer": referer})
    for attempt in range(1, 4):
        try:
            log.info(f"GET {url}")
            r = SCRAPER.get(url, timeout=20)
            if r.status_code == 403:
                wait = 15 * attempt
                log.warning(f"403 Forbidden — waiting {wait}s before retry {attempt}/3")
                time.sleep(wait)
                continue
            if r.status_code in (404, 410):
                log.debug(f"Skipping {r.status_code}: {url}")
                return None
            r.raise_for_status()
            _sleep()
            return BeautifulSoup(r.text, "lxml")
        except Exception as exc:
            log.warning(f"Attempt {attempt}/3 failed: {exc}")
            _sleep(4, 8)
    log.error(f"Giving up after 3 attempts: {url}")
    return None


def _num(text: str | None, pat: str) -> float | None:
    if not text:
        return None
    m = re.search(pat, str(text).replace(",", ""))
    return float(m.group(1)) if m else None


# ── STEP 1: Resolve makes ──────────────────────────────────────────────────────
def get_makes(makes_filter: list[str] | None = None) -> list[tuple[str, str]]:
    """
    Returns [(display_name, make_url), ...] using the hardcoded KNOWN_MAKES
    allowlist, optionally filtered to the slugs/names in makes_filter.

    Falls back to scraping /cars/ only to discover makes NOT in KNOWN_MAKES.
    """
    log.info("Warm-up: visiting homepage to obtain cookies...")
    try:
        SCRAPER.get(BASE + "/", timeout=20)
        _sleep(2, 4)
    except Exception as exc:
        log.warning(f"Warm-up failed: {exc}")

    # Start from the known list
    makes: list[tuple[str, str]] = [
        (name, f"{BASE}/{slug}/") for slug, name in KNOWN_MAKES.items()
    ]

    # Also try to discover any unlisted makes from /cars/ page
    soup = fetch(BASE + "/cars/")
    if soup:
        known_slugs = set(KNOWN_MAKES.keys())
        discovered = 0
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            m = re.match(r"^/([a-z][a-z0-9-]{1,25})/$", href)
            if not m:
                continue
            slug = m.group(1)
            # Only add if not already known and looks like a brand (no "car", "sale", etc.)
            if (slug not in known_slugs
                    and not any(w in slug for w in [
                        "car", "sale", "award", "review", "test", "valuation",
                        "finance", "insurance", "advice", "long", "owner",
                        "electric", "used", "new", "van", "bike", "truck",
                        "motorhome", "sell", "short",
                    ])):
                name = a.get_text(strip=True) or slug.replace("-", " ").title()
                if name and len(name) <= 40:
                    url = f"{BASE}/{slug}/"
                    makes.append((name, url))
                    known_slugs.add(slug)
                    discovered += 1
        if discovered:
            log.info(f"Discovered {discovered} additional make(s) from /cars/")

    # Apply filter
    if makes_filter:
        fs = {x.lower().replace(" ", "-") for x in makes_filter}
        makes = [
            (name, url) for name, url in makes
            if url.rstrip("/").split("/")[-1] in fs
            or name.lower() in {x.lower() for x in makes_filter}
        ]

    log.info(f"Processing {len(makes)} make(s)")
    return makes


# ── STEP 2: Models for a make ──────────────────────────────────────────────────
def get_models(make_slug: str, make_url: str) -> list[tuple[str, str]]:
    """
    Returns [(model_name, model_url), ...].
    Only accepts hrefs of the form /make/model/ and rejects navigation slugs.
    """
    soup = fetch(make_url, referer=BASE + "/cars/")
    if not soup:
        return []

    models: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Strictly: href must be /make_slug/something/ — nothing deeper, nothing else
    pat = re.compile(rf"^/{re.escape(make_slug)}/([a-z0-9][a-z0-9-]{{1,60}})/$")

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = pat.match(href)
        if not m:
            continue
        model_slug = m.group(1)
        if model_slug in _NOT_A_MODEL:
            continue
        url = f"{BASE}/{make_slug}/{model_slug}/"
        if url not in seen:
            seen.add(url)
            name = a.get_text(strip=True) or model_slug.replace("-", " ").title()
            models.append((name, url))

    log.info(f"  {make_slug}: {len(models)} model(s)")
    return models


# ── STEP 3: Spec URLs for a model ─────────────────────────────────────────────
def get_spec_urls(make_slug: str, model_slug: str, model_url: str) -> list[str]:
    """
    Walks model page → generation pages → variant pages to collect all
    URLs matching the exact pattern:
        /make/model/generation/variant/specs/

    Never follows for-sale, reviews, or other non-spec paths.
    """
    soup = fetch(model_url, referer=f"{BASE}/{make_slug}/")
    if not soup:
        return []

    found: list[str] = []
    seen_specs: set[str] = set()
    seen_gens: set[str] = set()

    # Regex for a valid spec page at depth 5
    spec_re = re.compile(
        rf"^/{re.escape(make_slug)}/{re.escape(model_slug)}"
        r"/[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*/specs/$"
    )
    # Regex for a generation page at depth 3
    gen_re = re.compile(
        rf"^/{re.escape(make_slug)}/{re.escape(model_slug)}"
        r"/([a-z0-9][a-z0-9-]*)/$"
    )
    # Regex for a variant page at depth 4
    var_re = re.compile(
        rf"^/{re.escape(make_slug)}/{re.escape(model_slug)}"
        r"/[a-z0-9][a-z0-9-]*/([a-z0-9][a-z0-9-]*)/$"
    )

    def _harvest_specs(soup_obj: BeautifulSoup) -> None:
        for a in soup_obj.select("a[href]"):
            href = a.get("href", "")
            if spec_re.match(href):
                full = BASE + href
                if full not in seen_specs:
                    seen_specs.add(full)
                    found.append(full)

    def _collect_gen_urls(soup_obj: BeautifulSoup) -> list[str]:
        urls = []
        for a in soup_obj.select("a[href]"):
            href = a.get("href", "")
            m = gen_re.match(href)
            if m and m.group(1) not in _NOT_A_GEN:
                full = BASE + href
                if full not in seen_gens:
                    seen_gens.add(full)
                    urls.append(full)
        return urls

    def _collect_var_urls(soup_obj: BeautifulSoup, gen_url: str) -> list[str]:
        """Collect variant-level URLs from a generation page."""
        gen_path = gen_url.replace(BASE, "")
        gen_slug_local = gen_path.strip("/").split("/")[-1]
        local_var_re = re.compile(
            rf"^/{re.escape(make_slug)}/{re.escape(model_slug)}"
            rf"/{re.escape(gen_slug_local)}/([a-z0-9][a-z0-9-]*)/$"
        )
        urls = []
        for a in soup_obj.select("a[href]"):
            href = a.get("href", "")
            m = local_var_re.match(href)
            if m:
                full = BASE + href
                urls.append(full)
        return urls

    # Level 1: check model page for direct spec links
    _harvest_specs(soup)

    # Level 2: drill into generation pages
    gen_urls = _collect_gen_urls(soup)
    for gen_url in gen_urls:
        gen_soup = fetch(gen_url, referer=model_url)
        if not gen_soup:
            continue
        _harvest_specs(gen_soup)

        # Level 3: drill into variant pages if no specs found at gen level
        var_urls = _collect_var_urls(gen_soup, gen_url)
        for var_url in var_urls:
            var_soup = fetch(var_url, referer=gen_url)
            if var_soup:
                _harvest_specs(var_soup)

    log.info(f"    {make_slug}/{model_slug}: {len(found)} spec page(s)")
    return found


# ── STEP 4: Parse a /specs/ page ──────────────────────────────────────────────
def scrape_spec_page(url: str) -> dict | None:
    """
    Fetches a /specs/ URL and returns a dict of attributes, or None on failure.
    Expected URL: BASE/make/model/generation/variant/specs/
    """
    parts = url.replace(BASE, "").strip("/").split("/")
    make_slug    = parts[0] if len(parts) > 0 else ""
    model_slug   = parts[1] if len(parts) > 1 else ""
    gen_slug     = parts[2] if len(parts) > 2 else ""
    variant_slug = parts[3] if len(parts) > 3 else ""

    soup = fetch(url, referer=f"{BASE}/{make_slug}/{model_slug}/{gen_slug}/")
    if not soup:
        return None

    raw: dict[str, str] = {}
    for item in soup.select("li.specs-detail-table__item"):
        lbl = item.select_one(".specs-detail-table__item__label")
        val = item.select_one(".specs-detail-table__item__value")
        if lbl and val:
            raw[lbl.get_text(strip=True).lower()] = val.get_text(strip=True)

    if not raw:
        log.warning(f"No spec rows found at {url}")
        return None

    h1 = soup.find("h1")
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


# ── Immediate CSV write (interrupt-safe) ───────────────────────────────────────
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
    """
    Append one row to the CSV immediately. Creates the file + header if needed.
    Each write is flushed to disk — Ctrl+C loses at most the in-flight request.
    """
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k) for k in _CSV_FIELDS})


def _load_done_urls(path: Path) -> set[str]:
    """Load already-scraped source_urls so we can skip them on resume."""
    if not path.exists():
        return set()
    try:
        df = pd.read_csv(path, usecols=["source_url"])
        urls = set(df["source_url"].dropna())
        log.info(f"Resuming — {len(urls)} URL(s) already in {path}")
        return urls
    except Exception as exc:
        log.warning(f"Could not read existing CSV for resume: {exc}")
        return set()


# ── Main crawler ───────────────────────────────────────────────────────────────
def crawl(makes_filter: list[str] | None = None, limit: int | None = None) -> pd.DataFrame:
    out  = Path(OUTPUT_CSV)
    done = _load_done_urls(out)
    total = 0

    def _sigint(sig, frame):
        log.info(f"Interrupted — {total} car(s) saved to {out}. Exiting cleanly.")
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    makes = get_makes(makes_filter)
    if not makes:
        log.error("No makes to process.")
        return pd.DataFrame()

    for make_name, make_url in makes:
        make_slug = make_url.rstrip("/").split("/")[-1]

        for model_name, model_url in get_models(make_slug, make_url):
            model_slug = model_url.rstrip("/").split("/")[-1]

            for spec_url in get_spec_urls(make_slug, model_slug, model_url):
                if spec_url in done:
                    log.debug(f"Already scraped, skipping: {spec_url}")
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
                    log.warning(f"No data extracted: {spec_url}")

                if limit and total >= limit:
                    log.info(f"Reached limit of {limit} cars.")
                    return pd.read_csv(out) if out.exists() else pd.DataFrame()

    log.info(f"Crawl complete — {total} new car(s) saved to {out}")
    return pd.read_csv(out) if out.exists() else pd.DataFrame()


# ── Scrape explicit URLs ───────────────────────────────────────────────────────
def scrape_urls(urls: list[str]) -> pd.DataFrame:
    """Scrape a list of explicit /specs/ URLs, appending results to the CSV."""
    out  = Path(OUTPUT_CSV)
    done = _load_done_urls(out)
    total = 0

    for url in urls:
        if url in done:
            log.info(f"Already scraped, skipping: {url}")
            continue
        row = scrape_spec_page(url)
        if row:
            _append_row(row, out)
            done.add(url)
            total += 1
            log.info(f"[{total}] Saved: {row['make']} {row['model']} {row['variant']}")
        else:
            log.warning(f"No data extracted: {url}")

    log.info(f"Done — {total} new row(s) appended to {out}")
    return pd.read_csv(out) if out.exists() else pd.DataFrame()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Scrape car specs from parkers.co.uk → cars_raw.csv"
    )
    ap.add_argument("--makes", nargs="+", metavar="MAKE",
                    help="Make slug(s) to scrape, e.g. volkswagen ford bmw")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="Stop after N cars total")
    ap.add_argument("--urls", nargs="+", metavar="URL",
                    help="Scrape specific /specs/ URLs directly")
    ap.add_argument("--list-makes", action="store_true",
                    help="Print all known makes and exit")
    args = ap.parse_args()

    if args.list_makes:
        for slug, name in sorted(KNOWN_MAKES.items()):
            print(f"  {slug:<25} {name}")
        sys.exit(0)

    df = scrape_urls(args.urls) if args.urls else crawl(args.makes, args.limit)

    print(f"\nFinished. {len(df)} total row(s) in {OUTPUT_CSV}")
    if not df.empty:
        cols = [c for c in ["make", "model", "variant", "year_from", "stock_hp"]
                if c in df.columns]
        print(df[cols].head(10).to_string(index=False))
