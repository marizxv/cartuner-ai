"""
scraper_cars.py — parkers.co.uk → cars_raw.csv

Real URL hierarchy on parkers.co.uk:
  /make/                              e.g. /bmw/
  /make/model/                        e.g. /bmw/x5/
  /make/model/generation/             e.g. /bmw/x5/m/           ← no year, links to year pages
  /make/model/generation-YEAR/specs/  e.g. /bmw/x5/m-2019/specs/   ← TYPE A (list of variants)
  /make/model/generation-YEAR/variant/specs/                         ← TYPE B (single variant)

Every scraped row is written to CSV immediately — safe to Ctrl+C at any time.

Install:  pip install cloudscraper lxml beautifulsoup4 pandas
Run:
    python scraper_cars.py --makes bmw volkswagen ford
    python scraper_cars.py --limit 50
    python scraper_cars.py --urls https://www.parkers.co.uk/bmw/x5/m-2019/xdrive-x5-m-competition-5dr-step-auto/specs/
    python scraper_cars.py --list-makes
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

# ── Known real car makes on parkers.co.uk ─────────────────────────────────────
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

# Slugs that cannot be a model name
_NOT_A_MODEL: frozenset[str] = frozenset({
    "specs", "used-prices", "car-leasing", "for-sale", "nearly-new",
    "owners-reviews", "owner-reviews", "reviews", "road-tests", "news",
    "advice", "compare", "leasing", "electric", "hybrid", "used", "new",
    "finance", "insurance", "shortlist", "valuations", "buying-guide",
    "cars-for-sale", "new-cars", "on-sale-soon",
})

# Slugs that cannot be a generation name
_NOT_A_GEN: frozenset[str] = frozenset({
    "specs", "used-prices", "car-leasing", "for-sale", "nearly-new",
    "owners-reviews", "owner-reviews", "reviews", "road-tests", "news",
    "advice", "compare", "leasing", "finance", "insurance", "buying-guide",
})

# Words that indicate a slug is a navigation/section page, not a make
_MAKE_BLOCKLIST_WORDS: frozenset[str] = frozenset({
    "car", "sale", "award", "review", "test", "valuation", "finance",
    "insurance", "advice", "long", "owner", "electric", "used",
    "van", "bike", "truck", "motorhome", "sell", "short", "lease",
    "price", "buy", "news", "guide", "search", "compare",
})


# ── Helpers ───────────────────────────────────────────────────────────────────
def _sleep(lo: float = 1.5, hi: float = 3.5) -> None:
    time.sleep(random.uniform(lo, hi))


def fetch(url: str, referer: str = BASE + "/") -> BeautifulSoup | None:
    """Fetch a URL, return BeautifulSoup on success, None on permanent failure."""
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


def _links_matching(soup: BeautifulSoup, pattern: re.Pattern) -> list[str]:
    """Return all unique hrefs from <a> tags matching the given compiled pattern."""
    seen: set[str] = set()
    result: list[str] = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if pattern.match(href) and href not in seen:
            seen.add(href)
            result.append(href)
    return result


# ── STEP 1: Resolve makes ──────────────────────────────────────────────────────
def get_makes(makes_filter: list[str] | None = None) -> list[tuple[str, str]]:
    """
    Returns [(display_name, make_url), ...].
    Uses KNOWN_MAKES as the primary source, then discovers extras from /cars/.
    Applies makes_filter if provided.
    """
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
            # Reject slugs that contain any navigation/section word
            if any(w in slug.split("-") for w in _MAKE_BLOCKLIST_WORDS):
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
    Accepts only hrefs of the form /make/model/ that are not in _NOT_A_MODEL.
    """
    soup = fetch(make_url, referer=BASE + "/cars/")
    if not soup:
        return []

    pat = re.compile(rf"^/{re.escape(make_slug)}/([a-z0-9][a-z0-9-]{{1,60}})/$")
    models: list[tuple[str, str]] = []
    seen: set[str] = set()

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = pat.match(href)
        if not m:
            continue
        model_slug = m.group(1)
        if model_slug in _NOT_A_MODEL:
            continue
        url = BASE + href
        if url not in seen:
            seen.add(url)
            name = a.get_text(strip=True) or model_slug.replace("-", " ").title()
            models.append((name, url))

    log.info(f"  {make_slug}: {len(models)} model(s)")
    return models


# ── STEP 3: Spec URLs for one model ───────────────────────────────────────────
#
# Observed hierarchy (from user-provided real URLs):
#
#   /bmw/x5/                       ← model page
#   /bmw/x5/m/                     ← generation page  (slug without year)
#   /bmw/x5/m/review/              ← review sub-page of generation (contains year links)
#   /bmw/x5/m-2019/specs/          ← TYPE A: generation-year spec listing
#   /bmw/x5/m-2019/<variant>/specs/← TYPE B: single-variant spec page
#
# Strategy:
#   1. Collect generation slugs from model page  (/make/model/gen/)
#   2. For each generation page, also visit its /review/ sub-page
#      (it often links to year-specific pages like /make/model/gen-YEAR/)
#   3. Collect generation-year pages  (/make/model/gen-YEAR/)
#   4. From each gen-year page grab:
#        TYPE A: /make/model/gen-YEAR/specs/
#        TYPE B: /make/model/gen-YEAR/<variant>/specs/
# ─────────────────────────────────────────────────────────────────────────────

def get_spec_urls(make_slug: str, model_slug: str, model_url: str) -> list[str]:
    """
    Walk the model → generation → generation-year → specs hierarchy and
    return all unique spec URLs for this model.
    """
    ms  = re.escape(make_slug)
    mds = re.escape(model_slug)

    # Matches /make/model/gen/  (depth-3 generation page, no year in slug)
    gen_re = re.compile(rf"^/{ms}/{mds}/([a-z0-9][a-z0-9-]*)/$")

    # Matches /make/model/gen-YEAR/  or  /make/model/gen/  (any depth-3)
    gen_year_re = re.compile(rf"^/{ms}/{mds}/([a-z0-9][a-z0-9-]*(?:-\d{{4}})[a-z0-9-]*)/$")

    # TYPE A: /make/model/gen-YEAR/specs/
    type_a_re = re.compile(rf"^/{ms}/{mds}/[a-z0-9][a-z0-9-]*(?:-\d{{4}})[a-z0-9-]*/specs/$")

    # TYPE B: /make/model/gen-YEAR/<variant>/specs/
    type_b_re = re.compile(rf"^/{ms}/{mds}/[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-\[\]()]*[/]specs/$")

    found:     list[str] = []
    seen_spec: set[str]  = set()

    def _add_spec(href: str) -> None:
        full = BASE + href if href.startswith("/") else href
        if full not in seen_spec:
            seen_spec.add(full)
            found.append(full)

    def _harvest(soup: BeautifulSoup) -> None:
        """Grab TYPE A and TYPE B spec links from any page."""
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if type_a_re.match(href) or type_b_re.match(href):
                _add_spec(href)

    # ── Level 1: model page ────────────────────────────────────────────────
    model_soup = fetch(model_url, referer=f"{BASE}/{make_slug}/")
    if not model_soup:
        log.info(f"    {make_slug}/{model_slug}: 0 spec page(s) (model page failed)")
        return []

    _harvest(model_soup)

    # Collect depth-3 generation slugs
    gen_hrefs = _links_matching(model_soup, gen_re)
    gen_hrefs = [h for h in gen_hrefs if gen_re.match(h).group(1) not in _NOT_A_GEN]

    # ── Level 2: generation pages + their /review/ sub-pages ──────────────
    gen_year_hrefs: list[str] = []   # depth-3 pages WITH a year in the slug
    seen_gy: set[str] = set()

    def _collect_gen_year(soup: BeautifulSoup, referer: str) -> None:
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if gen_year_re.match(href) and href not in seen_gy:
                seen_gy.add(href)
                gen_year_hrefs.append(href)

    for gen_href in gen_hrefs:
        gen_url  = BASE + gen_href
        gen_slug = gen_re.match(gen_href).group(1)
        gen_soup = fetch(gen_url, referer=model_url)
        if not gen_soup:
            continue

        _harvest(gen_soup)
        _collect_gen_year(gen_soup, gen_url)

        # Also visit /review/ sub-page — it often links to year pages
        review_url = gen_url.rstrip("/") + "/review/"
        review_soup = fetch(review_url, referer=gen_url)
        if review_soup:
            _harvest(review_soup)
            _collect_gen_year(review_soup, review_url)

    # ── Level 3: generation-year pages ────────────────────────────────────
    for gy_href in gen_year_hrefs:
        gy_url  = BASE + gy_href
        gy_soup = fetch(gy_url, referer=model_url)
        if not gy_soup:
            continue
        _harvest(gy_soup)

        # Some sites nest variants one level deeper still
        # /make/model/gen-YEAR/variant/  → then /specs/ inside
        var_re_local = re.compile(
            rf"^/{ms}/{mds}/[a-z0-9][a-z0-9-]*/([a-z0-9][a-z0-9-\[\]()]*)/$"
        )
        for a in gy_soup.select("a[href]"):
            href = a.get("href", "")
            if var_re_local.match(href) and not href.endswith("/specs/"):
                var_soup = fetch(BASE + href, referer=gy_url)
                if var_soup:
                    _harvest(var_soup)

    log.info(f"    {make_slug}/{model_slug}: {len(found)} spec page(s)")
    return found


# ── STEP 4: Parse a /specs/ page ──────────────────────────────────────────────
def scrape_spec_page(url: str) -> dict | None:
    """Fetch a spec URL and return a data dict, or None if no specs found."""
    parts = url.replace(BASE, "").strip("/").split("/")
    # parts: [make, model, gen-year, (variant,) "specs"]
    make_slug    = parts[0] if len(parts) > 0 else ""
    model_slug   = parts[1] if len(parts) > 1 else ""
    gen_slug     = parts[2] if len(parts) > 2 else ""
    # variant is either parts[3] (5-part URL) or empty (4-part URL)
    variant_slug = parts[3] if len(parts) > 4 else ""

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
    """Append one row to CSV immediately. Creates header if file is new."""
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k) for k in _CSV_FIELDS})


def _load_done_urls(path: Path) -> set[str]:
    """Return set of already-scraped source_urls for resume support."""
    if not path.exists():
        return set()
    try:
        df   = pd.read_csv(path, usecols=["source_url"])
        urls = set(df["source_url"].dropna())
        log.info(f"Resuming — {len(urls)} URL(s) already saved in {path}")
        return urls
    except Exception as exc:
        log.warning(f"Could not read existing CSV for resume: {exc}")
        return set()


# ── Main crawler ───────────────────────────────────────────────────────────────
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
    """Scrape a list of explicit /specs/ URLs, appending to the CSV."""
    out   = Path(OUTPUT_CSV)
    done  = _load_done_urls(out)
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
                    help="Make slug(s), e.g.: bmw volkswagen ford")
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
