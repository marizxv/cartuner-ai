"""
Scrapers for stock-specs and modification data.

Usage (from repo root, with venv active):
    python -c "from src.scraper import scrape_ultimatespecs_car; print(scrape_ultimatespecs_car('https://www.ultimatespecs.com/car-specs/Volkswagen/65173/'))"
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
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}
DELAY = 2.5  # seconds between requests — be polite


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_soup(url: str) -> BeautifulSoup:
    log.info(f"GET {url}")
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    time.sleep(DELAY)
    return BeautifulSoup(resp.text, "lxml")


def _extract_float(text: str, pattern: str) -> float | None:
    """Pull the first regex match out of a string and cast to float."""
    if not text:
        return None
    m = re.search(pattern, text.replace(",", "").replace(" ", ""))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# ultimatespecs.com — stock car specs
# ---------------------------------------------------------------------------

def scrape_ultimatespecs_car(url: str) -> dict:
    """
    Scrape one car's stock specs from ultimatespecs.com.

    ultimatespecs stores each spec in a <li> (or <div>) with two child elements:
    the label and the value.  The exact selectors can vary between page
    versions — the function tries two known layouts and falls back to a
    text-based scan if neither matches.

    Returns a flat dict ready to be inserted as a row in the `cars` table.
    Includes a '_raw' key with every key→value pair found (useful for
    debugging / extending the parser).
    """
    soup = _get_soup(url)
    raw: dict[str, str] = {}

    # Layout A: <ul class="list-unstyled"> rows with two <li> children
    for row in soup.select("ul.list-unstyled > li"):
        children = [c for c in row.children if c.name]
        if len(children) == 2:
            key = children[0].get_text(strip=True).lower().rstrip(":")
            val = children[1].get_text(strip=True)
            raw[key] = val

    # Layout B: table rows  <tr><th>label</th><td>value</td></tr>
    if not raw:
        for row in soup.select("tr"):
            th = row.find("th")
            td = row.find("td")
            if th and td:
                key = th.get_text(strip=True).lower().rstrip(":")
                raw[key] = td.get_text(strip=True)

    # Layout C: .title / .value spans inside .rowOdd / .rowEven divs
    if not raw:
        for row in soup.select("div.rowOdd, div.rowEven"):
            title = row.select_one(".title, .specLabel")
            value = row.select_one(".value, .specValue")
            if title and value:
                raw[title.get_text(strip=True).lower().rstrip(":")] = value.get_text(strip=True)

    if not raw:
        log.warning("No spec rows found — the page structure may have changed.")
        log.warning("Save the page HTML and inspect it to update the selectors.")

    # Page title for a human-readable label
    h1 = soup.find("h1")
    raw_title = h1.get_text(strip=True) if h1 else url

    # Map raw keys to schema columns
    # Keys vary slightly across page versions, so we check several aliases
    def get(*keys: str) -> str:
        for k in keys:
            for raw_key in raw:
                if k in raw_key:
                    return raw[raw_key]
        return ""

    return {
        "source_url": url,
        "raw_title": raw_title,
        "hp_stock":           _extract_float(get("max power", "power"), r"(\d+)\s*[Hh][Pp]"),
        "torque_nm_stock":    _extract_float(get("max torque", "torque"), r"(\d+)\s*[Nn][Mm]"),
        "weight_kg":          _extract_float(get("kerb weight", "curb weight", "weight"), r"(\d+)"),
        "zero_to_100_stock":  _extract_float(get("0 - 100", "0-100", "0 to 100"), r"(\d+\.?\d*)"),
        "top_speed_stock":    _extract_float(get("top speed", "max speed"), r"(\d+)"),
        "drag_coefficient":   _extract_float(get("drag", "cd"), r"(0\.\d+)"),
        "drivetrain":         get("drive", "drivetrain") or None,
        "engine_cc":          _extract_float(get("displacement", "engine size", "cubic"), r"(\d{3,4})"),
        "_raw": raw,
    }


def scrape_ultimatespecs_batch(urls: list[str]) -> pd.DataFrame:
    """Scrape multiple ultimatespecs pages and return as a DataFrame."""
    rows = []
    for url in urls:
        try:
            rows.append(scrape_ultimatespecs_car(url))
        except Exception as e:
            log.error(f"Failed {url}: {e}")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# goapr.com — modification specs
# ---------------------------------------------------------------------------

def scrape_goapr_product(url: str) -> list[dict]:
    """
    Scrape power figures from an APR product page.

    APR pages typically contain one or more HTML tables with columns like:
    Vehicle | Stock HP | Stock TQ | Stage HP | Stage TQ
    (exact column names vary per product).

    Returns a list of dicts — one per data row found across all matching
    tables on the page.  Each dict includes the source URL and the full
    raw row so you can decide which columns map to what.
    """
    soup = _get_soup(url)
    results: list[dict] = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        # Only process tables that look like power/torque tables
        if not any(kw in " ".join(headers) for kw in ("hp", "tq", "horsepower", "torque", "whp")):
            continue

        for tr in table.find_all("tr")[1:]:  # skip header row
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2 or not cells[0]:
                continue
            row = dict(zip(headers, cells))
            row["source_url"] = url
            results.append(row)
            log.info(f"  APR row: {row}")

    if not results:
        log.warning("No power tables found on APR page — check URL or page structure.")

    return results


def scrape_goapr_batch(urls: list[str]) -> pd.DataFrame:
    """Scrape multiple APR pages and return as a DataFrame."""
    rows = []
    for url in urls:
        try:
            rows.extend(scrape_goapr_product(url))
        except Exception as e:
            log.error(f"Failed {url}: {e}")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Save helpers
# ---------------------------------------------------------------------------

def save_csv(df: pd.DataFrame, path: str) -> None:
    df.to_csv(path, index=False)
    log.info(f"Saved {len(df)} rows → {path}")


def append_csv(new_rows: list[dict], path: str) -> pd.DataFrame:
    """Append rows to an existing CSV (or create it if missing)."""
    try:
        existing = pd.read_csv(path)
        combined = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
    except FileNotFoundError:
        combined = pd.DataFrame(new_rows)
    combined.to_csv(path, index=False)
    log.info(f"CSV now has {len(combined)} rows → {path}")
    return combined
