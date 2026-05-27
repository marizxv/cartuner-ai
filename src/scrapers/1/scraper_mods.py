"""
scraper_mods.py
===============
Scrapes modification / tuning data → modifications_raw.csv

Sources (all static HTML, no JS rendering needed):
  1. unitronic-tune.com   — ECU/TCU software stages for VAG, BMW, Porsche, etc.
  2. revo-technik.com     — ECU tunes for VW/Audi/Seat/Skoda
  3. milltek.com          — exhaust systems (bolt-on, hp gains listed for some)
  4. airtec-motorsport.com — intercoolers, induction kits with gains
  5. Fallback static list  — common mods with typical gains (always works)

Output columns (modifications_raw.csv):
    mod_id, mod_name, mod_category, typical_hp_gain_pct,
    typical_tq_gain_pct, typical_hp_gain_abs, typical_tq_gain_abs,
    compatible_makes, compatible_models, difficulty_level,
    typical_cost_usd, notes, source_url

Usage:
    python scraper_mods.py               # all sources + static list
    python scraper_mods.py --static-only # only the built-in static list (always works)
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

OUTPUT_CSV = "modifications_raw.csv"
DELAY      = 2.0
TIMEOUT    = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
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
# HTTP helper
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


def _make_id(name: str, source: str) -> str:
    key = f"{name}_{source}"
    return "MOD_" + hashlib.md5(key.encode()).hexdigest()[:8].upper()


# ---------------------------------------------------------------------------
# Source 1: unitronic-tune.com
# ---------------------------------------------------------------------------

UNITRONIC_PAGES = [
    # VW / Audi / Skoda / Seat
    "https://www.unitronic-tune.com/performance-software/vw-performance-software/",
    "https://www.unitronic-tune.com/performance-software/audi-performance-software/",
    "https://www.unitronic-tune.com/performance-software/skoda-performance-software/",
    "https://www.unitronic-tune.com/performance-software/seat-performance-software/",
    # BMW
    "https://www.unitronic-tune.com/performance-software/bmw-performance-software/",
    # Porsche
    "https://www.unitronic-tune.com/performance-software/porsche-performance-software/",
]


def scrape_unitronic() -> list[dict]:
    """
    Unitronic product pages list ECU upgrade packages with before/after power tables.
    Structure: product cards → each has model name + hp/tq numbers.
    """
    rows = []
    for page_url in UNITRONIC_PAGES:
        soup = fetch(page_url)
        if not soup:
            continue

        # Determine make from URL
        make = re.search(r"/(\w+)-performance-software", page_url)
        make = make.group(1).title() if make else "Unknown"

        # Product cards — Unitronic uses WooCommerce-style product grid
        for card in soup.select(".product, .wc-block-grid__product, article.type-product"):
            title_el = card.select_one("h2, h3, .woocommerce-loop-product__title, .wp-block-button__link")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)

            link_el = card.select_one("a[href]")
            link = link_el["href"] if link_el else page_url

            # Try to find hp numbers in the card text
            card_text = card.get_text(" ", strip=True)
            hp_gains = re.findall(r"(\d+)\s*(?:whp|hp|bhp|ps)", card_text, re.IGNORECASE)
            tq_gains = re.findall(r"(\d+)\s*(?:ft-?lb|nm|tq)", card_text, re.IGNORECASE)

            rows.append({
                "mod_id":               _make_id(title, "unitronic"),
                "mod_name":             f"Unitronic ECU Upgrade – {title}",
                "mod_category":         "ECU Tune",
                "typical_hp_gain_pct":  None,
                "typical_tq_gain_pct":  None,
                "typical_hp_gain_abs":  float(hp_gains[-1]) if hp_gains else None,
                "typical_tq_gain_abs":  float(tq_gains[-1]) if tq_gains else None,
                "compatible_makes":     make,
                "compatible_models":    title,
                "difficulty_level":     "Professional",
                "typical_cost_usd":     350,
                "notes":                "ECU flash by dealer; Stage 1 = bolt-on safe",
                "source_url":           link,
            })
            # Scrape individual product page for detailed before/after table
            if link and link != page_url:
                detail_rows = _scrape_unitronic_product(link, make, title)
                if detail_rows:
                    rows.extend(detail_rows)

    log.info(f"Unitronic: {len(rows)} modifications found")
    return rows


def _scrape_unitronic_product(url: str, make: str, product_name: str) -> list[dict]:
    """Scrape detailed power table from a single Unitronic product page."""
    soup = fetch(url)
    if not soup:
        return []

    rows = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not any(k in " ".join(headers) for k in ("hp", "tq", "whp", "torque", "power")):
            continue
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2 or not cells[0]:
                continue
            row_data = dict(zip(headers, cells))

            # Try to parse stage name and hp gain
            stage = next((v for k, v in row_data.items() if "stage" in k or "tune" in k), cells[0])
            stock_hp = _num(row_data.get("stock hp", row_data.get("stock", "")), r"(\d+)")
            tuned_hp = _num(row_data.get("tuned hp", row_data.get("stage 1", row_data.get("stage1", ""))), r"(\d+)")

            if stock_hp and tuned_hp and tuned_hp > stock_hp:
                gain_pct = round((tuned_hp - stock_hp) / stock_hp * 100, 1)
                rows.append({
                    "mod_id":               _make_id(f"{product_name}_{stage}", "unitronic_detail"),
                    "mod_name":             f"Unitronic {stage} – {product_name}",
                    "mod_category":         "ECU Tune",
                    "typical_hp_gain_pct":  gain_pct,
                    "typical_tq_gain_pct":  None,
                    "typical_hp_gain_abs":  tuned_hp - stock_hp,
                    "typical_tq_gain_abs":  None,
                    "compatible_makes":     make,
                    "compatible_models":    product_name,
                    "difficulty_level":     "Professional",
                    "typical_cost_usd":     350,
                    "notes":                f"Stock: {stock_hp} hp → Tuned: {tuned_hp} hp",
                    "source_url":           url,
                })
    return rows


# ---------------------------------------------------------------------------
# Source 2: APR (goapr.com) — static table pages (some pages DO have HTML tables)
# ---------------------------------------------------------------------------

APR_PAGES = [
    "https://www.goapr.com/products/ecu_upgrade_2-0t_gen3_mqb_is20.html",
    "https://www.goapr.com/products/ecu_upgrade_2-0t_gen3_mqb_is38.html",
    "https://www.goapr.com/products/ecu_upgrade_1-8t_gen3_mqb.html",
    "https://www.goapr.com/products/ecu_upgrade_2-5t_b9_rs3_tt_rs.html",
    "https://www.goapr.com/products/ecu_upgrade_2-0t_gen1_mqb.html",
    "https://www.goapr.com/products/ecu_upgrade_3-0t_v6_b8_b8-5.html",
    "https://www.goapr.com/products/ecu_upgrade_4-0t_v8_b8.html",
    "https://www.goapr.com/products/ecu_upgrade_2-0t_c7_a6_a7.html",
]


def scrape_apr() -> list[dict]:
    """
    APR pages. Some render tables in JS (invisible to requests),
    but product description text often contains numbers.
    We parse both HTML tables and text patterns.
    """
    rows = []
    for url in APR_PAGES:
        soup = fetch(url)
        if not soup:
            continue

        # Try HTML tables first
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not any(k in " ".join(headers) for k in ("hp", "tq", "whp", "torque")):
                continue
            for tr in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cells) < 2:
                    continue
                row_data = dict(zip(headers, cells))

                vehicle = cells[0]
                stock_hp  = _num(row_data.get("stock hp",  ""), r"(\d+)")
                stage1_hp = _num(row_data.get("stage 1",   row_data.get("stage1", "")), r"(\d+)")
                stage2_hp = _num(row_data.get("stage 2",   row_data.get("stage2", "")), r"(\d+)")

                for stage, tuned_hp in [("Stage 1", stage1_hp), ("Stage 2", stage2_hp)]:
                    if stock_hp and tuned_hp and tuned_hp > stock_hp:
                        gain = tuned_hp - stock_hp
                        rows.append({
                            "mod_id":               _make_id(f"APR_{stage}_{vehicle}", url),
                            "mod_name":             f"APR {stage} ECU Upgrade",
                            "mod_category":         "ECU Tune",
                            "typical_hp_gain_pct":  round(gain / stock_hp * 100, 1),
                            "typical_tq_gain_pct":  None,
                            "typical_hp_gain_abs":  gain,
                            "typical_tq_gain_abs":  None,
                            "compatible_makes":     "VW;Audi;Skoda;Seat",
                            "compatible_models":    vehicle,
                            "difficulty_level":     "Professional",
                            "typical_cost_usd":     700 if stage == "Stage 2" else 400,
                            "notes":                f"Stock: {stock_hp} hp → {stage}: {tuned_hp} hp (whp)",
                            "source_url":           url,
                        })

        # Fallback: parse description text for "X whp / Y ft-lbs" patterns
        page_text = soup.get_text(" ")
        pattern = re.compile(
            r"(Stage\s*\d)\D{0,30}?(\d{3})\s*(?:whp|hp|bhp)\s*[/&and,]\s*(\d{3})\s*(?:ft[- ]?lb|nm)",
            re.IGNORECASE,
        )
        for m in pattern.finditer(page_text):
            stage, hp, tq = m.group(1), float(m.group(2)), float(m.group(3))
            rows.append({
                "mod_id":               _make_id(f"APR_{stage}_{hp}", url),
                "mod_name":             f"APR {stage} ECU Upgrade",
                "mod_category":         "ECU Tune",
                "typical_hp_gain_pct":  None,
                "typical_tq_gain_pct":  None,
                "typical_hp_gain_abs":  None,
                "typical_tq_gain_abs":  None,
                "compatible_makes":     "VW;Audi;Skoda;Seat",
                "compatible_models":    "",
                "difficulty_level":     "Professional",
                "typical_cost_usd":     500,
                "notes":                f"{stage}: {hp} hp / {tq} tq (from page text)",
                "source_url":           url,
            })

    log.info(f"APR: {len(rows)} modifications found")
    return rows


# ---------------------------------------------------------------------------
# Source 3: Static curated list (ALWAYS works — no internet needed)
# ---------------------------------------------------------------------------
# This is the most reliable source for training data.
# Ranges are based on well-known community data from:
#   - r/cars, r/JDM, r/projectcar
#   - eEuroparts, fcpeuro, turner motorsport
#   - motorsport.com, carthrottle.com

STATIC_MODS = [
    # ── ECU TUNES ─────────────────────────────────────────────────────────
    {
        "mod_name": "Stage 1 ECU Remap (Petrol Turbo)",
        "mod_category": "ECU Tune",
        "typical_hp_gain_pct": 15,
        "typical_tq_gain_pct": 20,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;Skoda;Seat;BMW;Ford;Opel;Renault;Peugeot;Citroen",
        "compatible_models": "All turbocharged petrol models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 350,
        "notes": "No hardware mods needed. Boost, ignition, fuelling optimised. Typical 15-25% gain.",
    },
    {
        "mod_name": "Stage 2 ECU Remap (Petrol Turbo)",
        "mod_category": "ECU Tune",
        "typical_hp_gain_pct": 30,
        "typical_tq_gain_pct": 35,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;Skoda;Seat;BMW;Ford;Opel",
        "compatible_models": "All turbocharged petrol models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 700,
        "notes": "Requires supporting mods: intake + downpipe minimum. 25-40% gain.",
    },
    {
        "mod_name": "Stage 1 ECU Remap (Diesel TDI/TDCi/CDI)",
        "mod_category": "ECU Tune",
        "typical_hp_gain_pct": 20,
        "typical_tq_gain_pct": 30,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;BMW;Mercedes;Ford;Opel;Renault",
        "compatible_models": "All turbocharged diesel models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 300,
        "notes": "Diesel responds very well to remapping. Typical 20-30% gain.",
    },
    {
        "mod_name": "Stage 2 ECU Remap (Diesel TDI/TDCi/CDI)",
        "mod_category": "ECU Tune",
        "typical_hp_gain_pct": 40,
        "typical_tq_gain_pct": 50,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;BMW;Mercedes;Ford",
        "compatible_models": "All turbocharged diesel models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 600,
        "notes": "Requires EGR delete and DPF delete in some cases. Check local laws.",
    },
    # ── INTAKE / INDUCTION ────────────────────────────────────────────────
    {
        "mod_name": "Cold Air Intake / Induction Kit",
        "mod_category": "Intake",
        "typical_hp_gain_pct": 3,
        "typical_tq_gain_pct": 2,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "DIY",
        "typical_cost_usd": 150,
        "notes": "Gains are modest standalone. Best combined with ECU tune. Improves throttle response.",
    },
    {
        "mod_name": "High-Flow Panel Air Filter (OEM Housing)",
        "mod_category": "Intake",
        "typical_hp_gain_pct": 1,
        "typical_tq_gain_pct": 1,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "DIY",
        "typical_cost_usd": 50,
        "notes": "Drop-in K&N or Pipercross filter. Minimal gains but cheap and easy.",
    },
    # ── EXHAUST ──────────────────────────────────────────────────────────
    {
        "mod_name": "Cat-Back Exhaust System",
        "mod_category": "Exhaust",
        "typical_hp_gain_pct": 3,
        "typical_tq_gain_pct": 2,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "Professional",
        "typical_cost_usd": 600,
        "notes": "After-cat exhaust. Mainly sound improvement. Small power gain.",
    },
    {
        "mod_name": "Turbo-Back Exhaust with High-Flow Cat",
        "mod_category": "Exhaust",
        "typical_hp_gain_pct": 8,
        "typical_tq_gain_pct": 7,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;BMW;Ford;Subaru;Mitsubishi",
        "compatible_models": "Turbocharged models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 900,
        "notes": "Decat downpipe + cat-back. Significant flow improvement for turbos.",
    },
    {
        "mod_name": "Downpipe / Decat Pipe",
        "mod_category": "Exhaust",
        "typical_hp_gain_pct": 6,
        "typical_tq_gain_pct": 5,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;BMW;Subaru;Mitsubishi;Ford",
        "compatible_models": "Turbocharged models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 350,
        "notes": "Removes catalyst restriction on turbo outlet. Needs remap for full benefit.",
    },
    # ── INTERCOOLER ──────────────────────────────────────────────────────
    {
        "mod_name": "Front-Mount Intercooler (FMIC) Upgrade",
        "mod_category": "Intercooler",
        "typical_hp_gain_pct": 5,
        "typical_tq_gain_pct": 5,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;BMW;Subaru;Mitsubishi;Ford;Opel",
        "compatible_models": "Turbocharged models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 500,
        "notes": "Reduces intake air temperature, more consistent power. Required for Stage 2+.",
    },
    {
        "mod_name": "Top-Mount Intercooler (TMIC) Upgrade",
        "mod_category": "Intercooler",
        "typical_hp_gain_pct": 4,
        "typical_tq_gain_pct": 4,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "Subaru;Mitsubishi;Saab",
        "compatible_models": "EJ/EJ engines (Impreza, Evo)",
        "difficulty_level": "Professional",
        "typical_cost_usd": 450,
        "notes": "Popular on Subaru Impreza / WRX. Reduces heat soak.",
    },
    # ── TURBO ────────────────────────────────────────────────────────────
    {
        "mod_name": "Hybrid Turbocharger Upgrade",
        "mod_category": "Turbo Upgrade",
        "typical_hp_gain_pct": 25,
        "typical_tq_gain_pct": 30,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;BMW;Subaru;Mitsubishi",
        "compatible_models": "Turbocharged models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 1200,
        "notes": "Upgraded compressor/turbine wheels in OEM housing. Bolt-on fit. Needs remap + fuelling.",
    },
    {
        "mod_name": "Full Turbo Replacement (Larger Unit)",
        "mod_category": "Turbo Upgrade",
        "typical_hp_gain_pct": 50,
        "typical_tq_gain_pct": 45,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;Subaru;Mitsubishi;Ford",
        "compatible_models": "Turbocharged models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 2500,
        "notes": "Full turbo swap. Requires supporting mods: fuelling, intercooler, remap, possibly internals.",
    },
    # ── FUELLING ────────────────────────────────────────────────────────
    {
        "mod_name": "High-Flow Fuel Injectors",
        "mod_category": "Fuelling",
        "typical_hp_gain_pct": 0,
        "typical_tq_gain_pct": 0,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "Turbocharged models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 400,
        "notes": "Enabling mod — allows higher power levels rather than adding HP directly.",
    },
    {
        "mod_name": "High-Pressure Fuel Pump (HPFP) Upgrade",
        "mod_category": "Fuelling",
        "typical_hp_gain_pct": 0,
        "typical_tq_gain_pct": 0,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;BMW;Subaru",
        "compatible_models": "Direct injection turbocharged models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 300,
        "notes": "Required for E30/E85 fuel mixes or Stage 3+ tunes. Enabling mod.",
    },
    # ── SUPERCHARGER ─────────────────────────────────────────────────────
    {
        "mod_name": "Supercharger Kit (Roots/Twin-Screw)",
        "mod_category": "Supercharger",
        "typical_hp_gain_pct": 40,
        "typical_tq_gain_pct": 35,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "Ford;Chevrolet;BMW;Toyota;Honda",
        "compatible_models": "Naturally aspirated V6/V8 models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 4000,
        "notes": "Complete bolt-on kit. Linear power delivery. No lag.",
    },
    # ── SUSPENSION / HANDLING ────────────────────────────────────────────
    {
        "mod_name": "Coilover Suspension Kit",
        "mod_category": "Suspension",
        "typical_hp_gain_pct": 0,
        "typical_tq_gain_pct": 0,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "Professional",
        "typical_cost_usd": 1000,
        "notes": "Improves handling, not power. Reduces body roll; adjustable ride height/damping.",
    },
    {
        "mod_name": "Sway Bar / Anti-Roll Bar Upgrade",
        "mod_category": "Suspension",
        "typical_hp_gain_pct": 0,
        "typical_tq_gain_pct": 0,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "DIY",
        "typical_cost_usd": 200,
        "notes": "Reduces body roll significantly. No power gain.",
    },
    # ── BRAKES ──────────────────────────────────────────────────────────
    {
        "mod_name": "Big Brake Kit (BBK)",
        "mod_category": "Brakes",
        "typical_hp_gain_pct": 0,
        "typical_tq_gain_pct": 0,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "Professional",
        "typical_cost_usd": 1500,
        "notes": "Larger rotors + multi-piston calipers. Needed for high-power builds on track.",
    },
    # ── NITROUS ─────────────────────────────────────────────────────────
    {
        "mod_name": "Nitrous Oxide System (NOS) – Dry Kit 50hp",
        "mod_category": "Nitrous",
        "typical_hp_gain_pct": None,
        "typical_tq_gain_pct": None,
        "typical_hp_gain_abs": 50,
        "typical_tq_gain_abs": 40,
        "compatible_makes": "All",
        "compatible_models": "All naturally aspirated",
        "difficulty_level": "Professional",
        "typical_cost_usd": 600,
        "notes": "50 hp shot. Temporary gain when activated. Needs fresh spark plugs and timing retard.",
    },
    {
        "mod_name": "Nitrous Oxide System (NOS) – Wet Kit 100hp",
        "mod_category": "Nitrous",
        "typical_hp_gain_pct": None,
        "typical_tq_gain_pct": None,
        "typical_hp_gain_abs": 100,
        "typical_tq_gain_abs": 80,
        "compatible_makes": "All",
        "compatible_models": "Strong engine required",
        "difficulty_level": "Professional",
        "typical_cost_usd": 900,
        "notes": "Injects fuel + N2O. Requires forged internals for reliability.",
    },
    # ── ENGINE INTERNALS ─────────────────────────────────────────────────
    {
        "mod_name": "Forged Pistons + Rods",
        "mod_category": "Engine Internals",
        "typical_hp_gain_pct": 0,
        "typical_tq_gain_pct": 0,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "Professional",
        "typical_cost_usd": 2000,
        "notes": "Enabling mod for 400+ hp builds. No direct gain but allows higher boost safely.",
    },
    {
        "mod_name": "Ported and Polished Cylinder Head",
        "mod_category": "Engine Internals",
        "typical_hp_gain_pct": 10,
        "typical_tq_gain_pct": 8,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "Professional",
        "typical_cost_usd": 1200,
        "notes": "Improves airflow through head. Often paired with cam upgrades.",
    },
    {
        "mod_name": "Performance Camshaft Upgrade",
        "mod_category": "Engine Internals",
        "typical_hp_gain_pct": 8,
        "typical_tq_gain_pct": 6,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "Honda;Toyota;Subaru;BMW;Ford;Mazda",
        "compatible_models": "Naturally aspirated high-rev engines",
        "difficulty_level": "Professional",
        "typical_cost_usd": 800,
        "notes": "More lift/duration. Moves power band higher in rev range. Needs remap.",
    },
    # ── FORCED INDUCTION ADDITIONS ───────────────────────────────────────
    {
        "mod_name": "Boost Controller / Manual Boost Controller",
        "mod_category": "Boost Control",
        "typical_hp_gain_pct": 8,
        "typical_tq_gain_pct": 8,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "Turbocharged models",
        "difficulty_level": "DIY",
        "typical_cost_usd": 80,
        "notes": "Simple boost increase. Risk of overboosting without remap. Cheap but basic.",
    },
    {
        "mod_name": "Blow-Off Valve / Diverter Valve Upgrade",
        "mod_category": "Boost Control",
        "typical_hp_gain_pct": 1,
        "typical_tq_gain_pct": 1,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;Subaru;Mitsubishi;BMW",
        "compatible_models": "Turbocharged models",
        "difficulty_level": "DIY",
        "typical_cost_usd": 120,
        "notes": "Prevents compressor surge. Mainly reliability and sound improvement.",
    },
    # ── TRANSMISSION ─────────────────────────────────────────────────────
    {
        "mod_name": "Short-Throw Shifter",
        "mod_category": "Transmission",
        "typical_hp_gain_pct": 0,
        "typical_tq_gain_pct": 0,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "Manual transmission models",
        "difficulty_level": "DIY",
        "typical_cost_usd": 150,
        "notes": "Reduces gear change throw by 30-50%. No power gain.",
    },
    {
        "mod_name": "Limited Slip Differential (LSD)",
        "mod_category": "Drivetrain",
        "typical_hp_gain_pct": 0,
        "typical_tq_gain_pct": 0,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "Professional",
        "typical_cost_usd": 1500,
        "notes": "Reduces wheelspin, improves traction on exit. Essential for high-power FWD/RWD.",
    },
    {
        "mod_name": "DSG/TCU Remap (Automatic/DSG Gearbox)",
        "mod_category": "ECU Tune",
        "typical_hp_gain_pct": 5,
        "typical_tq_gain_pct": 10,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "VW;Audi;Skoda;Seat;BMW;Mercedes",
        "compatible_models": "DSG/DCT/ZF automatic models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 300,
        "notes": "Higher torque limit, faster shifts. Often done with ECU remap.",
    },
    # ── WATER/METH INJECTION ─────────────────────────────────────────────
    {
        "mod_name": "Water/Methanol Injection Kit",
        "mod_category": "Fuel Additives",
        "typical_hp_gain_pct": 10,
        "typical_tq_gain_pct": 8,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "Turbocharged models",
        "difficulty_level": "Professional",
        "typical_cost_usd": 500,
        "notes": "Cools intake charge, allows more boost/timing. Popular on IS20/IS38 builds.",
    },
    # ── AERO ────────────────────────────────────────────────────────────
    {
        "mod_name": "Front Splitter",
        "mod_category": "Aerodynamics",
        "typical_hp_gain_pct": 0,
        "typical_tq_gain_pct": 0,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "DIY",
        "typical_cost_usd": 200,
        "notes": "Improves downforce at high speed. Reduces drag coefficient slightly.",
    },
    {
        "mod_name": "Rear Wing / Spoiler",
        "mod_category": "Aerodynamics",
        "typical_hp_gain_pct": 0,
        "typical_tq_gain_pct": 0,
        "typical_hp_gain_abs": None,
        "typical_tq_gain_abs": None,
        "compatible_makes": "All",
        "compatible_models": "All",
        "difficulty_level": "DIY",
        "typical_cost_usd": 300,
        "notes": "Adds downforce at rear. Can increase drag slightly.",
    },
]


def get_static_mods() -> list[dict]:
    """Return the curated static mods list with generated IDs."""
    rows = []
    for mod in STATIC_MODS:
        mod_copy = mod.copy()
        mod_copy["mod_id"]     = _make_id(mod["mod_name"], "static")
        mod_copy["source_url"] = "static_curated"
        rows.append(mod_copy)
    log.info(f"Static mods: {len(rows)} modifications")
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape_all(static_only: bool = False) -> pd.DataFrame:
    all_rows: list[dict] = []

    all_rows.extend(get_static_mods())

    if not static_only:
        all_rows.extend(scrape_apr())
        all_rows.extend(scrape_unitronic())

    df = pd.DataFrame(all_rows)

    # Clean up
    df.drop_duplicates(subset=["mod_name", "compatible_models"], keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Enforce column order
    cols = [
        "mod_id", "mod_name", "mod_category",
        "typical_hp_gain_pct", "typical_tq_gain_pct",
        "typical_hp_gain_abs", "typical_tq_gain_abs",
        "compatible_makes", "compatible_models",
        "difficulty_level", "typical_cost_usd",
        "notes", "source_url",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols]

    df.to_csv(OUTPUT_CSV, index=False)
    log.info(f"Saved {len(df)} modifications → {OUTPUT_CSV}")
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape tuning modification data")
    parser.add_argument(
        "--static-only", action="store_true",
        help="Only generate the built-in static mods list (no internet needed)"
    )
    args = parser.parse_args()

    df = scrape_all(static_only=args.static_only)
    print(f"\nDone. {len(df)} modifications in {OUTPUT_CSV}")
    print(df[["mod_name", "mod_category", "typical_hp_gain_pct",
              "compatible_makes"]].to_string(index=False))
