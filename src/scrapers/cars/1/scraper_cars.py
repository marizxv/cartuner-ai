"""
scraper_cars.py
===============
Scrapes stock car specs from parkers.co.uk → cars_raw.csv

Strategy:
  1. Crawl /cars/ listing page to get all make slugs
  2. For each make, crawl model pages
  3. For each model, crawl variant/generation pages
  4. For each variant, scrape /specs/ page

Output columns (cars_raw.csv):
    car_id, make, model, variant, year_from, year_to,
    displacement_cc, cylinders, stock_hp, stock_torque_nm,
    weight_kg, drivetrain, transmission, engine_type,
    fuel_type, drag_coef, frontal_area_m2,
    top_speed_stock, zero_hundred_stock, source_url

Usage:
    python scraper_cars.py                        # scrape everything
    python scraper_cars.py --makes ford volkswagen # only specific makes
    python scraper_cars.py --limit 50              # stop after N cars
"""

import re
import time
import logging
import argparse
import hashlib
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL   = "https://www.parkers.co.uk"
OUTPUT_CSV = "cars_raw.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT":             "1",
    "Connection":      "keep-alive",
}

DELAY   = 2.0   # seconds between requests (be polite)
TIMEOUT = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str, retries: int = 3) -> BeautifulSoup | None:
    """GET url, return BeautifulSoup. Returns None on repeated failure."""
    for attempt in range(1, retries + 1):
        try:
            log.info(f"GET {url}")
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            time.sleep(DELAY)
            return BeautifulSoup(r.text, "lxml")
        except requests.HTTPError as e:
            if e.response.status_code in (403, 404, 410):
                log.warning(f"Skipping {url} — HTTP {e.response.status_code}")
                return None
            log.warning(f"Attempt {attempt}/{retries} failed for {url}: {e}")
            time.sleep(DELAY * attempt)
        except Exception as e:
            log.warning(f"Attempt {attempt}/{retries} failed for {url}: {e}")
            time.sleep(DELAY * attempt)
    log.error(f"Giving up on {url}")
    return None


def _num(text: str, pattern: str) -> float | None:
    """Extract first float matching pattern from text. Returns None on failure."""
    if not text:
        return None
    m = re.search(pattern, text.replace(",", ""))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Step 1: discover makes
# ---------------------------------------------------------------------------

def get_makes() -> list[tuple[str, str]]:
    """
    Returns list of (make_name, make_url) from parkers /cars/ listing.
    e.g. [('Audi', 'https://www.parkers.co.uk/audi/'), ...]
    """
    soup = fetch(f"{BASE_URL}/cars/")
    if not soup:
        log.error("Cannot reach parkers /cars/ — check your network.")
        return []

    makes = []
    # Parkers renders makes as links inside an A-Z listing or a grid
    for a in soup.select("a[href]"):
        href = a["href"]
        # Make URLs look like /ford/ or /volkswagen/ — single slug, no sub-path
        m = re.match(r"^/([a-z0-9-]+)/$", href)
        if m:
            slug = m.group(1)
            name = a.get_text(strip=True)
            # Filter out navigation links by checking the text looks like a brand
            if name and len(name) > 1 and not any(w in name.lower() for w in
                    ["home", "news", "review", "advice", "menu", "login", "cookie"]):
                url = BASE_URL + href
                makes.append((name, url))

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for name, url in makes:
        if url not in seen:
            seen.add(url)
            unique.append((name, url))

    log.info(f"Found {len(unique)} makes")
    return unique


# ---------------------------------------------------------------------------
# Step 2: discover models for a make
# ---------------------------------------------------------------------------

def get_models(make_name: str, make_url: str) -> list[tuple[str, str]]:
    """
    Returns list of (model_name, model_url) for a given make page.
    e.g. [('Golf', 'https://www.parkers.co.uk/volkswagen/golf/'), ...]
    """
    soup = fetch(make_url)
    if not soup:
        return []

    models = []
    # Model links sit one level deeper: /make/model/
    pattern = re.compile(rf"^/{re.escape(make_name.lower())}/([^/]+)/$")
    for a in soup.select("a[href]"):
        href = a["href"]
        m = pattern.match(href)
        if m:
            model_slug = m.group(1)
            name = a.get_text(strip=True) or model_slug.replace("-", " ").title()
            url  = BASE_URL + href
            models.append((name, url))

    # Deduplicate
    seen = set()
    unique = []
    for name, url in models:
        if url not in seen:
            seen.add(url)
            unique.append((name, url))

    log.info(f"  {make_name}: {len(unique)} models")
    return unique


# ---------------------------------------------------------------------------
# Step 3: discover variants/generations for a model
# ---------------------------------------------------------------------------

def get_spec_urls(make_name: str, model_name: str, model_url: str) -> list[str]:
    """
    Returns all /specs/ URLs for a given model page.
    Parkers organises cars as: /make/model/generation/variant/specs/
    We look for any href ending in /specs/ under the model's URL prefix.
    """
    soup = fetch(model_url)
    if not soup:
        return []

    prefix = model_url.rstrip("/")
    spec_urls = []

    for a in soup.select("a[href]"):
        href = a["href"]
        full = BASE_URL + href if href.startswith("/") else href
        if full.startswith(prefix) and full.endswith("/specs/"):
            spec_urls.append(full)

    # Also check if the model page itself has a "Specs" tab we need to follow
    if not spec_urls:
        # Look for links to sub-pages (generations), then recurse one level
        gen_pattern = re.compile(rf"^/{make_name.lower()}/{re.escape(model_url.split('/')[-2])}/([^/]+)/$")
        for a in soup.select("a[href]"):
            m = gen_pattern.match(a["href"])
            if m:
                gen_url = BASE_URL + a["href"]
                gen_soup = fetch(gen_url)
                if gen_soup:
                    for a2 in gen_soup.select("a[href]"):
                        h2 = a2["href"]
                        full2 = BASE_URL + h2 if h2.startswith("/") else h2
                        if full2.startswith(BASE_URL) and full2.endswith("/specs/"):
                            spec_urls.append(full2)

    # Deduplicate
    spec_urls = list(dict.fromkeys(spec_urls))
    log.info(f"    {model_name}: {len(spec_urls)} spec pages")
    return spec_urls


# ---------------------------------------------------------------------------
# Step 4: scrape one /specs/ page
# ---------------------------------------------------------------------------

def scrape_spec_page(url: str) -> dict | None:
    """
    Scrape one parkers /specs/ page and return a flat dict
    ready to be a row in cars_raw.csv.
    """
    soup = fetch(url)
    if not soup:
        return None

    # Parse URL to extract make / model / generation / variant slugs
    # URL: https://www.parkers.co.uk/MAKE/MODEL/GEN/VARIANT/specs/
    parts = url.replace(BASE_URL, "").strip("/").split("/")
    make_slug    = parts[0] if len(parts) > 0 else ""
    model_slug   = parts[1] if len(parts) > 1 else ""
    gen_slug     = parts[2] if len(parts) > 2 else ""
    variant_slug = parts[3] if len(parts) > 3 else ""

    # Every spec row: <li class="specs-detail-table__item">
    #   <span ...label>Label</span>  <span ...value>Value</span>
    raw: dict[str, str] = {}
    for item in soup.select("li.specs-detail-table__item"):
        lbl = item.select_one(".specs-detail-table__item__label")
        val = item.select_one(".specs-detail-table__item__value")
        if lbl and val:
            key = lbl.get_text(strip=True).lower().strip()
            raw[key] = val.get_text(strip=True)

    if not raw:
        log.warning(f"No spec data at {url}")
        return None

    # Human-readable title from h1
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else url

    # ---------- Parse year range from generation slug ----------
    # Gen slugs often look like: "mk7-2012", "2015", "st-2012", "(2019-)"
    year_from, year_to = _parse_years(gen_slug, title)

    # ---------- Convert top speed mph → km/h ----------
    top_speed_mph = _num(raw.get("top speed", ""), r"(\d+(?:\.\d+)?)")
    top_speed_kph = round(top_speed_mph * 1.60934, 1) if top_speed_mph else None

    # ---------- 0-60 mph ≈ 0-100 km/h (within 0.2s) ----------
    zero_60 = _num(raw.get("acceleration 0-60mph", ""), r"(\d+(?:\.\d+)?)")

    # ---------- Engine type heuristic ----------
    fuel  = (raw.get("fuel type", "") or "").lower()
    title_l = title.lower()
    engine_type = "N/A"
    if "electric" in fuel or "ev" in fuel:
        engine_type = "EV"
    elif "hybrid" in fuel or "hybrid" in title_l:
        engine_type = "Hybrid"
    elif any(w in title_l for w in ["tdi", "tdci", "cdi", "dci", "d ", " d4", " d5", "diesel"]):
        engine_type = "Diesel"
    elif any(w in title_l for w in ["tsi", "tfsi", "turbo", "t ", "gti", "sti", "wrx", "type r"]):
        engine_type = "Turbocharged"
    elif fuel in ("petrol", "gasoline"):
        engine_type = "Naturally Aspirated"

    # ---------- Stable car_id from URL ----------
    car_id = "CAR_" + hashlib.md5(url.encode()).hexdigest()[:8].upper()

    return {
        "car_id":           car_id,
        "make":             make_slug.replace("-", " ").title(),
        "model":            model_slug.replace("-", " ").title(),
        "variant":          variant_slug.replace("-", " "),
        "raw_title":        title,
        "year_from":        year_from,
        "year_to":          year_to,
        "displacement_cc":  _num(raw.get("engine size", ""), r"(\d+)"),
        "cylinders":        _num(raw.get("cylinders", ""), r"(\d+)"),
        "stock_hp":         _num(raw.get("horsepower", ""), r"(\d+)"),
        "stock_torque_nm":  _num(raw.get("torque", ""), r"(\d+)\s*[Nn][Mm]"),
        "weight_kg":        _num(raw.get("weight", ""), r"(\d+)"),
        "drivetrain":       raw.get("drivetrain"),
        "transmission":     raw.get("transmission"),
        "engine_type":      engine_type,
        "fuel_type":        raw.get("fuel type"),
        "cylinders_text":   raw.get("cylinders"),
        "drag_coef":        _num(raw.get("drag coefficient", ""), r"(\d+\.\d+)"),
        "frontal_area_m2":  None,   # Parkers does not publish this
        "top_speed_stock":  top_speed_kph,
        "top_speed_mph":    top_speed_mph,
        "zero_hundred_stock": zero_60,
        "co2_gkm":          _num(raw.get("co2", ""), r"(\d+)"),
        "source_url":       url,
    }


def _parse_years(slug: str, title: str) -> tuple[int | None, int | None]:
    """Try to extract year_from / year_to from a slug or page title."""
    # Slug: "mk7-2012" → from=2012; "(2019-2023)" → from=2019, to=2023
    years = re.findall(r"\b(20\d{2}|19\d{2})\b", slug + " " + title)
    years = [int(y) for y in years]
    if len(years) >= 2:
        return min(years), max(years)
    elif len(years) == 1:
        return years[0], None
    return None, None


# ---------------------------------------------------------------------------
# Main crawler
# ---------------------------------------------------------------------------

def crawl(
    makes_filter: list[str] | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """
    Full crawl of parkers.co.uk/cars/.
    makes_filter: if set, only scrape those makes (case-insensitive slugs).
    limit:        stop after this many successfully scraped cars.
    """
    output_path = Path(OUTPUT_CSV)
    # Load already-scraped URLs to allow resume
    already_scraped: set[str] = set()
    if output_path.exists():
        try:
            existing = pd.read_csv(output_path)
            already_scraped = set(existing["source_url"].dropna())
            log.info(f"Resuming — {len(already_scraped)} cars already in {OUTPUT_CSV}")
        except Exception:
            pass

    all_rows: list[dict] = []
    scraped_count = 0

    makes = get_makes()
    if not makes:
        log.error("No makes found. Check if parkers.co.uk is reachable.")
        return pd.DataFrame()

    if makes_filter:
        filter_set = {m.lower().strip() for m in makes_filter}
        makes = [(n, u) for n, u in makes if n.lower() in filter_set]
        log.info(f"Filtered to {len(makes)} makes: {[n for n,_ in makes]}")

    for make_name, make_url in makes:
        models = get_models(make_name, make_url)

        for model_name, model_url in models:
            spec_urls = get_spec_urls(make_name, model_name, model_url)

            for spec_url in spec_urls:
                if spec_url in already_scraped:
                    log.info(f"  SKIP (already scraped): {spec_url}")
                    continue

                row = scrape_spec_page(spec_url)
                if row:
                    all_rows.append(row)
                    scraped_count += 1
                    log.info(f"  ✓ {row['make']} {row['model']} {row['variant']} "
                             f"({row['year_from']}) — {row['stock_hp']} hp  "
                             f"[{scraped_count} scraped]")

                    # Save incrementally every 20 rows
                    if scraped_count % 20 == 0:
                        _save_incremental(all_rows, output_path, already_scraped)
                        all_rows = []

                if limit and scraped_count >= limit:
                    log.info(f"Reached limit of {limit} cars — stopping.")
                    _save_incremental(all_rows, output_path, already_scraped)
                    return _load_csv(output_path)

    _save_incremental(all_rows, output_path, already_scraped)
    return _load_csv(output_path)


def _save_incremental(new_rows: list[dict], path: Path, already_scraped: set) -> None:
    if not new_rows:
        return
    df_new = pd.DataFrame(new_rows)
    if path.exists():
        df_existing = pd.read_csv(path)
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        df_combined.drop_duplicates(subset=["source_url"], keep="last", inplace=True)
    else:
        df_combined = df_new
    df_combined.to_csv(path, index=False)
    already_scraped.update(df_new["source_url"].dropna())
    log.info(f"Saved — {len(df_combined)} total rows in {path}")


def _load_csv(path: Path) -> pd.DataFrame:
    if path.exists():
        return pd.read_csv(path)
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# CLI for scraping a list of explicit URLs (original functionality)
# ---------------------------------------------------------------------------

def scrape_urls(urls: list[str]) -> pd.DataFrame:
    """Scrape a specific list of parkers /specs/ URLs. Good for quick testing."""
    rows = []
    for url in urls:
        row = scrape_spec_page(url)
        if row:
            rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        df.to_csv(OUTPUT_CSV, index=False)
        log.info(f"Saved {len(df)} rows → {OUTPUT_CSV}")
    return df


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape car specs from parkers.co.uk")
    parser.add_argument(
        "--makes", nargs="+", metavar="MAKE",
        help="Only scrape these makes (e.g. --makes ford volkswagen bmw)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after N successfully scraped cars"
    )
    parser.add_argument(
        "--urls", nargs="+", metavar="URL",
        help="Scrape specific /specs/ URLs instead of full crawl"
    )
    args = parser.parse_args()

    if args.urls:
        df = scrape_urls(args.urls)
    else:
        df = crawl(makes_filter=args.makes, limit=args.limit)

    print(f"\nDone. {len(df)} cars in {OUTPUT_CSV}")
    if not df.empty:
        print(df[["make", "model", "variant", "year_from", "stock_hp",
                   "stock_torque_nm", "weight_kg"]].head(10).to_string(index=False))
