"""
scraper_cars.py — parkers.co.uk → cars_raw.csv

Follows only the strict URL path:
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

# ── HTTP client setup ──────────────────────────────────────────────────────────
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

# ── Strict URL patterns ────────────────────────────────────────────────────────
# Segments that are never a car make on parkers.co.uk
_NOT_A_MAKE = {
    "cars", "vans", "vans-pickups", "bikes", "trucks", "motorhomes",
    "car-reviews", "car-news", "advice", "valuations", "finance",
    "insurance", "electric-cars", "used-cars", "new-cars", "sitemap",
    "contact", "about", "login", "register", "search", "privacy",
    "terms", "cookies", "faq", "help", "subscription", "newsletters",
    "for-sale", "nearly-new", "owners-reviews", "road-tests",
    "news", "guides", "compare", "leasing", "van-reviews",
}

# Model-level slugs that are navigation/listing pages — not real models
_NOT_A_MODEL = {
    "for-sale", "nearly-new", "owners-reviews", "reviews",
    "road-tests", "news", "advice", "compare", "leasing",
    "electric", "hybrid", "used", "new",
}

# Generation/trim slugs that signal non-spec pages
_NOT_A_GEN = {"for-sale", "nearly-new", "reviews", "owners-reviews", "news", "advice"}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _sleep(lo: float = 1.5, hi: float = 3.5) -> None:
    time.sleep(random.uniform(lo, hi))


def fetch(url: str, referer: str = BASE + "/") -> BeautifulSoup | None:
    """Fetch a URL and return a BeautifulSoup, retrying up to 3 times on errors."""
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


def _href_slug(a_tag, index: int) -> str | None:
    """Return the slug at position `index` of an <a> href, or None."""
    href = a_tag.get("href", "")
    parts = href.strip("/").split("/")
    return parts[index] if len(parts) > index else None


# ── STEP 1: Collect car makes from /cars/ ─────────────────────────────────────
def get_makes() -> list[tuple[str, str]]:
    """
    Returns [(make_name, make_url), ...] for real car brands only.
    Parses /cars/ and keeps only first-level slugs not in _NOT_A_MAKE.
    """
    log.info("Warm-up: visiting homepage to obtain cookies...")
    try:
        SCRAPER.get(BASE + "/", timeout=20)
        _sleep(2, 4)
    except Exception as exc:
        log.warning(f"Warm-up failed: {exc}")

    soup = fetch(BASE + "/cars/")
    if not soup:
        return []

    makes, seen = [], set()
    # Only accept hrefs of the form  /slug/  (exactly one path segment)
    pat = re.compile(r"^/([a-z][a-z0-9-]{1,30})/$")
    for a in soup.select("a[href]"):
        m = pat.match(a.get("href", ""))
        if not m:
            continue
        slug = m.group(1)
        if slug in _NOT_A_MAKE:
            continue
        name = a.get_text(strip=True) or slug.replace("-", " ").title()
        if not name or len(name) > 40:
            continue
        url = f"{BASE}/{slug}/"
        if url not in seen:
            seen.add(url)
            makes.append((name, url))

    log.info(f"Found {len(makes)} car makes")
    return makes


# ── STEP 2: Collect models for a make ─────────────────────────────────────────
def get_models(make_slug: str, make_url: str) -> list[tuple[str, str]]:
    """
    Returns [(model_name, model_url), ...].
    Only follows /make/model/ links — skips for-sale, reviews, etc.
    """
    soup = fetch(make_url, referer=BASE + "/cars/")
    if not soup:
        return []

    models, seen = [], set()
    # Accept hrefs that are exactly  /make/model/
    pat = re.compile(rf"^/{re.escape(make_slug)}/([^/]+)/$")
    for a in soup.select("a[href]"):
        m = pat.match(a.get("href", ""))
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

    log.info(f"  {make_slug}: {len(models)} models found")
    return models


# ── STEP 3: Collect all /specs/ URLs for a model ─────────────────────────────
def get_spec_urls(make_slug: str, model_slug: str, model_url: str) -> list[str]:
    """
    Walks  /make/model/  →  /make/model/generation/  →  /make/model/generation/variant/specs/
    Only follows the strict depth-4 specs path. Skips all listing/sale pages.

    Target pattern: /make/model/generation/variant/specs/
    """
    soup = fetch(model_url, referer=f"{BASE}/{make_slug}/")
    if not soup:
        return []

    found: list[str] = []
    seen: set[str] = set()

    # Pattern for a generation page: /make/model/generation/
    gen_pat = re.compile(
        rf"^/{re.escape(make_slug)}/{re.escape(model_slug)}/([^/]+)/$"
    )
    # Pattern for a spec page: /make/model/generation/variant/specs/
    spec_pat = re.compile(
        rf"^/{re.escape(make_slug)}/{re.escape(model_slug)}/[^/]+/[^/]+/specs/$"
    )

    def _collect_specs(soup_obj: BeautifulSoup) -> None:
        for a in soup_obj.select("a[href]"):
            href = a.get("href", "")
            if spec_pat.match(href):
                full = BASE + href
                if full not in seen:
                    seen.add(full)
                    found.append(full)

    # Check if spec links are already on the model page
    _collect_specs(soup)

    # If not, drill into generation pages
    gen_urls: list[str] = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = gen_pat.match(href)
        if m and m.group(1) not in _NOT_A_GEN:
            full = BASE + href
            if full not in {u for u in gen_urls}:
                gen_urls.append(full)

    for gen_url in gen_urls:
        gen_soup = fetch(gen_url, referer=model_url)
        if not gen_soup:
            continue
        _collect_specs(gen_soup)

        # Some sites have an extra variant listing level before /specs/
        # e.g.  /make/model/generation/  →  /make/model/generation/variant/
        variant_pat = re.compile(
            rf"^/{re.escape(make_slug)}/{re.escape(model_slug)}/[^/]+/([^/]+)/$"
        )
        for a in gen_soup.select("a[href]"):
            href = a.get("href", "")
            if variant_pat.match(href) and not href.endswith("/specs/"):
                var_url = BASE + href
                var_soup = fetch(var_url, referer=gen_url)
                if var_soup:
                    _collect_specs(var_soup)

    log.info(f"    {make_slug}/{model_slug}: {len(found)} spec pages")
    return found


# ── STEP 4: Parse a /specs/ page ──────────────────────────────────────────────
def scrape_spec_page(url: str) -> dict | None:
    """
    Fetches a /specs/ URL and returns a dict of car attributes, or None on failure.
    URL structure expected: /make/model/generation/variant/specs/
    """
    parts = url.replace(BASE, "").strip("/").split("/")
    make_slug    = parts[0] if len(parts) > 0 else ""
    model_slug   = parts[1] if len(parts) > 1 else ""
    gen_slug     = parts[2] if len(parts) > 2 else ""
    variant_slug = parts[3] if len(parts) > 3 else ""

    soup = fetch(url, referer=f"{BASE}/{make_slug}/{model_slug}/{gen_slug}/")
    if not soup:
        return None

    # Extract key-value spec table
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

    # Year extraction from generation slug and title
    yrs = sorted(
        int(y)
        for y in re.findall(r"\b(20\d{2}|19\d{2})\b", gen_slug + " " + title)
    )
    year_from = yrs[0]  if yrs          else None
    year_to   = yrs[-1] if len(yrs) > 1 else None

    # Engine type classification
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


# ── CSV writer — immediate, interrupt-safe ─────────────────────────────────────
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
    Append a single row to the CSV immediately after scraping.
    Creates the file with a header if it does not exist yet.
    This makes every write atomic — Ctrl+C loses at most the row being processed.
    """
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k) for k in _CSV_FIELDS})


def _load_done_urls(path: Path) -> set[str]:
    """Read already-scraped source_urls from an existing CSV (for resume)."""
    if not path.exists():
        return set()
    try:
        df = pd.read_csv(path, usecols=["source_url"])
        urls = set(df["source_url"].dropna())
        log.info(f"Resuming — {len(urls)} URLs already in {path}")
        return urls
    except Exception as exc:
        log.warning(f"Could not read existing CSV for resume: {exc}")
        return set()


# ── Main crawler ───────────────────────────────────────────────────────────────
def crawl(makes_filter: list[str] | None = None, limit: int | None = None) -> pd.DataFrame:
    out = Path(OUTPUT_CSV)
    done = _load_done_urls(out)
    total = 0

    # Graceful Ctrl+C: finish current write, then exit
    def _sigint(sig, frame):
        log.info(f"Interrupted — {total} cars saved to {out}. Exiting cleanly.")
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    makes = get_makes()
    if not makes:
        log.error("No makes found — the site may be blocking requests.")
        return pd.DataFrame()

    if makes_filter:
        fs = {x.lower() for x in makes_filter}
        makes = [
            (name, url) for name, url in makes
            if name.lower() in fs or url.rstrip("/").split("/")[-1] in fs
        ]
        log.info(f"Filtered to {len(makes)} make(s): {[n for n, _ in makes]}")

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
                    _append_row(row, out)       # write immediately to disk
                    done.add(spec_url)
                    total += 1
                    log.info(
                        f"[{total}] Saved: {row['make']} {row['model']} "
                        f"{row['variant']} ({row['year_from']}) — {row['stock_hp']} hp"
                    )
                else:
                    log.warning(f"No data extracted from: {spec_url}")

                if limit and total >= limit:
                    log.info(f"Reached limit of {limit} cars.")
                    return pd.read_csv(out) if out.exists() else pd.DataFrame()

    log.info(f"Crawl complete — {total} new cars saved to {out}")
    return pd.read_csv(out) if out.exists() else pd.DataFrame()


# ── Scrape explicit URLs ───────────────────────────────────────────────────────
def scrape_urls(urls: list[str]) -> pd.DataFrame:
    """Scrape a list of explicit /specs/ URLs and append them to the CSV."""
    out = Path(OUTPUT_CSV)
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
            log.warning(f"No data extracted from: {url}")

    log.info(f"Done — {total} new rows appended to {out}")
    return pd.read_csv(out) if out.exists() else pd.DataFrame()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Scrape car specs from parkers.co.uk into cars_raw.csv"
    )
    ap.add_argument("--makes", nargs="+", metavar="MAKE",
                    help="One or more make slugs, e.g. volkswagen ford bmw")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="Stop after scraping N cars")
    ap.add_argument("--urls", nargs="+", metavar="URL",
                    help="Scrape specific /specs/ URLs directly")
    args = ap.parse_args()

    df = scrape_urls(args.urls) if args.urls else crawl(args.makes, args.limit)

    print(f"\nFinished. {len(df)} total rows in {OUTPUT_CSV}")
    if not df.empty:
        cols = [c for c in ["make", "model", "variant", "year_from", "stock_hp"] if c in df.columns]
        print(df[cols].head(10).to_string(index=False))
