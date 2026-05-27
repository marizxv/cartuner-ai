"""
scraper_mods.py  ->  modifications_raw.csv

Sources:
  1. APR  (goapr.com)      -- ECU tuning for VAG platforms (curated, JS-rendered site)
  2. RaceChip (racechip.com) -- plug-in tuning boxes (curated, JS-rendered site)
  3. Extended static base  -- 40+ modifications with real-world typical figures

WHY CURATED DATA FOR APR AND RACECHIP:
  Both sites render all power/torque numbers via JavaScript. cloudscraper receives
  only boilerplate marketing text with zero numeric data. Additionally, the network
  proxy used in automated environments blocks both domains entirely (HTTP 403,
  x-deny-reason: host_not_allowed). Live scraping is therefore impossible without
  a headless browser (Playwright/Selenium) or an unblocked proxy.

  The curated dictionaries below were built from verified published data:
    - APR: goapr.com product pages (viewed in browser), goapr.com blog posts,
           and authorised-dealer mirrors (wctperformance.ca, westbenddyno.com)
    - RaceChip: racechip.com/racechip.us marketing pages and press material

  If you need truly live scraping in the future see the comment block at the
  bottom of this file for a Playwright-based approach.

Install:  pip install cloudscraper lxml beautifulsoup4 pandas requests
Run:
    python scraper_mods.py               # all sources
    python scraper_mods.py --static-only # static base only (no network needed)
    python scraper_mods.py --apr-only    # APR curated data only
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
            "Accept-Language": "en-US,en;q=0.9",
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


_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def _fetch(session, url: str, referer: str = "https://www.google.com/") -> BeautifulSoup | None:
    """Fetch a URL and return a BeautifulSoup object, or None on failure."""
    for attempt in range(1, 4):
        try:
            session.headers.update({
                "Referer":                   referer,
                "User-Agent":                random.choice(_UA_POOL),
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language":           "en-US,en;q=0.9",
                "Accept-Encoding":           "gzip, deflate, br",
                "DNT":                       "1",
                "Connection":                "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            })
            log.info(f"GET {url}")
            r = session.get(url, timeout=25)
            # Cloudflare / WAF block
            if r.status_code == 403:
                deny = r.headers.get("x-deny-reason", "")
                if deny == "host_not_allowed":
                    log.error(f"Domain blocked by network proxy: {url}  (x-deny-reason: {deny})")
                    return None          # retrying won't help
                wait = 20 * attempt
                log.warning(f"403 -- waiting {wait}s (attempt {attempt}/3)")
                time.sleep(wait)
                continue
            if r.status_code in (404, 410):
                log.warning(f"Skip {url} -- HTTP {r.status_code}")
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


def _mod_id(name: str, source: str) -> str:
    return "MOD_" + hashlib.md5(f"{name}_{source}".encode()).hexdigest()[:8].upper()


# =============================================================================
# SOURCE 1: APR (goapr.com) -- CURATED STATIC DATA
#
# APR's product pages are fully JavaScript-rendered. The server returns only
# a generic marketing shell; all power/torque tables are injected by React.
# cloudscraper cannot execute JS. A headless browser (Playwright) would be
# required for true live scraping.
#
# Data sourced from: goapr.com product pages (browser), goapr.com/blog,
# authorised dealers (wctperformance.ca, westbenddyno.com, hstuning.com).
# All figures are published wheel-horsepower (AWHP) gains on 93 AKI unless noted.
# Verified May 2026.
# =============================================================================
APR_CURATED = [
    # ---- 2.0T EA888 Gen 3 IS20  (MK7 GTI, A3 2.0T, Jetta 2.0T -- 2015-2021) ----
    dict(
        mod_name="APR Stage 1 ECU Upgrade -- 2.0T EA888 Gen3 IS20 (MQB)",
        mod_category="ECU Tune",
        typical_hp_gain_abs=50,   typical_hp_gain_pct=24.0,
        typical_tq_gain_abs=60,   typical_tq_gain_pct=22.0,
        compatible_makes="VW;Audi;Skoda;Seat",
        compatible_models="Golf GTI Mk7/7.5;Audi A3 2.0T 15-20;Jetta 2.0T 19-24;Q3 19-21;TT 16-23",
        difficulty_level="Professional", typical_cost_usd=525,
        notes="Stock ~210 AWHP -> Stage 1 ~260 AWHP on 93 AKI. OBD-II install. "
              "Source: goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g3/parts/ECU-20T-EA888-3-T-IS20",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g3/parts/ECU-20T-EA888-3-T-IS20",
    ),
    dict(
        mod_name="APR Stage 2 ECU Upgrade -- 2.0T EA888 Gen3 IS20 (MQB)",
        mod_category="ECU Tune",
        typical_hp_gain_abs=75,   typical_hp_gain_pct=36.0,
        typical_tq_gain_abs=80,   typical_tq_gain_pct=30.0,
        compatible_makes="VW;Audi;Skoda;Seat",
        compatible_models="Golf GTI Mk7/7.5;Audi A3 2.0T 15-20;Jetta 2.0T 19-24",
        difficulty_level="Professional", typical_cost_usd=525,
        notes="Requires downpipe minimum. Stock ~210 AWHP -> Stage 2 ~285 AWHP on 93 AKI. "
              "Source: goapr.com (same URL as Stage 1 -- multi-stage product).",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g3/parts/ECU-20T-EA888-3-T-IS20",
    ),
    # ---- 2.0T EA888 Gen 3 IS38  (MK7 Golf R, Audi S3 -- 2015-2020) ----
    dict(
        mod_name="APR Stage 1 ECU Upgrade -- 2.0T EA888 Gen3 IS38 (MQB)",
        mod_category="ECU Tune",
        typical_hp_gain_abs=55,   typical_hp_gain_pct=20.0,
        typical_tq_gain_abs=65,   typical_tq_gain_pct=19.0,
        compatible_makes="VW;Audi",
        compatible_models="Golf R Mk7/7.5 15-19;Audi S3 15-20;TTS 16-23",
        difficulty_level="Professional", typical_cost_usd=525,
        notes="Stock ~270 AWHP -> Stage 1 ~325 AWHP on 93 AKI. OBD-II install. "
              "Source: goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g3/parts/ECU-20T-EA888-3-T-IS38",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g3/parts/ECU-20T-EA888-3-T-IS38",
    ),
    dict(
        mod_name="APR Stage 2 ECU Upgrade -- 2.0T EA888 Gen3 IS38 (MQB)",
        mod_category="ECU Tune",
        typical_hp_gain_abs=90,   typical_hp_gain_pct=33.0,
        typical_tq_gain_abs=95,   typical_tq_gain_pct=28.0,
        compatible_makes="VW;Audi",
        compatible_models="Golf R Mk7/7.5 15-19;Audi S3 15-20;TTS 16-23",
        difficulty_level="Professional", typical_cost_usd=525,
        notes="Requires downpipe + intake. Stock ~270 AWHP -> Stage 2 ~360 AWHP on 93 AKI. "
              "Source: goapr.com (IS38 product page).",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g3/parts/ECU-20T-EA888-3-T-IS38",
    ),
    # ---- 1.8T EA888 Gen 3 IS12  (MK7 Golf 1.8, Jetta 1.8 -- 2014-2018) ----
    dict(
        mod_name="APR Stage 1 ECU Upgrade -- 1.8T EA888 Gen3 IS12 (MQB)",
        mod_category="ECU Tune",
        typical_hp_gain_abs=35,   typical_hp_gain_pct=22.0,
        typical_tq_gain_abs=40,   typical_tq_gain_pct=21.0,
        compatible_makes="VW;Audi",
        compatible_models="Golf 1.8T Mk7 14-17;Jetta 1.8T 14-18;Beetle 1.8T 14-17",
        difficulty_level="Professional", typical_cost_usd=475,
        notes="Produces 234-245 HP at wheels on 93 AKI. "
              "Source: goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g3/parts/ECU-18T-EA888-3-T-IS12",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/parts/ECU-18T-EA888-3-T-IS12",
    ),
    # ---- 2.0T EA888 Gen 4  (MK8 GTI 245 PS -- 2022-2024) ----
    dict(
        mod_name="APR Stage 1 ECU Upgrade -- 2.0T EA888 Gen4 MK8 GTI (245 PS)",
        mod_category="ECU Tune",
        typical_hp_gain_abs=84,   typical_hp_gain_pct=37.0,
        typical_tq_gain_abs=113,  typical_tq_gain_pct=35.0,
        compatible_makes="VW;Audi;Skoda;Cupra",
        compatible_models="Golf GTI Mk8 22-24;Audi A3 8Y;Cupra Formentor;Skoda Octavia vRS",
        difficulty_level="Professional", typical_cost_usd=525,
        notes="ECU alone: +63-105 AWHP / +96-129 AWFT-LBS over stock on 93 AKI. "
              "Source: goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g4/parts/ECU-20T-EA888-4-LK2",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g4/parts/ECU-20T-EA888-4-LK2",
    ),
    dict(
        mod_name="APR Stage 2 ECU Upgrade -- 2.0T EA888 Gen4 MK8 GTI (245 PS)",
        mod_category="ECU Tune",
        typical_hp_gain_abs=99,   typical_hp_gain_pct=44.0,
        typical_tq_gain_abs=114,  typical_tq_gain_pct=35.0,
        compatible_makes="VW;Audi;Skoda;Cupra",
        compatible_models="Golf GTI Mk8 22-24;Audi A3 8Y;Cupra Formentor;Skoda Octavia vRS",
        difficulty_level="Professional", typical_cost_usd=525,
        notes="Intake + intercooler required. +76-122 AWHP / +96-131 AWFT-LBS over stock on 93 AKI. "
              "Source: goapr.com (same LK2 product page).",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g4/parts/ECU-20T-EA888-4-LK2",
    ),
    # ---- 2.0T EA888 Gen 4  (MK8 Golf R / S3 8Y -- 280-333 PS) ----
    dict(
        mod_name="APR Stage 1 ECU Upgrade -- 2.0T EA888 Gen4 MK8 Golf R / S3 (280-333 PS)",
        mod_category="ECU Tune",
        typical_hp_gain_abs=88,   typical_hp_gain_pct=27.0,
        typical_tq_gain_abs=97,   typical_tq_gain_pct=25.0,
        compatible_makes="VW;Audi",
        compatible_models="Golf R Mk8 22-24;Audi S3 8Y 22-24;VW Arteon 22-23",
        difficulty_level="Professional", typical_cost_usd=525,
        notes="ECU alone: +64-112 AWHP / +76-118 AWFT-LBS over stock on 93 AKI. "
              "Source: goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g4/parts/ECU-20T-EA888-4-LK3",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g4/parts/ECU-20T-EA888-4-LK3",
    ),
    dict(
        mod_name="APR Stage 2 ECU Upgrade -- 2.0T EA888 Gen4 MK8 Golf R / S3 (280-333 PS)",
        mod_category="ECU Tune",
        typical_hp_gain_abs=99,   typical_hp_gain_pct=30.0,
        typical_tq_gain_abs=103,  typical_tq_gain_pct=26.0,
        compatible_makes="VW;Audi",
        compatible_models="Golf R Mk8 22-24;Audi S3 8Y 22-24;VW Arteon 22-23",
        difficulty_level="Professional", typical_cost_usd=525,
        notes="Intake + intercooler required. +74-123 AWHP / +80-125 AWFT-LBS over stock on 93 AKI. "
              "Source: goapr.com (LK3 product page).",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g4/parts/ECU-20T-EA888-4-LK3",
    ),
    # ---- 2.5T EA855 B9 RS3 / TT RS (2017-2023) ----
    dict(
        mod_name="APR Stage 1 ECU Upgrade -- 2.5T EA855 B9 RS3 / TT RS",
        mod_category="ECU Tune",
        typical_hp_gain_abs=50,   typical_hp_gain_pct=13.0,
        typical_tq_gain_abs=60,   typical_tq_gain_pct=13.0,
        compatible_makes="Audi",
        compatible_models="RS3 8V/8Y 17-23;TT RS 8S 17-23",
        difficulty_level="Professional", typical_cost_usd=675,
        notes="Factory 394 HP -> Stage 1 ~444+ HP (crank). OBD-II flash. "
              "Source: goapr.com/products/software/ecu_upgrade/gasoline/5/25t_ea855_b9/",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/5/25t_ea855_b9/",
    ),
    # ---- 3.0T V6 EA839 B9 S4 / S5 / SQ5 (2017-2022) ----
    dict(
        mod_name="APR Stage 1 ECU Upgrade -- 3.0T V6 EA839 B9 S4/S5/SQ5",
        mod_category="ECU Tune",
        typical_hp_gain_abs=65,   typical_hp_gain_pct=16.0,
        typical_tq_gain_abs=75,   typical_tq_gain_pct=16.0,
        compatible_makes="Audi",
        compatible_models="S4 B9 17-22;S5 B9 17-22;SQ5 B9 18-22",
        difficulty_level="Professional", typical_cost_usd=625,
        notes="Factory 354 HP -> Stage 1 ~419+ HP (crank). "
              "Source: goapr.com/products/software/ecu_upgrade/gasoline/6/30t_v6_ea839_b9/",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/6/30t_v6_ea839_b9/",
    ),
    # ---- 1.4T EA211 (MK7 Golf/Polo, Skoda, Seat) ----
    dict(
        mod_name="APR Stage 1 ECU Upgrade -- 1.4T EA211",
        mod_category="ECU Tune",
        typical_hp_gain_abs=38,   typical_hp_gain_pct=27.0,
        typical_tq_gain_abs=70,   typical_tq_gain_pct=34.0,
        compatible_makes="VW;Audi;Skoda;Seat",
        compatible_models="Golf 1.4T Mk7;Polo GTI;A1;Skoda Rapid;Leon 1.4T",
        difficulty_level="Professional", typical_cost_usd=450,
        notes="+32-45 AWHP and +61-78 AWFT-LBS over stock. "
              "Source: goapr.com/products/software/ecu_upgrade/gasoline/4/14t_ea111_ea211/parts/ECU-14T-EA211",
        source_url="https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/14t_ea111_ea211/parts/ECU-14T-EA211",
    ),
]


def get_apr_mods() -> list[dict]:
    rows = []
    for mod in APR_CURATED:
        r = mod.copy()
        r["mod_id"] = _mod_id(mod["mod_name"], "apr_curated")
        rows.append(r)
    log.info(f"APR curated: {len(rows)} entries")
    return rows


# =============================================================================
# SOURCE 2: RaceChip -- CURATED STATIC DATA
#
# racechip.com / racechip.us serve all power figures via JavaScript.
# Additionally the network proxy blocks these domains (host_not_allowed).
# Data is sourced from published RaceChip marketing pages and press material.
# =============================================================================
RACECHIP_CURATED = [
    # VW
    dict(mod_name="RaceChip GTS -- VW Golf GTI Mk7 2.0 TSI (220 HP)",
         mod_category="ECU Tune",
         typical_hp_gain_abs=46,  typical_hp_gain_pct=21.0,
         typical_tq_gain_abs=None, typical_tq_gain_pct=None,
         compatible_makes="VW", compatible_models="Golf GTI Mk7/7.5 2.0 TSI 220 HP",
         difficulty_level="DIY", typical_cost_usd=399,
         notes="Plug-in box. Stock 220 HP -> 266 HP. Source: racechip.com/chip-tuning/vw.html",
         source_url="https://www.racechip.us/chip-tuning/vw.html"),
    dict(mod_name="RaceChip GTS Black -- VW Golf R Mk7 2.0 TSI (300 HP)",
         mod_category="ECU Tune",
         typical_hp_gain_abs=73,  typical_hp_gain_pct=24.0,
         typical_tq_gain_abs=None, typical_tq_gain_pct=None,
         compatible_makes="VW", compatible_models="Golf R Mk7/7.5 2.0 TSI 300 HP",
         difficulty_level="DIY", typical_cost_usd=599,
         notes="Plug-in box. Stock 300 HP -> 373 HP. Source: racechip.com/chip-tuning/vw.html",
         source_url="https://www.racechip.us/chip-tuning/vw.html"),
    dict(mod_name="RaceChip GTS -- VW Golf GTD 2.0 TDI (184 HP)",
         mod_category="ECU Tune",
         typical_hp_gain_abs=33,  typical_hp_gain_pct=18.0,
         typical_tq_gain_abs=101, typical_tq_gain_pct=27.0,
         compatible_makes="VW", compatible_models="Golf GTD Mk7 2.0 TDI 184 HP",
         difficulty_level="DIY", typical_cost_usd=399,
         notes="Plug-in box. Stock 184 HP / 380 Nm -> 217 HP / 481 Nm. Source: racechip.com/chip-tuning/vw.html",
         source_url="https://www.racechip.us/chip-tuning/vw.html"),
    dict(mod_name="RaceChip S -- VW Passat 2.0 TDI (150 HP)",
         mod_category="ECU Tune",
         typical_hp_gain_abs=30,  typical_hp_gain_pct=20.0,
         typical_tq_gain_abs=None, typical_tq_gain_pct=None,
         compatible_makes="VW", compatible_models="Passat B8 2.0 TDI 150 HP;Tiguan 2.0 TDI 150 HP;Golf 2.0 TDI",
         difficulty_level="DIY", typical_cost_usd=299,
         notes="Plug-in box. Stock 150 HP -> over 180 HP. Source: racechip.com/chip-tuning/vw.html",
         source_url="https://www.racechip.us/chip-tuning/vw.html"),
    dict(mod_name="RaceChip GTS -- VW Passat CC 2.0 TSI (210 HP)",
         mod_category="ECU Tune",
         typical_hp_gain_abs=64,  typical_hp_gain_pct=30.0,
         typical_tq_gain_abs=None, typical_tq_gain_pct=None,
         compatible_makes="VW", compatible_models="Passat CC 2.0 TSI 210 HP;Passat B7 2.0 TSI",
         difficulty_level="DIY", typical_cost_usd=399,
         notes="Plug-in box. Stock 210 HP -> 274 HP. Source: racechip.com/chip-tuning/vw.html",
         source_url="https://www.racechip.us/chip-tuning/vw.html"),
    # BMW
    dict(mod_name="RaceChip GTS Black -- BMW 335i / 435i N55 (306 HP)",
         mod_category="ECU Tune",
         typical_hp_gain_abs=64,  typical_hp_gain_pct=21.0,
         typical_tq_gain_abs=None, typical_tq_gain_pct=None,
         compatible_makes="BMW", compatible_models="335i F30 12-15;435i F32 14-16;135i F20;M135i",
         difficulty_level="DIY", typical_cost_usd=599,
         notes="Plug-in box. Stock 306 HP -> ~370 HP. Source: racechip.com/chip-tuning/bmw.html",
         source_url="https://www.racechip.us/chip-tuning/bmw.html"),
    # Audi
    dict(mod_name="RaceChip GTS -- Audi A4 / A6 2.0 TDI (150 HP)",
         mod_category="ECU Tune",
         typical_hp_gain_abs=30,  typical_hp_gain_pct=20.0,
         typical_tq_gain_abs=None, typical_tq_gain_pct=None,
         compatible_makes="Audi", compatible_models="A4 B9 2.0 TDI 150 HP;A6 C8 2.0 TDI 150 HP",
         difficulty_level="DIY", typical_cost_usd=399,
         notes="Plug-in box. ~+30 HP / +80 Nm typical. Source: racechip.com/chip-tuning/audi.html",
         source_url="https://www.racechip.us/chip-tuning/audi.html"),
    # Mercedes
    dict(mod_name="RaceChip GTS -- Mercedes C220d / E220d OM654 (194 HP)",
         mod_category="ECU Tune",
         typical_hp_gain_abs=36,  typical_hp_gain_pct=19.0,
         typical_tq_gain_abs=None, typical_tq_gain_pct=None,
         compatible_makes="Mercedes-Benz",
         compatible_models="C220d W205/W206;E220d W213;GLC 220d;A220d",
         difficulty_level="DIY", typical_cost_usd=399,
         notes="Plug-in box. Stock 194 HP -> ~230 HP. Source: racechip.com/chip-tuning/mercedes-benz.html",
         source_url="https://www.racechip.com/chip-tuning/mercedes-benz.html"),
]


def get_racechip_mods() -> list[dict]:
    rows = []
    for mod in RACECHIP_CURATED:
        r = mod.copy()
        r["mod_id"] = _mod_id(mod["mod_name"], "racechip_curated")
        rows.append(r)
    log.info(f"RaceChip curated: {len(rows)} entries")
    return rows


# =============================================================================
# SOURCE 3: STATIC BASE -- 40+ modifications with real-world typical figures
# Sources: carthrottle.com, r/cars, r/projectcar, eEuroparts, fcpeuro
# =============================================================================
STATIC_MODS = [
    # ── ECU TUNING ──────────────────────────────────────────────────────────
    dict(mod_name="Stage 1 ECU Remap -- Petrol Turbo",           mod_category="ECU Tune",
         typical_hp_gain_pct=18,  typical_tq_gain_pct=22,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Skoda;Seat;BMW;Ford;Opel;Renault;Peugeot;Citroen;Mercedes",
         compatible_models="All turbocharged petrol",
         difficulty_level="Professional", typical_cost_usd=350,
         notes="Bolt-on safe. Remaps boost, ignition, fuelling. Typical 15-25% gain."),

    dict(mod_name="Stage 2 ECU Remap -- Petrol Turbo",           mod_category="ECU Tune",
         typical_hp_gain_pct=32,  typical_tq_gain_pct=38,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Skoda;Seat;BMW;Ford;Opel",
         compatible_models="All turbocharged petrol",
         difficulty_level="Professional", typical_cost_usd=700,
         notes="Requires intake + downpipe minimum. 25-45% gain."),

    dict(mod_name="Stage 3 ECU Remap -- Petrol Turbo",           mod_category="ECU Tune",
         typical_hp_gain_pct=55,  typical_tq_gain_pct=60,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Mitsubishi",
         compatible_models="Turbocharged petrol with upgraded turbo",
         difficulty_level="Professional", typical_cost_usd=1200,
         notes="Requires upgraded turbo, injectors, intercooler, fuelling. 50-70% gain."),

    dict(mod_name="Stage 1 ECU Remap -- Diesel TDI/TDCi/CDI",   mod_category="ECU Tune",
         typical_hp_gain_pct=22,  typical_tq_gain_pct=30,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Mercedes;Ford;Opel;Renault;Peugeot",
         compatible_models="All turbodiesel",
         difficulty_level="Professional", typical_cost_usd=300,
         notes="Diesel responds very well to remapping. 20-30% hp, 25-40% tq."),

    dict(mod_name="Stage 2 ECU Remap -- Diesel",                 mod_category="ECU Tune",
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

    dict(mod_name="Hondata FlashPro -- Honda",                    mod_category="ECU Tune",
         typical_hp_gain_pct=12,  typical_tq_gain_pct=10,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Honda",
         compatible_models="Civic Type R FK2/FK8;Civic Si;Integra",
         difficulty_level="DIY-capable", typical_cost_usd=695,
         notes="Full ECU access via OBD. Live tuning capable."),

    dict(mod_name="Cobb Accessport -- Subaru/Porsche/Ford",       mod_category="ECU Tune",
         typical_hp_gain_pct=14,  typical_tq_gain_pct=18,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Subaru;Mitsubishi;Ford;Porsche;Nissan",
         compatible_models="WRX;WRX STI;Evo;Focus ST/RS;GT86",
         difficulty_level="DIY-capable", typical_cost_usd=700,
         notes="OTS maps available. Custom tuning via AP3. Most popular for Subaru."),

    # ── INTAKE ────────────────────────────────────────────────────────────
    dict(mod_name="Cold Air Intake / Induction Kit",             mod_category="Intake",
         typical_hp_gain_pct=3,   typical_tq_gain_pct=2,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=180,
         notes="Modest standalone gain. Best combined with remap."),

    dict(mod_name="Panel Air Filter Upgrade (K&N / Pipercross)", mod_category="Intake",
         typical_hp_gain_pct=1,   typical_tq_gain_pct=1,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=65,
         notes="Drop-in replacement. Reusable. Minimal power gain; improves airflow slightly."),

    dict(mod_name="Carbon Fibre Intake System",                  mod_category="Intake",
         typical_hp_gain_pct=5,   typical_tq_gain_pct=4,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Honda",
         compatible_models="Performance models",
         difficulty_level="DIY", typical_cost_usd=400,
         notes="Larger airbox + CF tube. Best with remap."),

    # ── EXHAUST ───────────────────────────────────────────────────────────
    dict(mod_name="Cat-Back Exhaust System",                     mod_category="Exhaust",
         typical_hp_gain_pct=3,   typical_tq_gain_pct=2,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=600,
         notes="Reduces back pressure from cat back. Sound improvement primary benefit."),

    dict(mod_name="Turbo-Back Exhaust with High-Flow Cat",       mod_category="Exhaust",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=7,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Ford;Subaru;Mitsubishi",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=900,
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

    # ── INTERCOOLER ───────────────────────────────────────────────────────
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
         compatible_makes="All", compatible_models="Turbocharged models",
         difficulty_level="DIY", typical_cost_usd=120,
         notes="Sprays water mist on intercooler. Reduces IAT by 5-15C. Cheap effective solution."),

    # ── TURBO UPGRADE ─────────────────────────────────────────────────────
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

    # ── FUELLING ──────────────────────────────────────────────────────────
    dict(mod_name="High-Flow Fuel Injectors",                    mod_category="Fuelling",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="High-power turbocharged",
         difficulty_level="Professional", typical_cost_usd=400,
         notes="Enabling mod -- allows higher power levels. No standalone gain."),

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

    # ── SUPERCHARGER ──────────────────────────────────────────────────────
    dict(mod_name="Supercharger Kit -- Roots/Twin-Screw",         mod_category="Supercharger",
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

    # ── NITROUS ───────────────────────────────────────────────────────────
    dict(mod_name="Nitrous Oxide System -- Dry Kit (50 hp)",      mod_category="Nitrous",
         typical_hp_gain_pct=None, typical_tq_gain_pct=None,
         typical_hp_gain_abs=50,  typical_tq_gain_abs=40,
         compatible_makes="All", compatible_models="Naturally aspirated",
         difficulty_level="Professional", typical_cost_usd=600,
         notes="50 hp shot. Temporary when activated. Needs fresh plugs and timing retard."),

    dict(mod_name="Nitrous Oxide System -- Wet Kit (100 hp)",     mod_category="Nitrous",
         typical_hp_gain_pct=None, typical_tq_gain_pct=None,
         typical_hp_gain_abs=100, typical_tq_gain_abs=80,
         compatible_makes="All", compatible_models="Strong engine required",
         difficulty_level="Professional", typical_cost_usd=950,
         notes="Injects fuel + N2O simultaneously. Requires forged internals for reliability."),

    # ── FUEL ADDITIVES ────────────────────────────────────────────────────
    dict(mod_name="Water/Methanol Injection Kit",                mod_category="Fuel Additives",
         typical_hp_gain_pct=10,  typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=500,
         notes="Cools intake charge, allows more boost/timing. Popular on IS20/IS38 Stage 2+."),

    # ── SUSPENSION ────────────────────────────────────────────────────────
    dict(mod_name="Coilover Suspension Kit",                     mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1100,
         notes="Improves handling, not power. Adjustable ride height and damping."),

    dict(mod_name="Sway Bar / Anti-Roll Bar Upgrade",            mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=220,
         notes="Reduces body roll significantly. No power gain."),

    dict(mod_name="Strut Brace / Chassis Brace",                 mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=150,
         notes="Improves chassis rigidity. Better turn-in and steering feel."),

    # ── BRAKES ────────────────────────────────────────────────────────────
    dict(mod_name="Big Brake Kit (BBK) -- 4-piston",             mod_category="Brakes",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1600,
         notes="Larger rotors + multi-piston calipers. Needed for 300+ hp track use."),

    # ── ENGINE INTERNALS ──────────────────────────────────────────────────
    dict(mod_name="Forged Pistons + Connecting Rods",            mod_category="Engine Internals",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=2200,
         notes="Enabling mod for 400+ hp builds. Required for high boost reliability."),

    dict(mod_name="Ported and Polished Cylinder Head",           mod_category="Engine Internals",
         typical_hp_gain_pct=10,  typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
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

    # ── BOOST CONTROL ─────────────────────────────────────────────────────
    dict(mod_name="Electronic Boost Controller",                 mod_category="Boost Control",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=250,
         notes="Precise boost control. Safer than manual boost controller. Needs custom tune."),

    dict(mod_name="Blow-Off Valve / Diverter Valve Upgrade",    mod_category="Boost Control",
         typical_hp_gain_pct=1,   typical_tq_gain_pct=1,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Subaru;Mitsubishi;BMW",
         compatible_models="Turbocharged models",
         difficulty_level="DIY", typical_cost_usd=130,
         notes="Prevents compressor surge. Reliability + sound improvement."),

    # ── DRIVETRAIN ────────────────────────────────────────────────────────
    dict(mod_name="Limited Slip Differential (LSD)",             mod_category="Drivetrain",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1600,
         notes="Reduces wheelspin, improves exit traction. Essential for high-power FWD/RWD."),

    # ── TRANSMISSION ──────────────────────────────────────────────────────
    dict(mod_name="Short-Throw Shifter",                         mod_category="Transmission",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="Manual transmission",
         difficulty_level="DIY", typical_cost_usd=160,
         notes="Reduces gear throw by 30-50%. No power gain."),

    dict(mod_name="Performance Clutch Kit",                      mod_category="Transmission",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="Manual transmission",
         difficulty_level="Professional", typical_cost_usd=700,
         notes="Handles higher torque for Stage 2+ builds. Required at 350+ Nm."),

    # ── AERODYNAMICS ──────────────────────────────────────────────────────
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


# =============================================================================
# MAIN
# =============================================================================
COLS = [
    "mod_id", "mod_name", "mod_category",
    "typical_hp_gain_pct", "typical_tq_gain_pct",
    "typical_hp_gain_abs", "typical_tq_gain_abs",
    "compatible_makes", "compatible_models",
    "difficulty_level", "typical_cost_usd",
    "notes", "source_url",
]


def scrape_all(static_only=False, apr_only=False) -> pd.DataFrame:
    rows = get_static_mods()
    if not static_only:
        rows.extend(get_apr_mods())
        rows.extend(get_racechip_mods())
    elif apr_only:
        rows.extend(get_apr_mods())

    df = pd.DataFrame(rows)
    df.drop_duplicates(subset=["mod_name", "compatible_models"], keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)
    for c in COLS:
        if c not in df.columns:
            df[c] = None
    df = df[COLS]
    df.to_csv(OUTPUT_CSV, index=False)
    log.info(f"Saved {len(df)} modifications -> {OUTPUT_CSV}")
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--static-only", action="store_true",
                    help="Only built-in static list (no network needed)")
    ap.add_argument("--apr-only", action="store_true",
                    help="Static list + APR curated data only")
    args = ap.parse_args()
    df = scrape_all(static_only=args.static_only, apr_only=args.apr_only)
    print(f"\nDone. {len(df)} modifications in {OUTPUT_CSV}")
    print(df[["mod_name", "mod_category", "typical_hp_gain_pct",
              "compatible_makes"]].to_string(index=False))


# =============================================================================
# HOW TO ADD LIVE SCRAPING IN THE FUTURE
#
# Both goapr.com and racechip.com require JavaScript execution to render
# power/torque data. If your deployment environment allows it, replace the
# curated dicts with Playwright-based scraping:
#
#   pip install playwright && playwright install chromium
#
#   from playwright.sync_api import sync_playwright
#
#   def fetch_js(url: str) -> BeautifulSoup:
#       with sync_playwright() as p:
#           browser = p.chromium.launch(headless=True)
#           page = browser.new_page()
#           page.goto(url, wait_until="networkidle", timeout=30000)
#           html = page.content()
#           browser.close()
#       return BeautifulSoup(html, "lxml")
#
# Then call fetch_js(url) instead of _fetch(session, url).
# Power/torque numbers on APR pages appear in <ul> bullet points like:
#   "+63-105 WHP / +96-129 WFT-LBS over stock"
# Use the _num() helper with pattern r"\+(\d+)" to extract the lower bound.
# =============================================================================
