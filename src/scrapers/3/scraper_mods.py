"""
scraper_mods.py — modifications_raw.csv

Sources:
  1. APR (goapr.com)         — VAG ECU tuning
  2. RaceChip (racechip.com) — ECU tuning for all makes
  3. Extended static base    — 40+ modifications with real numbers

Install: pip install cloudscraper lxml beautifulsoup4 pandas
Run:
    python scraper_mods.py               # all sources
    python scraper_mods.py --static-only # static only (no internet)
"""

import re, time, random, logging, hashlib, argparse
from pathlib import Path
import pandas as pd
from bs4 import BeautifulSoup

try:
    import cloudscraper
    def _make_session():
        return cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    log_backend = "cloudscraper"
except ImportError:
    import requests
    def _make_session():
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        })
        return s
    log_backend = "requests (may get 403)"

OUTPUT_CSV = "modifications_raw.csv"

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)
log.info(f"Backend: {log_backend}")


def _sleep(lo=1.5, hi=3.5):
    time.sleep(random.uniform(lo, hi))


def _fetch(session, url: str, referer: str = "https://www.google.com/") -> BeautifulSoup | None:
    session.headers.update({"Referer": referer})
    for attempt in range(1, 4):
        try:
            log.info(f"GET {url}")
            r = session.get(url, timeout=20)
            if r.status_code == 403:
                wait = 15 * attempt
                log.warning(f"403 — waiting {wait}s (attempt {attempt}/3)")
                time.sleep(wait)
                continue
            if r.status_code in (404, 410):
                log.warning(f"Skip {url} — HTTP {r.status_code}")
                return None
            r.raise_for_status()
            _sleep()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log.warning(f"Attempt {attempt}/3: {e}")
            _sleep(4, 8)
    log.error(f"Giving up: {url}")
    return None


def _num(text, pat):
    if not text: return None
    m = re.search(pat, str(text).replace(",", ""))
    return float(m.group(1)) if m else None


def _mod_id(name: str, source: str) -> str:
    return "MOD_" + hashlib.md5(f"{name}_{source}".encode()).hexdigest()[:8].upper()


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 1: APR (goapr.com)
# ─────────────────────────────────────────────────────────────────────────────
APR_URLS = [
    "https://www.goapr.com/products/ecu_upgrade_2-0t_gen3_mqb_is20.html",
    "https://www.goapr.com/products/ecu_upgrade_2-0t_gen3_mqb_is38.html",
    "https://www.goapr.com/products/ecu_upgrade_1-8t_gen3_mqb.html",
    "https://www.goapr.com/products/ecu_upgrade_2-5t_b9_rs3_tt_rs.html",
    "https://www.goapr.com/products/ecu_upgrade_2-0t_gen1_mqb.html",
    "https://www.goapr.com/products/ecu_upgrade_3-0t_v6_b8_b8-5.html",
    "https://www.goapr.com/products/ecu_upgrade_4-0t_v8_b8.html",
    "https://www.goapr.com/products/ecu_upgrade_2-0t_c7_a6_a7.html",
    "https://www.goapr.com/products/ecu_upgrade_3-0t_c7_a6_a7_s6_s7_sq5.html",
]


def scrape_apr() -> list[dict]:
    session = _make_session()
    rows = []

    for url in APR_URLS:
        soup = _fetch(session, url, referer="https://www.goapr.com/")
        if not soup:
            continue

        # Attempt 1: HTML tables
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not any(k in " ".join(headers) for k in ("hp", "tq", "torque", "whp")):
                continue
            for tr in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cells) < 2 or not cells[0]:
                    continue
                row_d = dict(zip(headers, cells))
                vehicle    = cells[0]
                stock_hp   = _num(row_d.get("stock hp",  ""), r"(\d+)")
                stage1_hp  = _num(row_d.get("stage 1",   row_d.get("stage1",  "")), r"(\d+)")
                stage2_hp  = _num(row_d.get("stage 2",   row_d.get("stage2",  "")), r"(\d+)")
                stock_tq   = _num(row_d.get("stock tq",  ""), r"(\d+)")
                stage1_tq  = _num(row_d.get("stage 1 tq",row_d.get("stage1tq","")), r"(\d+)")

                for stage, t_hp, t_tq in [
                    ("Stage 1", stage1_hp, stage1_tq),
                    ("Stage 2", stage2_hp, None),
                ]:
                    if stock_hp and t_hp and t_hp > stock_hp:
                        gain_hp  = t_hp - stock_hp
                        gain_pct = round(gain_hp / stock_hp * 100, 1)
                        gain_tq  = (t_tq - stock_tq) if (t_tq and stock_tq) else None
                        rows.append({
                            "mod_id":              _mod_id(f"APR_{stage}_{vehicle}", url),
                            "mod_name":            f"APR {stage} ECU Upgrade",
                            "mod_category":        "ECU Tune",
                            "typical_hp_gain_pct": gain_pct,
                            "typical_tq_gain_pct": round(gain_tq / stock_tq * 100, 1) if (gain_tq and stock_tq) else None,
                            "typical_hp_gain_abs": gain_hp,
                            "typical_tq_gain_abs": gain_tq,
                            "compatible_makes":    "VW;Audi;Skoda;Seat",
                            "compatible_models":   vehicle,
                            "difficulty_level":    "Professional",
                            "typical_cost_usd":    700 if stage == "Stage 2" else 400,
                            "notes":               f"Stock: {stock_hp} hp → {stage}: {t_hp} hp (whp). APR dyno.",
                            "source_url":          url,
                        })

        # Attempt 2: text patterns if table is not found
        text = soup.get_text(" ")
        for m in re.finditer(
            r"(Stage\s*\d)\D{0,40}?(\d{3})\s*(?:whp|hp|bhp)\D{0,10}?(\d{3})\s*(?:ft[- ]?lb|nm)",
            text, re.IGNORECASE
        ):
            stage, hp, tq = m.group(1), float(m.group(2)), float(m.group(3))
            rows.append({
                "mod_id":              _mod_id(f"APR_text_{stage}_{hp}", url),
                "mod_name":            f"APR {stage} ECU Upgrade",
                "mod_category":        "ECU Tune",
                "typical_hp_gain_pct": None,
                "typical_tq_gain_pct": None,
                "typical_hp_gain_abs": None,
                "typical_tq_gain_abs": None,
                "compatible_makes":    "VW;Audi;Skoda;Seat",
                "compatible_models":   "",
                "difficulty_level":    "Professional",
                "typical_cost_usd":    500,
                "notes":               f"{stage}: {hp} hp / {tq} tq (parsed from page text)",
                "source_url":          url,
            })

    log.info(f"APR: {len(rows)} modifications scraped")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 2: RaceChip (racechip.com) — ECU for all makes
# ─────────────────────────────────────────────────────────────────────────────
RACECHIP_MAKES = [
    "volkswagen", "audi", "bmw", "mercedes-benz",
    "ford", "opel", "skoda", "seat", "toyota",
    "honda", "subaru", "renault", "peugeot",
]


def scrape_racechip() -> list[dict]:
    session = _make_session()
    rows    = []
    base    = "https://www.racechip.com"

    for make in RACECHIP_MAKES:
        url  = f"{base}/performance-chips/{make}/"
        soup = _fetch(session, url, referer=base + "/")
        if not soup:
            continue

        # RaceChip shows model cards with hp before/after
        for card in soup.select(".model-card, .car-card, .product-item, article"):
            title_el = card.select_one("h2, h3, .model-name, .title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            text  = card.get_text(" ", strip=True)

            # Looking for "from Xhp to Yhp" or "+Z hp"
            gain_abs = _num(text, r"\+\s*(\d+)\s*(?:hp|ps|bhp)")
            from_hp  = _num(text, r"from\s+(\d+)\s*(?:hp|ps|bhp)")
            to_hp    = _num(text, r"to\s+(\d+)\s*(?:hp|ps|bhp)")

            if from_hp and to_hp and to_hp > from_hp:
                gain_abs = to_hp - from_hp
            if not gain_abs:
                continue

            gain_pct = round(gain_abs / from_hp * 100, 1) if from_hp else None

            link_el = card.select_one("a[href]")
            link    = base + link_el["href"] if link_el and link_el["href"].startswith("/") else url

            rows.append({
                "mod_id":              _mod_id(f"RaceChip_{make}_{title}", url),
                "mod_name":            f"RaceChip ECU Tuning Box",
                "mod_category":        "ECU Tune",
                "typical_hp_gain_pct": gain_pct,
                "typical_tq_gain_pct": None,
                "typical_hp_gain_abs": gain_abs,
                "typical_tq_gain_abs": None,
                "compatible_makes":    make.replace("-", " ").title(),
                "compatible_models":   title,
                "difficulty_level":    "DIY",
                "typical_cost_usd":    400,
                "notes":               f"Plug-in tuning box. {f'Stock: {from_hp} hp → Tuned: {to_hp} hp' if from_hp and to_hp else ''}",
                "source_url":          link,
            })

        log.info(f"RaceChip {make}: {len(rows)} total so far")

    log.info(f"RaceChip: {len(rows)} modifications scraped")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# STATIC BASE — 40 modifications with real typical numbers
# Sources: carthrottle.com, r/cars, r/projectcar, eEuroparts, fcpeuro
# ─────────────────────────────────────────────────────────────────────────────
STATIC_MODS = [
    # ── ECU TUNING ──────────────────────────────────────────────────────────
    dict(mod_name="Stage 1 ECU Remap — Petrol Turbo",           mod_category="ECU Tune",
         typical_hp_gain_pct=18,  typical_tq_gain_pct=22,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Skoda;Seat;BMW;Ford;Opel;Renault;Peugeot;Citroen;Mercedes",
         compatible_models="All turbocharged petrol",
         difficulty_level="Professional", typical_cost_usd=350,
         notes="Bolt-on safe. Remaps boost, ignition, fuelling. Typical 15-25% gain."),

    dict(mod_name="Stage 2 ECU Remap — Petrol Turbo",           mod_category="ECU Tune",
         typical_hp_gain_pct=32,  typical_tq_gain_pct=38,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Skoda;Seat;BMW;Ford;Opel",
         compatible_models="All turbocharged petrol",
         difficulty_level="Professional", typical_cost_usd=700,
         notes="Requires intake + downpipe minimum. 25-45% gain."),

    dict(mod_name="Stage 3 ECU Remap — Petrol Turbo",           mod_category="ECU Tune",
         typical_hp_gain_pct=55,  typical_tq_gain_pct=60,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Mitsubishi",
         compatible_models="Turbocharged petrol with upgraded turbo",
         difficulty_level="Professional", typical_cost_usd=1200,
         notes="Requires upgraded turbo, injectors, intercooler, fuelling. 50-70% gain."),

    dict(mod_name="Stage 1 ECU Remap — Diesel TDI/TDCi/CDI",   mod_category="ECU Tune",
         typical_hp_gain_pct=22,  typical_tq_gain_pct=30,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Mercedes;Ford;Opel;Renault;Peugeot",
         compatible_models="All turbodiesel",
         difficulty_level="Professional", typical_cost_usd=300,
         notes="Diesel responds very well to remapping. 20-30% hp, 25-40% tq."),

    dict(mod_name="Stage 2 ECU Remap — Diesel",                 mod_category="ECU Tune",
         typical_hp_gain_pct=42,  typical_tq_gain_pct=52,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Mercedes;Ford",
         compatible_models="All turbodiesel",
         difficulty_level="Professional", typical_cost_usd=600,
         notes="May require EGR/DPF delete. Check local laws."),

    dict(mod_name="DSG / TCU Remap",                             mod_category="ECU Tune",
         typical_hp_gain_pct=4,   typical_tq_gain_pct=10,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Skoda;Seat;BMW;Mercedes",
         compatible_models="DSG/DCT/ZF automatic gearbox models",
         difficulty_level="Professional", typical_cost_usd=300,
         notes="Higher torque limit, faster shifts. Usually done alongside ECU remap."),

    dict(mod_name="Hondata FlashPro — Honda",                    mod_category="ECU Tune",
         typical_hp_gain_pct=12,  typical_tq_gain_pct=10,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Honda",
         compatible_models="Civic Type R FK2/FK8;Civic Si;Integra",
         difficulty_level="DIY-capable", typical_cost_usd=695,
         notes="Full ECU access via OBD. Live tuning capable."),

    dict(mod_name="Cobb Accessport — Subaru/Porsche/Ford",       mod_category="ECU Tune",
         typical_hp_gain_pct=14,  typical_tq_gain_pct=18,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Subaru;Mitsubishi;Ford;Porsche;Nissan",
         compatible_models="WRX;WRX STI;Evo;Focus ST/RS;GT86",
         difficulty_level="DIY-capable", typical_cost_usd=700,
         notes="OTS maps available. Custom tuning via AP3. Most popular for Subaru."),

    # ── INTAKE ──────────────────────────────────────────────────────
    dict(mod_name="Cold Air Intake / Induction Kit",             mod_category="Intake",
         typical_hp_gain_pct=3,   typical_tq_gain_pct=2,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=150,
         notes="Best combined with ECU remap. Improves throttle response and sound."),

    dict(mod_name="Panel Air Filter Upgrade (K&N / Pipercross)", mod_category="Intake",
         typical_hp_gain_pct=1,   typical_tq_gain_pct=1,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=55,
         notes="Drop-in OEM housing filter. Minimal gains, very cheap."),

    dict(mod_name="Carbon Fibre Intake System",                  mod_category="Intake",
         typical_hp_gain_pct=5,   typical_tq_gain_pct=3,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Honda",
         compatible_models="Performance models",
         difficulty_level="DIY", typical_cost_usd=400,
         notes="Better heat insulation than aluminium intake pipes."),

    # ── EXHAUST ────────────────────────────────────────────────────
    dict(mod_name="Cat-Back Exhaust System",                     mod_category="Exhaust",
         typical_hp_gain_pct=3,   typical_tq_gain_pct=2,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=650,
         notes="After-cat exhaust. Main benefit is sound. Small power gain."),

    dict(mod_name="Turbo-Back Exhaust with High-Flow Cat",       mod_category="Exhaust",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=7,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Ford;Subaru;Mitsubishi",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=950,
         notes="Significant flow improvement. Decat downpipe + cat-back."),

    dict(mod_name="Downpipe / Decat Pipe",                       mod_category="Exhaust",
         typical_hp_gain_pct=6,   typical_tq_gain_pct=5,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Mitsubishi;Ford",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=350,
         notes="Removes catalyst restriction on turbo outlet. Needs remap for full benefit."),

    dict(mod_name="Sports Cat (200 cell) Downpipe",              mod_category="Exhaust",
         typical_hp_gain_pct=4,   typical_tq_gain_pct=4,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Ford;Subaru",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=280,
         notes="Road-legal alternative to full decat. Reduces back pressure significantly."),

    # ── INTERCOOLER ──────────────────────────────────────────────────────────
    dict(mod_name="Front-Mount Intercooler Upgrade (FMIC)",      mod_category="Intercooler",
         typical_hp_gain_pct=5,   typical_tq_gain_pct=5,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Ford;Opel;Mitsubishi",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=500,
         notes="Reduces IAT, more consistent power. Required for Stage 2+."),

    dict(mod_name="Top-Mount Intercooler Upgrade (TMIC)",        mod_category="Intercooler",
         typical_hp_gain_pct=4,   typical_tq_gain_pct=4,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Subaru;Mitsubishi;Saab",
         compatible_models="Impreza WRX/STI;Evo;9-3 Turbo",
         difficulty_level="Professional", typical_cost_usd=450,
         notes="Reduces heat soak. Popular on Subaru EJ engines."),

    dict(mod_name="Intercooler Spray Kit (Water Mist)",          mod_category="Intercooler",
         typical_hp_gain_pct=3,   typical_tq_gain_pct=3,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="Turbocharged models",
         difficulty_level="DIY", typical_cost_usd=120,
         notes="Sprays water mist on intercooler. Reduces IAT by 5-15°C. Cheap effective solution."),

    # ── TURBO ─────────────────────────────────────────────────────────────
    dict(mod_name="Hybrid Turbocharger Upgrade",                 mod_category="Turbo Upgrade",
         typical_hp_gain_pct=25,  typical_tq_gain_pct=30,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Mitsubishi",
         compatible_models="IS20;IS38;N54;N55;EJ257;4B11T",
         difficulty_level="Professional", typical_cost_usd=1200,
         notes="Upgraded compressor/turbine in OEM housing. Bolt-on. Needs remap + fuelling."),

    dict(mod_name="Full Turbo Swap (Larger Turbocharger)",       mod_category="Turbo Upgrade",
         typical_hp_gain_pct=55,  typical_tq_gain_pct=50,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Subaru;Mitsubishi;Ford",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=2500,
         notes="Full turbo swap. Requires fuelling, FMIC, remap, possibly forged internals."),

    dict(mod_name="IS38 Turbo Upgrade (for IS20 cars)",          mod_category="Turbo Upgrade",
         typical_hp_gain_pct=30,  typical_tq_gain_pct=35,
         typical_hp_gain_abs=70,  typical_tq_gain_abs=90,
         compatible_makes="VW;Audi;Skoda;Seat",
         compatible_models="Golf GTI Mk7;Polo GTI;A3 2.0 TFSI;Leon Cupra",
         difficulty_level="Professional", typical_cost_usd=900,
         notes="IS38 is a direct bolt-on to IS20 cars. Common upgrade on Mk7 GTI. Needs remap."),

    # ── FUEL SYSTEM ─────────────────────────────────────────────────────
    dict(mod_name="High-Flow Fuel Injectors",                    mod_category="Fuelling",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="High-power turbocharged",
         difficulty_level="Professional", typical_cost_usd=400,
         notes="Enabling mod — allows higher power levels. No standalone gain."),

    dict(mod_name="High-Pressure Fuel Pump (HPFP) Upgrade",     mod_category="Fuelling",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru",
         compatible_models="Direct injection turbocharged",
         difficulty_level="Professional", typical_cost_usd=300,
         notes="Required for E30/E85 tunes or Stage 3+. Enabling mod."),

    dict(mod_name="E85 / Ethanol Flex Fuel Conversion",         mod_category="Fuelling",
         typical_hp_gain_pct=18,  typical_tq_gain_pct=15,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Subaru;Mitsubishi;BMW",
         compatible_models="Turbocharged with upgraded fuelling",
         difficulty_level="Professional", typical_cost_usd=600,
         notes="E85 allows more boost and timing advance. Requires HPFP + injector upgrades."),

    # ── SUPERCHARGER ─────────────────────────────────────────────
    dict(mod_name="Supercharger Kit — Roots/Twin-Screw",         mod_category="Supercharger",
         typical_hp_gain_pct=42,  typical_tq_gain_pct=38,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Ford;Chevrolet;BMW;Toyota;Honda;Mazda",
         compatible_models="Naturally aspirated V6/V8",
         difficulty_level="Professional", typical_cost_usd=4200,
         notes="Bolt-on complete kit. Linear power delivery. No lag. Popular on NA V8s."),

    dict(mod_name="Supercharger Pulley Upgrade",                 mod_category="Supercharger",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=7,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Ford;Chevrolet;Toyota",
         compatible_models="Supercharged models (Mustang GT500, Camaro ZL1, Corolla GR)",
         difficulty_level="Professional", typical_cost_usd=300,
         notes="Smaller pulley = more boost. Cheap and effective on existing supercharged cars."),

    # ── NITROUS ────────────────────────────────────────────────
    dict(mod_name="Nitrous Oxide System — Dry Kit (50 hp)",      mod_category="Nitrous",
         typical_hp_gain_pct=None, typical_tq_gain_pct=None,
         typical_hp_gain_abs=50,  typical_tq_gain_abs=40,
         compatible_makes="All",
         compatible_models="Naturally aspirated",
         difficulty_level="Professional", typical_cost_usd=600,
         notes="50 hp shot. Temporary when activated. Needs fresh plugs and timing retard."),

    dict(mod_name="Nitrous Oxide System — Wet Kit (100 hp)",     mod_category="Nitrous",
         typical_hp_gain_pct=None, typical_tq_gain_pct=None,
         typical_hp_gain_abs=100, typical_tq_gain_abs=80,
         compatible_makes="All",
         compatible_models="Strong engine required",
         difficulty_level="Professional", typical_cost_usd=950,
         notes="Injects fuel + N2O simultaneously. Requires forged internals for reliability."),

    # ── WATER / METHANOL ──────────────────────────────────────────────────────
    dict(mod_name="Water/Methanol Injection Kit",                mod_category="Fuel Additives",
         typical_hp_gain_pct=10,  typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=500,
         notes="Cools intake charge, allows more boost/timing. Popular on IS20/IS38 Stage 2+."),

    # ── SUSPENSION ────────────────────────────────────────────────────────────
    dict(mod_name="Coilover Suspension Kit",                     mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1100,
         notes="Improves handling, not power. Adjustable ride height and damping."),

    dict(mod_name="Sway Bar / Anti-Roll Bar Upgrade",            mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=220,
         notes="Reduces body roll significantly. No power gain."),

    dict(mod_name="Strut Brace / Chassis Brace",                 mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=150,
         notes="Improves chassis rigidity. Better turn-in and steering feel."),

    # ── BRAKES ──────────────────────────────────────────────────────────────
    dict(mod_name="Big Brake Kit (BBK) — 4-piston",             mod_category="Brakes",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1600,
         notes="Larger rotors + multi-piston calipers. Needed for 300+ hp track use."),

    # ── ENGINE — MECHANICAL ─────────────────────────────────────────────────────
    dict(mod_name="Forged Pistons + Connecting Rods",            mod_category="Engine Internals",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=2200,
         notes="Enabling mod for 400+ hp builds. Required for high boost reliability."),

    dict(mod_name="Ported and Polished Cylinder Head",           mod_category="Engine Internals",
         typical_hp_gain_pct=10,  typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1300,
         notes="Improves airflow through head. Best combined with cam upgrades."),

    dict(mod_name="Performance Camshaft Upgrade",                mod_category="Engine Internals",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=6,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Honda;Toyota;Subaru;BMW;Ford;Mazda",
         compatible_models="High-rev naturally aspirated",
         difficulty_level="Professional", typical_cost_usd=850,
         notes="More lift/duration. Moves power band higher. Needs remap. Popular on K-series/B-series."),

    dict(mod_name="Stroker Kit (Increased Displacement)",        mod_category="Engine Internals",
         typical_hp_gain_pct=15,  typical_tq_gain_pct=20,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Honda;Subaru;Ford;Chevrolet",
         compatible_models="B16;B18;EJ20;EJ25;LS",
         difficulty_level="Professional", typical_cost_usd=3500,
         notes="Increases engine displacement via longer stroke crankshaft."),

    # ── BOOST CONTROL ───────────────────────────────────────────────
    dict(mod_name="Electronic Boost Controller",                 mod_category="Boost Control",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=250,
         notes="Precise boost control. Safer than manual boost controller. Needs custom tune."),

    dict(mod_name="Blow-Off Valve / Diverter Valve Upgrade",    mod_category="Boost Control",
         typical_hp_gain_pct=1,   typical_tq_gain_pct=1,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Subaru;Mitsubishi;BMW",
         compatible_models="Turbocharged models",
         difficulty_level="DIY", typical_cost_usd=130,
         notes="Prevents compressor surge. Reliability + sound improvement."),

    # ── DRIVETRAIN / TRANSMISSION ──────────────────────────────────────────────────────────
    dict(mod_name="Limited Slip Differential (LSD)",             mod_category="Drivetrain",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1600,
         notes="Reduces wheelspin, improves exit traction. Essential for high-power FWD/RWD."),

    dict(mod_name="Short-Throw Shifter",                         mod_category="Transmission",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="Manual transmission",
         difficulty_level="DIY", typical_cost_usd=160,
         notes="Reduces gear throw by 30-50%. No power gain."),

    dict(mod_name="Performance Clutch Kit",                      mod_category="Transmission",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="Manual transmission",
         difficulty_level="Professional", typical_cost_usd=700,
         notes="Handles higher torque for Stage 2+ builds. Required at 350+ Nm."),

    # ── AERODYNAMICS ────────────────────────────────────────────────────────
    dict(mod_name="Front Splitter",                              mod_category="Aerodynamics",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=200,
         notes="Increases front downforce. Effect significant only above 150 km/h."),

    dict(mod_name="Rear Wing / Spoiler",                         mod_category="Aerodynamics",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=320,
         notes="Adds rear downforce. Can slightly increase drag."),

    dict(mod_name="Diffuser (Rear Underbody)",                   mod_category="Aerodynamics",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=350,
         notes="Reduces drag and lift at rear. Noticeable above 180 km/h."),
]


def get_static_mods() -> list[dict]:
    rows = []
    for mod in STATIC_MODS:
        r = mod.copy()
        r["mod_id"]     = _mod_id(mod["mod_name"], "static")
        r["source_url"] = "static_curated"
        rows.append(r)
    log.info(f"Static mods: {len(rows)} entries")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
COLS = [
    "mod_id", "mod_name", "mod_category",
    "typical_hp_gain_pct", "typical_tq_gain_pct",
    "typical_hp_gain_abs", "typical_tq_gain_abs",
    "compatible_makes", "compatible_models",
    "difficulty_level", "typical_cost_usd",
    "notes", "source_url",
]


def scrape_all(static_only=False) -> pd.DataFrame:
    rows = get_static_mods()
    if not static_only:
        rows.extend(scrape_apr())
        rows.extend(scrape_racechip())

    df = pd.DataFrame(rows)
    df.drop_duplicates(subset=["mod_name", "compatible_models"], keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)
    for c in COLS:
        if c not in df.columns:
            df[c] = None
    df = df[COLS]
    df.to_csv(OUTPUT_CSV, index=False)
    log.info(f"Saved {len(df)} modifications → {OUTPUT_CSV}")
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--static-only", action="store_true",
                    help="Only use built-in static list (no internet needed)")
    args = ap.parse_args()
    df = scrape_all(static_only=args.static_only)
    print(f"\nDone. {len(df)} modifications in {OUTPUT_CSV}")
    print(df[["mod_name","mod_category","typical_hp_gain_pct",
              "compatible_makes"]].to_string(index=False))
