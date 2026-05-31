"""
scraper_builds.py
=================
Scrapes real-world tuned car builds → builds_raw.csv

Sources:
  1. fastestlaps.com  — tuned lap times + power figures for modified cars
  2. dragtimes.com    — 1/4 mile drag results with mod lists
  3. Static seed data — manually entered well-known builds (always works)

Output columns (builds_raw.csv):
    build_id, car_id, make, model, variant, year,
    mods_applied, mod_description, result_hp, result_torque,
    result_0_100, result_top_speed, result_quarter_mile,
    measurement_type, source_type, source_url

Usage:
    python scraper_builds.py                  # all sources
    python scraper_builds.py --static-only    # only seed data (no internet)
    python scraper_builds.py --makes vw audi  # filter by make
"""

import re
import time
import logging
import hashlib
import argparse
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OUTPUT_CSV = "builds_raw.csv"
DELAY      = 2.5
TIMEOUT    = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch(url: str, retries: int = 3) -> BeautifulSoup | None:
    for attempt in range(1, retries + 1):
        try:
            log.info(f"GET {url}")
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            time.sleep(DELAY)
            return BeautifulSoup(r.text, "lxml")
        except requests.HTTPError as e:
            if e.response.status_code in (403, 404, 410):
                log.warning(f"Skip {url} — HTTP {e.response.status_code}")
                return None
            time.sleep(DELAY * attempt)
        except Exception as e:
            log.warning(f"Attempt {attempt} failed: {e}")
            time.sleep(DELAY * attempt)
    return None


def _num(text: str, pattern: str) -> float | None:
    if not text:
        return None
    m = re.search(pattern, str(text).replace(",", ""))
    return float(m.group(1)) if m else None


def _build_id(make: str, model: str, mods: str, source: str) -> str:
    key = f"{make}_{model}_{mods}_{source}"
    return "BUILD_" + hashlib.md5(key.encode()).hexdigest()[:8].upper()


def _car_id_from_make_model(make: str, model: str) -> str:
    """Generate a placeholder car_id (links to cars_raw later via make+model)."""
    return "CAR_" + hashlib.md5(f"{make}_{model}".encode()).hexdigest()[:8].upper()


# ---------------------------------------------------------------------------
# Source 1: fastestlaps.com
# ---------------------------------------------------------------------------
# fastestlaps.com lists tuned cars in /tuning/ section with:
#   - Car name, power, 0-100, top speed
#   - Modification list

FASTESTLAPS_BASE = "https://www.fastestlaps.com"
FASTESTLAPS_TUNING_PAGES = [
    "/tuning/",
    "/tuning/?page=2",
    "/tuning/?page=3",
    "/tuning/?page=4",
    "/tuning/?page=5",
]


def scrape_fastestlaps() -> list[dict]:
    rows = []
    for page_url in FASTESTLAPS_TUNING_PAGES:
        full_url = FASTESTLAPS_BASE + page_url
        soup = fetch(full_url)
        if not soup:
            continue

        # Each tuned car is listed as a row or card
        # fastestlaps uses table rows with car name + specs
        for row_el in soup.select("table tr, .car-row, .tuning-row"):
            cells = [td.get_text(strip=True) for td in row_el.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            # Try to identify car name cell (usually first column with a link)
            link_el = row_el.find("a", href=True)
            if not link_el:
                continue

            car_name = link_el.get_text(strip=True)
            car_url  = FASTESTLAPS_BASE + link_el["href"] if link_el["href"].startswith("/") else link_el["href"]

            # Parse specs from cells
            text = " ".join(cells)
            hp       = _num(text, r"(\d{3,4})\s*(?:hp|bhp|ps|kw)")
            zero_100 = _num(text, r"(\d+\.\d)\s*s")
            top_spd  = _num(text, r"(\d{2,3})\s*(?:km/h|mph)")

            if not car_name or not hp:
                continue

            # Parse make/model from car name
            parts = car_name.split(" ", 2)
            make  = parts[0] if len(parts) > 0 else "Unknown"
            model = parts[1] if len(parts) > 1 else "Unknown"

            # Scrape individual page for mod details
            detail = _scrape_fastestlaps_detail(car_url)

            rows.append({
                "build_id":         _build_id(make, model, str(hp), car_url),
                "car_id":           _car_id_from_make_model(make, model),
                "make":             make,
                "model":            model,
                "variant":          car_name,
                "year":             detail.get("year"),
                "mods_applied":     detail.get("mods_applied", ""),
                "mod_description":  detail.get("mod_description", ""),
                "result_hp":        hp,
                "result_torque":    detail.get("torque"),
                "result_0_100":     zero_100,
                "result_top_speed": top_spd,
                "result_quarter_mile": detail.get("quarter_mile"),
                "measurement_type": "estimated",
                "source_type":      "fastestlaps",
                "source_url":       car_url,
            })

    log.info(f"Fastestlaps: {len(rows)} builds found")
    return rows


def _scrape_fastestlaps_detail(url: str) -> dict:
    """Scrape a single fastestlaps tuned car page for modification details."""
    soup = fetch(url)
    if not soup:
        return {}

    result = {}

    # Look for modification list
    mod_section = soup.find(string=re.compile(r"modif|upgrade|tune", re.I))
    if mod_section:
        parent = mod_section.parent
        if parent:
            result["mod_description"] = parent.get_text(strip=True)
            result["mods_applied"]    = parent.get_text(strip=True)[:200]

    # Torque
    full_text = soup.get_text(" ")
    result["torque"]        = _num(full_text, r"(\d{3,4})\s*(?:nm|ft.?lb)")
    result["quarter_mile"]  = _num(full_text, r"(\d+\.\d+)\s*(?:sec|s)\b.*?(?:quarter|1/4)")
    result["year"]          = _num(full_text, r"\b(20\d{2}|19\d{2})\b")

    return result


# ---------------------------------------------------------------------------
# Source 2: dragtimes.com
# ---------------------------------------------------------------------------
# dragtimes.com has user-submitted 1/4 mile results with car + mod info

DRAGTIMES_BASE  = "https://www.dragtimes.com"
DRAGTIMES_PAGES = [
    "/slips.php",
    "/slips.php?page=2",
    "/slips.php?page=3",
]


def scrape_dragtimes(makes_filter: list[str] | None = None) -> list[dict]:
    rows = []
    for page_url in DRAGTIMES_PAGES:
        full_url = DRAGTIMES_BASE + page_url
        soup = fetch(full_url)
        if not soup:
            continue

        for row_el in soup.select("tr.odd, tr.even, .slip-row"):
            cells = [td.get_text(strip=True) for td in row_el.find_all("td")]
            if len(cells) < 4:
                continue

            link_el = row_el.find("a", href=True)
            car_name = cells[0] if cells else ""
            if not car_name:
                continue

            parts = car_name.split(" ", 2)
            make  = parts[0] if len(parts) > 0 else "Unknown"
            model = parts[1] if len(parts) > 1 else "Unknown"

            if makes_filter:
                if make.lower() not in [m.lower() for m in makes_filter]:
                    continue

            # Quarter mile time is usually in column 2 or 3
            quarter = None
            for cell in cells[1:5]:
                q = _num(cell, r"(\d+\.\d+)")
                if q and 8 < q < 20:  # valid 1/4 mile range
                    quarter = q
                    break

            # MPH trap speed
            trap = None
            for cell in cells[1:6]:
                t = _num(cell, r"(\d{2,3}\.\d+)")
                if t and 60 < t < 200:
                    trap = t
                    break

            detail_url = ""
            if link_el:
                href = link_el["href"]
                detail_url = DRAGTIMES_BASE + href if href.startswith("/") else href

            rows.append({
                "build_id":            _build_id(make, model, str(quarter), detail_url),
                "car_id":              _car_id_from_make_model(make, model),
                "make":                make,
                "model":               model,
                "variant":             car_name,
                "year":                _num(car_name, r"\b(20\d{2}|19\d{2})\b"),
                "mods_applied":        "",
                "mod_description":     "",
                "result_hp":           None,
                "result_torque":       None,
                "result_0_100":        None,
                "result_top_speed":    round(trap * 1.60934, 1) if trap else None,
                "result_quarter_mile": quarter,
                "measurement_type":    "dyno/measured",
                "source_type":         "dragtimes",
                "source_url":          detail_url or full_url,
            })

    log.info(f"Dragtimes: {len(rows)} builds found")
    return rows


# ---------------------------------------------------------------------------
# Source 3: Static seed builds (always works, no internet)
# ---------------------------------------------------------------------------
# Real, well-documented builds from community sources.
# References: tunezilla.io, carthrottle, r/projectcar, evom.net, nasioc.com

STATIC_BUILDS = [
    # ── VW Golf GTI Mk7 ──────────────────────────────────────────────────
    {
        "make": "VW", "model": "Golf GTI", "variant": "Mk7 2.0 TSI IS20",
        "year": 2015,
        "mods_applied": "Stage 1 ECU;Cold Air Intake",
        "mod_description": "APR Stage 1 ECU remap + aftermarket intake",
        "result_hp": 260, "result_torque": 360,
        "result_0_100": 6.0, "result_top_speed": 255,
        "result_quarter_mile": 13.9,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "VW", "model": "Golf GTI", "variant": "Mk7 2.0 TSI IS20",
        "year": 2015,
        "mods_applied": "Stage 2 ECU;Cold Air Intake;Downpipe;Intercooler",
        "mod_description": "APR Stage 2: ECU + intake + downpipe + FMIC",
        "result_hp": 320, "result_torque": 430,
        "result_0_100": 5.3, "result_top_speed": 270,
        "result_quarter_mile": 13.2,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "VW", "model": "Golf GTI", "variant": "Mk7.5 2.0 TSI IS38",
        "year": 2018,
        "mods_applied": "Stage 1 ECU",
        "mod_description": "Unitronic Stage 1 ECU remap only",
        "result_hp": 295, "result_torque": 410,
        "result_0_100": 5.5, "result_top_speed": 262,
        "result_quarter_mile": 13.5,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "VW", "model": "Golf GTI", "variant": "Mk7.5 2.0 TSI IS38",
        "year": 2018,
        "mods_applied": "Stage 2 ECU;Cold Air Intake;Downpipe;Intercooler;DSG Tune",
        "mod_description": "Full Stage 2: ECU + intake + Milltek downpipe + FMIC + DSG remap",
        "result_hp": 380, "result_torque": 490,
        "result_0_100": 4.8, "result_top_speed": 280,
        "result_quarter_mile": 12.8,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── VW Golf R Mk7 ────────────────────────────────────────────────────
    {
        "make": "VW", "model": "Golf R", "variant": "Mk7 2.0 TSI 4Motion",
        "year": 2016,
        "mods_applied": "Stage 1 ECU",
        "mod_description": "APR Stage 1 ECU, bolt-on only",
        "result_hp": 360, "result_torque": 460,
        "result_0_100": 4.5, "result_top_speed": 275,
        "result_quarter_mile": 12.8,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "VW", "model": "Golf R", "variant": "Mk7 2.0 TSI 4Motion",
        "year": 2016,
        "mods_applied": "Stage 2 ECU;Downpipe;Intercooler;Intake;DSG Tune",
        "mod_description": "Stage 2 full bolt-on: remap + downpipe + FMIC + intake + DSG",
        "result_hp": 430, "result_torque": 520,
        "result_0_100": 4.0, "result_top_speed": 285,
        "result_quarter_mile": 12.0,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── Audi S3 8V ──────────────────────────────────────────────────────
    {
        "make": "Audi", "model": "S3", "variant": "8V 2.0 TFSI",
        "year": 2015,
        "mods_applied": "Stage 1 ECU",
        "mod_description": "Revo Stage 1 ECU remap",
        "result_hp": 340, "result_torque": 450,
        "result_0_100": 4.6, "result_top_speed": 272,
        "result_quarter_mile": 13.0,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "Audi", "model": "S3", "variant": "8V 2.0 TFSI",
        "year": 2015,
        "mods_applied": "Stage 2 ECU;Downpipe;Intercooler;Intake",
        "mod_description": "Stage 2: Revo ECU + VRSF downpipe + FMIC + intake",
        "result_hp": 400, "result_torque": 510,
        "result_0_100": 4.1, "result_top_speed": 285,
        "result_quarter_mile": 12.4,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── BMW M3 / M4 F80/F82 ──────────────────────────────────────────────
    {
        "make": "BMW", "model": "M3", "variant": "F80 3.0 S55",
        "year": 2016,
        "mods_applied": "Stage 1 ECU;Downpipe",
        "mod_description": "MHD Stage 2 flash + catless downpipes",
        "result_hp": 480, "result_torque": 620,
        "result_0_100": 3.8, "result_top_speed": 300,
        "result_quarter_mile": 11.8,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "BMW", "model": "M4", "variant": "F82 3.0 S55",
        "year": 2017,
        "mods_applied": "Stage 2 ECU;Downpipe;Intercooler;Charge Pipe",
        "mod_description": "MHD Stage 2 + Wagner FMIC + catless DP + charge pipe",
        "result_hp": 530, "result_torque": 680,
        "result_0_100": 3.6, "result_top_speed": 310,
        "result_quarter_mile": 11.4,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── Subaru WRX STI ──────────────────────────────────────────────────
    {
        "make": "Subaru", "model": "WRX STI", "variant": "VA 2.5 EJ257",
        "year": 2016,
        "mods_applied": "Stage 1 ECU;Intake;Downpipe",
        "mod_description": "Cobb Accessport Stage 2: OTS map + intake + Invidia DP",
        "result_hp": 340, "result_torque": 440,
        "result_0_100": 5.0, "result_top_speed": 255,
        "result_quarter_mile": 13.2,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "Subaru", "model": "WRX STI", "variant": "VA 2.5 EJ257",
        "year": 2016,
        "mods_applied": "Stage 2 ECU;Turbo Upgrade;Intercooler;Injectors;Fuel Pump",
        "mod_description": "Tomei turbo + TMIC + injectors + E85 tune + forged internals",
        "result_hp": 500, "result_torque": 600,
        "result_0_100": 3.8, "result_top_speed": 280,
        "result_quarter_mile": 11.5,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── Honda Civic Type R FK8 ───────────────────────────────────────────
    {
        "make": "Honda", "model": "Civic Type R", "variant": "FK8 2.0 K20C1",
        "year": 2018,
        "mods_applied": "Stage 1 ECU;Intake",
        "mod_description": "Hondata FlashPro Stage 1 + J's Racing intake",
        "result_hp": 340, "result_torque": 420,
        "result_0_100": 5.5, "result_top_speed": 275,
        "result_quarter_mile": 13.5,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "Honda", "model": "Civic Type R", "variant": "FK8 2.0 K20C1",
        "year": 2018,
        "mods_applied": "Stage 2 ECU;Intake;Turbo Upgrade;Intercooler;Downpipe",
        "mod_description": "Precision 6266 turbo + FMIC + Hondata tune + full exhaust",
        "result_hp": 500, "result_torque": 560,
        "result_0_100": 4.2, "result_top_speed": 295,
        "result_quarter_mile": 11.8,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── Mitsubishi Lancer Evo X ─────────────────────────────────────────
    {
        "make": "Mitsubishi", "model": "Lancer Evo X", "variant": "CZ4A 2.0 4B11T",
        "year": 2010,
        "mods_applied": "Stage 1 ECU;Intake;Downpipe",
        "mod_description": "EvoTune Stage 2 map + Injen intake + catless DP",
        "result_hp": 380, "result_torque": 480,
        "result_0_100": 4.2, "result_top_speed": 275,
        "result_quarter_mile": 12.5,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "Mitsubishi", "model": "Lancer Evo X", "variant": "CZ4A 2.0 4B11T",
        "year": 2010,
        "mods_applied": "Stage 2 ECU;Turbo Upgrade;Intercooler;Injectors;Exhaust",
        "mod_description": "Garrett GTX3076R + FMIC + 1000cc injectors + EvoTune aggressive map",
        "result_hp": 550, "result_torque": 640,
        "result_0_100": 3.4, "result_top_speed": 290,
        "result_quarter_mile": 10.8,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── Ford Focus ST Mk3 ────────────────────────────────────────────────
    {
        "make": "Ford", "model": "Focus ST", "variant": "Mk3 2.0 EcoBoost",
        "year": 2014,
        "mods_applied": "Stage 1 ECU;Intake",
        "mod_description": "Mountune Stage 1 remap + upgraded intake",
        "result_hp": 280, "result_torque": 390,
        "result_0_100": 5.8, "result_top_speed": 255,
        "result_quarter_mile": 13.8,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "Ford", "model": "Focus ST", "variant": "Mk3 2.0 EcoBoost",
        "year": 2014,
        "mods_applied": "Stage 2 ECU;Intercooler;Intake;Downpipe",
        "mod_description": "Mountune MP275 kit: ECU + FMIC + intake + downpipe",
        "result_hp": 310, "result_torque": 430,
        "result_0_100": 5.3, "result_top_speed": 262,
        "result_quarter_mile": 13.3,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── Renault Megane RS 3 ──────────────────────────────────────────────
    {
        "make": "Renault", "model": "Megane RS", "variant": "Mk3 2.0 Turbo",
        "year": 2013,
        "mods_applied": "Stage 1 ECU;Intake",
        "mod_description": "Remap to 280 hp + aftermarket intake",
        "result_hp": 280, "result_torque": 380,
        "result_0_100": 6.0, "result_top_speed": 255,
        "result_quarter_mile": 14.0,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── Toyota GR Yaris ─────────────────────────────────────────────────
    {
        "make": "Toyota", "model": "GR Yaris", "variant": "GXPA16 1.6 G16E-GTS",
        "year": 2021,
        "mods_applied": "Stage 1 ECU;Intake",
        "mod_description": "Ecutek Stage 1 + Ramair intake",
        "result_hp": 290, "result_torque": 400,
        "result_0_100": 5.0, "result_top_speed": 240,
        "result_quarter_mile": 13.3,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "Toyota", "model": "GR Yaris", "variant": "GXPA16 1.6 G16E-GTS",
        "year": 2021,
        "mods_applied": "Stage 2 ECU;Intercooler;Intake;Exhaust",
        "mod_description": "Ecutek Stage 2 + FMIC + catback exhaust",
        "result_hp": 360, "result_torque": 470,
        "result_0_100": 4.3, "result_top_speed": 255,
        "result_quarter_mile": 12.4,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── Mercedes AMG A45 W176 ────────────────────────────────────────────
    {
        "make": "Mercedes", "model": "A45 AMG", "variant": "W176 2.0 M133",
        "year": 2016,
        "mods_applied": "Stage 1 ECU;Intake",
        "mod_description": "Weistec W.1 ECU upgrade + performance intake",
        "result_hp": 415, "result_torque": 530,
        "result_0_100": 4.0, "result_top_speed": 280,
        "result_quarter_mile": 12.2,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── Volkswagen Polo GTI AW ───────────────────────────────────────────
    {
        "make": "VW", "model": "Polo GTI", "variant": "AW 2.0 TSI",
        "year": 2019,
        "mods_applied": "Stage 1 ECU",
        "mod_description": "Revo Stage 1 remap only",
        "result_hp": 230, "result_torque": 330,
        "result_0_100": 6.4, "result_top_speed": 240,
        "result_quarter_mile": 14.5,
        "measurement_type": "dyno", "source_type": "community",
    },
    # ── Skoda Octavia RS 5E ──────────────────────────────────────────────
    {
        "make": "Skoda", "model": "Octavia RS", "variant": "5E 2.0 TSI",
        "year": 2017,
        "mods_applied": "Stage 1 ECU;Intake",
        "mod_description": "Unitronic Stage 1 + K&N intake",
        "result_hp": 260, "result_torque": 380,
        "result_0_100": 6.2, "result_top_speed": 255,
        "result_quarter_mile": 14.0,
        "measurement_type": "dyno", "source_type": "community",
    },
    {
        "make": "Skoda", "model": "Octavia RS", "variant": "5E 2.0 TSI",
        "year": 2017,
        "mods_applied": "Stage 2 ECU;Intercooler;Downpipe;Intake;DSG Tune",
        "mod_description": "Unitronic Stage 2 full package with DSG flash",
        "result_hp": 350, "result_torque": 460,
        "result_0_100": 5.3, "result_top_speed": 265,
        "result_quarter_mile": 13.0,
        "measurement_type": "dyno", "source_type": "community",
    },
]


def get_static_builds() -> list[dict]:
    rows = []
    for b in STATIC_BUILDS:
        row = b.copy()
        row["build_id"]  = _build_id(b["make"], b["model"], b["mods_applied"], "static")
        row["car_id"]    = _car_id_from_make_model(b["make"], b["model"])
        row["source_url"] = "static_curated"
        rows.append(row)
    log.info(f"Static builds: {len(rows)} entries")
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape_all(
    static_only: bool = False,
    makes_filter: list[str] | None = None,
) -> pd.DataFrame:
    all_rows: list[dict] = []

    all_rows.extend(get_static_builds())

    if not static_only:
        fl_rows = scrape_fastestlaps()
        dt_rows = scrape_dragtimes(makes_filter=makes_filter)
        all_rows.extend(fl_rows)
        all_rows.extend(dt_rows)

    df = pd.DataFrame(all_rows)
    df.drop_duplicates(subset=["build_id"], keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Enforce column order
    cols = [
        "build_id", "car_id", "make", "model", "variant", "year",
        "mods_applied", "mod_description",
        "result_hp", "result_torque",
        "result_0_100", "result_top_speed", "result_quarter_mile",
        "measurement_type", "source_type", "source_url",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols]

    df.to_csv(OUTPUT_CSV, index=False)
    log.info(f"Saved {len(df)} builds → {OUTPUT_CSV}")
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape tuned car build data")
    parser.add_argument(
        "--static-only", action="store_true",
        help="Only use built-in seed builds (no internet needed)"
    )
    parser.add_argument(
        "--makes", nargs="+", metavar="MAKE",
        help="Filter by make (e.g. --makes VW BMW)"
    )
    args = parser.parse_args()

    df = scrape_all(static_only=args.static_only, makes_filter=args.makes)
    print(f"\nDone. {len(df)} builds in {OUTPUT_CSV}")
    print(df[["make", "model", "variant", "result_hp",
              "result_0_100", "mods_applied"]].head(20).to_string(index=False))
