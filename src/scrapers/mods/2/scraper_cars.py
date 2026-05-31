"""
scraper_cars.py — parkers.co.uk → cars_raw.csv

Використовує cloudscraper для обходу Cloudflare/403.
Встановити: pip install cloudscraper lxml beautifulsoup4 pandas

Запуск:
    python scraper_cars.py --makes volkswagen ford bmw honda toyota
    python scraper_cars.py --limit 100
    python scraper_cars.py --urls https://www.parkers.co.uk/.../specs/
"""

import re, time, random, logging, argparse, hashlib
from pathlib import Path
import pandas as pd
from bs4 import BeautifulSoup

try:
    import cloudscraper
    SCRAPER = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    log_msg = "Using cloudscraper (Cloudflare bypass)"
except ImportError:
    import requests
    SCRAPER = requests.Session()
    SCRAPER.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    log_msg = "cloudscraper not found, using requests (may get 403)"

OUTPUT_CSV = "cars_raw.csv"
BASE       = "https://www.parkers.co.uk"

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)
log.info(log_msg)


def _sleep(lo=1.5, hi=3.5):
    time.sleep(random.uniform(lo, hi))


def fetch(url: str, referer: str = BASE + "/") -> BeautifulSoup | None:
    SCRAPER.headers.update({"Referer": referer})
    for attempt in range(1, 4):
        try:
            log.info(f"GET {url}")
            r = SCRAPER.get(url, timeout=20)
            if r.status_code == 403:
                wait = 15 * attempt
                log.warning(f"403 — waiting {wait}s before retry {attempt+1}/3")
                time.sleep(wait)
                continue
            if r.status_code in (404, 410):
                return None
            r.raise_for_status()
            _sleep()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log.warning(f"Attempt {attempt}/3 failed: {e}")
            _sleep(4, 8)
    log.error(f"Giving up: {url}")
    return None


def _num(text, pat):
    if not text: return None
    m = re.search(pat, str(text).replace(",", ""))
    return float(m.group(1)) if m else None


# ── КРОК 1: Марки ─────────────────────────────────────────────────────────────
def get_makes() -> list[tuple[str, str]]:
    # Warm-up: відвідуємо головну щоб отримати cookies
    log.info("Warm-up: visiting homepage to get cookies...")
    try:
        SCRAPER.get(BASE + "/", timeout=20)
        _sleep(2, 4)
    except Exception as e:
        log.warning(f"Warm-up failed: {e}")

    soup = fetch(BASE + "/cars/")
    if not soup:
        return []

    BLOCKED_SLUGS = {
        "cars","vans","bikes","trucks","motorhomes","car-reviews","car-news",
        "advice","valuations","finance","insurance","electric-cars","used-cars",
        "new-cars","sitemap","contact","about","login","register","search",
        "privacy","terms","cookies","faq","help","subscription","newsletters",
    }

    makes, seen = [], set()
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = re.match(r"^/([a-z][a-z0-9-]{1,30})/?$", href)
        if not m:
            continue
        slug = m.group(1)
        if slug in BLOCKED_SLUGS:
            continue
        text = (a.get_text(strip=True) or slug.replace("-", " ").title())
        if not text or len(text) > 35:
            continue
        url = f"{BASE}/{slug}/"
        if url not in seen:
            seen.add(url)
            makes.append((text, url))

    log.info(f"Found {len(makes)} makes")
    return makes


# ── КРОК 2: Моделі марки ──────────────────────────────────────────────────────
def get_models(make_slug: str, make_url: str) -> list[tuple[str, str]]:
    soup = fetch(make_url, referer=BASE + "/cars/")
    if not soup:
        return []

    models, seen = [], set()
    pat = re.compile(rf"^/{re.escape(make_slug)}/([^/]+)/?$")
    for a in soup.select("a[href]"):
        m = pat.match(a.get("href", ""))
        if m:
            slug = m.group(1)
            url  = f"{BASE}/{make_slug}/{slug}/"
            if url not in seen:
                seen.add(url)
                name = a.get_text(strip=True) or slug.replace("-", " ").title()
                models.append((name, url))

    log.info(f"  → {make_slug}: {len(models)} models")
    return models


# ── КРОК 3: Всі /specs/ URL для моделі ───────────────────────────────────────
def get_spec_urls(make_slug: str, model_url: str) -> list[str]:
    soup = fetch(model_url, referer=model_url.rsplit("/", 2)[0] + "/")
    if not soup:
        return []

    found, seen = [], set()

    def _add(href):
        full = BASE + href if href.startswith("/") else href
        if full.endswith("/specs/") and make_slug in full and full not in seen:
            seen.add(full)
            found.append(full)

    for a in soup.select("a[href]"):
        _add(a.get("href", ""))

    # Якщо нічого — заходимо в покоління (один рівень глибше)
    if not found:
        gen_pat = re.compile(rf"^/{re.escape(make_slug)}/[^/]+/([^/]+)/?$")
        gen_urls = list({
            BASE + a["href"]
            for a in soup.select("a[href]")
            if gen_pat.match(a.get("href", ""))
        })[:12]
        for gurl in gen_urls:
            gs = fetch(gurl, referer=model_url)
            if gs:
                for a in gs.select("a[href]"):
                    _add(a.get("href", ""))

    return found


# ── КРОК 4: Парсинг /specs/ ───────────────────────────────────────────────────
def scrape_spec_page(url: str) -> dict | None:
    parts        = url.replace(BASE, "").strip("/").split("/")
    make_slug    = parts[0] if parts else ""
    model_slug   = parts[1] if len(parts) > 1 else ""
    gen_slug     = parts[2] if len(parts) > 2 else ""
    variant_slug = parts[3] if len(parts) > 3 else ""

    soup = fetch(url, referer=url.rsplit("/", 2)[0] + "/")
    if not soup:
        return None

    raw: dict[str, str] = {}
    for item in soup.select("li.specs-detail-table__item"):
        lbl = item.select_one(".specs-detail-table__item__label")
        val = item.select_one(".specs-detail-table__item__value")
        if lbl and val:
            raw[lbl.get_text(strip=True).lower()] = val.get_text(strip=True)

    if not raw:
        log.warning(f"No spec rows at {url}")
        return None

    h1    = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else url

    top_mph = _num(raw.get("top speed", ""),               r"(\d+(?:\.\d+)?)")
    zero60  = _num(raw.get("acceleration 0-60mph", ""),    r"(\d+(?:\.\d+)?)")

    yrs = sorted(set(int(y) for y in re.findall(r"\b(20\d{2}|19\d{2})\b", gen_slug + " " + title)))
    year_from = yrs[0]  if yrs          else None
    year_to   = yrs[-1] if len(yrs) > 1 else None

    fuel  = (raw.get("fuel type", "") or "").lower()
    tl    = title.lower()
    if   "electric" in fuel:                                        etype = "EV"
    elif "hybrid"   in fuel or "hybrid" in tl:                     etype = "Hybrid"
    elif any(w in tl for w in ["tdi","tdci","cdi","dci","diesel"]): etype = "Diesel"
    elif any(w in tl for w in ["tsi","tfsi","turbo","gti","sti",
                                "wrx","type r","st ","rs "]):       etype = "Turbocharged"
    elif fuel in ("petrol","gasoline"):                             etype = "Naturally Aspirated"
    else:                                                           etype = "Unknown"

    return {
        "car_id":             "CAR_" + hashlib.md5(url.encode()).hexdigest()[:8].upper(),
        "make":               make_slug.replace("-", " ").title(),
        "model":              model_slug.replace("-", " ").title(),
        "variant":            variant_slug.replace("-", " "),
        "raw_title":          title,
        "year_from":          year_from,
        "year_to":            year_to,
        "displacement_cc":    _num(raw.get("engine size",""),    r"(\d+)"),
        "cylinders":          _num(raw.get("cylinders",""),       r"(\d+)"),
        "stock_hp":           _num(raw.get("horsepower",""),      r"(\d+)"),
        "stock_torque_nm":    _num(raw.get("torque",""),          r"(\d+)\s*[Nn][Mm]"),
        "weight_kg":          _num(raw.get("weight",""),          r"(\d+)"),
        "drivetrain":         raw.get("drivetrain"),
        "transmission":       raw.get("transmission"),
        "engine_type":        etype,
        "fuel_type":          raw.get("fuel type"),
        "drag_coef":          _num(raw.get("drag coefficient",""),r"(\d+\.\d+)"),
        "frontal_area_m2":    None,
        "top_speed_stock":    round(top_mph * 1.60934, 1) if top_mph else None,
        "top_speed_mph":      top_mph,
        "zero_hundred_stock": zero60,
        "co2_gkm":            _num(raw.get("co2",""),             r"(\d+)"),
        "source_url":         url,
    }


# ── Кролер ────────────────────────────────────────────────────────────────────
def crawl(makes_filter=None, limit=None):
    out  = Path(OUTPUT_CSV)
    done: set[str] = set()
    if out.exists():
        try:
            done = set(pd.read_csv(out)["source_url"].dropna())
            log.info(f"Resume: {len(done)} already scraped")
        except Exception:
            pass

    makes = get_makes()
    if not makes:
        log.error("No makes found — site may still block us.")
        return pd.DataFrame()

    if makes_filter:
        fs    = {x.lower() for x in makes_filter}
        makes = [(n, u) for n, u in makes
                 if n.lower() in fs or u.rstrip("/").split("/")[-1] in fs]
        log.info(f"Filtered to: {[n for n,_ in makes]}")

    buf, total = [], 0
    for make_name, make_url in makes:
        slug = make_url.rstrip("/").split("/")[-1]
        for model_name, model_url in get_models(slug, make_url):
            for spec_url in get_spec_urls(slug, model_url):
                if spec_url in done:
                    continue
                row = scrape_spec_page(spec_url)
                if row:
                    buf.append(row); done.add(spec_url); total += 1
                    log.info(f"✓ [{total}] {row['make']} {row['model']} "
                             f"{row['variant']} — {row['stock_hp']} hp")
                    if len(buf) >= 20:
                        _flush(buf, out); buf = []
                if limit and total >= limit:
                    _flush(buf, out)
                    return pd.read_csv(out)
    _flush(buf, out)
    return pd.read_csv(out) if out.exists() else pd.DataFrame()


def _flush(rows, path):
    if not rows: return
    df_new = pd.DataFrame(rows)
    if path.exists():
        df = pd.concat([pd.read_csv(path), df_new], ignore_index=True)
        df.drop_duplicates(subset=["source_url"], keep="last", inplace=True)
    else:
        df = df_new
    df.to_csv(path, index=False)
    log.info(f"Saved — {len(df)} rows in {path}")


def scrape_urls(urls):
    rows = [r for u in urls if (r := scrape_spec_page(u))]
    df   = pd.DataFrame(rows)
    if not df.empty:
        df.to_csv(OUTPUT_CSV, index=False)
        log.info(f"Saved {len(df)} rows → {OUTPUT_CSV}")
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--makes",  nargs="+", help="e.g. volkswagen ford bmw")
    ap.add_argument("--limit",  type=int,  help="Stop after N cars")
    ap.add_argument("--urls",   nargs="+", help="Specific /specs/ URLs")
    args = ap.parse_args()

    df = scrape_urls(args.urls) if args.urls else crawl(args.makes, args.limit)
    print(f"\nDone. {len(df)} cars in {OUTPUT_CSV}")
    if not df.empty:
        print(df[["make","model","variant","year_from","stock_hp"]].head(10).to_string(index=False))
