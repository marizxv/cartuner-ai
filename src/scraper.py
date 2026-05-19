"""
Scrapers for stock specs and modification data.

Sources:
  - parkers.co.uk  →  cars table  (stock specs)
  - goapr.com      →  modifications table

Usage example (from repo root, venv active):
    python3 -c "
    from src.scraper import scrape_parkers
    import pprint
    pprint.pprint(scrape_parkers('https://www.parkers.co.uk/ford/focus/st-2012/20t-st-3-estate-(0115-)-5d/specs/'))
    "
"""

import re
import time
import logging
import requests
from bs4 import BeautifulSoup
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
DELAY = 2.0  # seconds between requests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch(url: str) -> BeautifulSoup:
    log.info(f"Fetching {url}")
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    time.sleep(DELAY)
    return BeautifulSoup(r.text, "lxml")


def _num(text: str, pattern: str) -> float | None:
    """Extract the first number matching `pattern` from `text`. Returns None if no match."""
    if not text:
        return None
    m = re.search(pattern, text.replace(",", ""))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# parkers.co.uk — stock car specs
# ---------------------------------------------------------------------------
#
# URL pattern:
#   https://www.parkers.co.uk/{make}/{model}/{generation}/{variant}/specs/
#
# Example:
#   https://www.parkers.co.uk/ford/focus/st-2012/20t-st-3-estate-(0115-)-5d/specs/
#   https://www.parkers.co.uk/volkswagen/golf/mk7-2012/2-0-tsi-gti-performance-5d/specs/
#   https://www.parkers.co.uk/honda/civic-type-r/2015/2-0-ivtec-type-r-gt-5d/specs/
#
# Finding URLs: go to parkers.co.uk, search for the car, click Specs.
# ---------------------------------------------------------------------------

def scrape_parkers(url: str) -> dict:
    """
    Scrape one car's stock specs from a parkers.co.uk /specs/ page.

    Returns a flat dict with the columns that go into cars_raw.csv.
    Units: hp in bhp (≈ same as hp), speed in km/h, weight in kg, engine in cc.

    Parkers gives 0-60 mph which is essentially 0-100 km/h (100 km/h = 62.1 mph,
    difference is ~0.1-0.2 sec). Stored as zero_to_100 with a note.
    """
    soup = _fetch(url)

    # Every spec is in:  <li class="specs-detail-table__item">
    #   <span class="specs-detail-table__item__label">Label</span>
    #   <span class="specs-detail-table__item__value">Value</span>
    # Some specs appear twice (once in summary, once in detail).
    # We keep the LAST occurrence — the detail section is more complete.
    raw: dict[str, str] = {}
    for item in soup.select("li.specs-detail-table__item"):
        label = item.select_one(".specs-detail-table__item__label")
        value = item.select_one(".specs-detail-table__item__value")
        if label and value:
            raw[label.get_text(strip=True).lower()] = value.get_text(strip=True)

    if not raw:
        log.warning(f"No specs found on {url} — parkers may have changed their HTML.")
        return {"source_url": url}

    # Page title
    h1 = soup.find("h1")
    raw_title = h1.get_text(strip=True) if h1 else url

    top_speed_mph = _num(raw.get("top speed", ""), r"(\d+)")
    zero_60       = _num(raw.get("acceleration 0-60mph", ""), r"(\d+\.?\d*)")

    return {
        "source_url":         url,
        "raw_title":          raw_title,
        "hp_stock":           _num(raw.get("horsepower", ""), r"(\d+)"),    # bhp ≈ hp
        "torque_nm_stock":    _num(raw.get("torque", ""), r"(\d+)\s*[Nn][Mm]"),
        "weight_kg":          _num(raw.get("weight", ""), r"(\d+)"),
        "zero_to_100":        zero_60,   # 0-60 mph ≈ 0-100 km/h, ~0.1s difference
        "top_speed_kph":      round(top_speed_mph * 1.60934) if top_speed_mph else None,
        "top_speed_mph":      top_speed_mph,
        "engine_cc":          _num(raw.get("engine size", ""), r"(\d+)"),
        "drivetrain":         raw.get("drivetrain"),
        "transmission":       raw.get("transmission"),
        "fuel_type":          raw.get("fuel type"),
        "cylinders":          _num(raw.get("cylinders", ""), r"(\d+)"),
        "co2_gkm":            _num(raw.get("co2", ""), r"(\d+)"),
        "_raw": raw,   # full dump — useful for debugging or adding more columns later
    }


def scrape_parkers_batch(urls: list[str]) -> pd.DataFrame:
    """Scrape multiple parkers pages. Returns a DataFrame, one row per car."""
    rows = []
    for url in urls:
        try:
            rows.append(scrape_parkers(url))
        except Exception as e:
            log.error(f"Failed {url}: {e}")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# goapr.com — modification specs
# ---------------------------------------------------------------------------
#
# APR publish before/after hp and torque tables for each ECU tune product.
# Each page covers one turbo+car combination (IS20, IS38, EFR7163, etc.)
# Figures are wheel horsepower (whp) measured on their in-house dyno.
#
# Example pages:
#   https://www.goapr.com/products/ecu_upgrade_2-0t_gen3_mqb_is20.html
#   https://www.goapr.com/products/ecu_upgrade_2-0t_gen3_mqb_is38.html
# ---------------------------------------------------------------------------

def scrape_goapr(url: str) -> list[dict]:
    """
    Scrape power figures from one APR product page.

    APR pages have HTML tables with columns like:
      Vehicle | Stock HP | Stock TQ | Stage 1 HP | Stage 1 TQ | ...
    Column names vary per page — all raw columns are kept so you can inspect them.

    Returns a list of dicts (one per vehicle row in the table).
    hp_type is always 'whp' — APR publish wheel figures.
    """
    soup = _fetch(url)
    results = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        # Only care about tables that have power/torque data
        if not any(kw in " ".join(headers) for kw in ("hp", "tq", "horsepower", "torque", "whp")):
            continue
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2 or not cells[0]:
                continue
            row = dict(zip(headers, cells))
            row["source_url"] = url
            row["hp_type"] = "whp"
            results.append(row)

    if not results:
        log.warning(f"No power tables found at {url}")
    return results


def scrape_goapr_batch(urls: list[str]) -> pd.DataFrame:
    rows = []
    for url in urls:
        try:
            rows.extend(scrape_goapr(url))
        except Exception as e:
            log.error(f"Failed {url}: {e}")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def save_csv(df: pd.DataFrame, path: str) -> None:
    """Save a DataFrame to CSV (overwrites if exists)."""
    df.to_csv(path, index=False)
    log.info(f"Saved {len(df)} rows → {path}")


def append_csv(new_rows: list[dict], path: str) -> pd.DataFrame:
    """
    Add rows to a CSV file. Creates the file if it doesn't exist yet.
    Use this for builds where you're adding rows one session at a time.
    """
    try:
        existing = pd.read_csv(path)
        combined = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
    except FileNotFoundError:
        combined = pd.DataFrame(new_rows)
    combined.to_csv(path, index=False)
    log.info(f"CSV now has {len(combined)} rows → {path}")
    return combined
