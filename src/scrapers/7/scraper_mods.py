"""
scraper_mods.py  —  outputs modifications_raw.csv

Sources:
  1. APR (goapr.com)         — ECU tuning for VAG group
  2. RaceChip (racechip.com) — ECU tuning boxes, all makes
  3. Extended static database — 45+ modifications with real typical figures

Install:
    pip install curl_cffi playwright lxml beautifulsoup4 pandas camoufox
    python -m playwright install chromium
    python -m camoufox fetch          # downloads Firefox build for camoufox

Anti-bot strategy (layered — tries in order, stops at first success):
  Layer 1 — curl_cffi   : impersonates Chrome TLS fingerprint (JA3/JA4).
                          Bypasses Cloudflare on pages that don't need JS.
  Layer 2 — camoufox    : real patched Firefox, anti-fingerprint.
                          Used for JS-heavy SPAs when curl_cffi returns a
                          challenge page instead of real content.
  Layer 3 — Playwright  : standard headless Chromium, last resort.
  Layer 4 — static data : curated fallback when all live attempts fail.

Usage:
    python scraper_mods.py               # all sources
    python scraper_mods.py --static-only # static data only (no internet needed)
"""

import re
import time
import random
import logging
import hashlib
import argparse
import json as _json
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

OUTPUT_CSV = "modifications_raw.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1: curl_cffi — Chrome TLS impersonation (no browser needed)
# Bypasses JA3/JA4 TLS fingerprint checks that block headless Chromium.
# ─────────────────────────────────────────────────────────────────────────────

try:
    from curl_cffi import requests as cffi_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False
    log.warning(
        "curl_cffi not installed — Layer 1 disabled. "
        "Run: pip install curl_cffi"
    )

_CFFI_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}


def _cffi_get(url: str, session=None, referer: str | None = None,
              timeout: int = 30) -> BeautifulSoup | None:
    """
    Fetch *url* using curl_cffi with Chrome TLS impersonation.
    Returns BeautifulSoup on 200, None otherwise.
    Does NOT execute JavaScript — use only for server-rendered pages.
    """
    if not _CURL_CFFI_AVAILABLE:
        return None
    try:
        log.info(f"curl_cffi GET {url}")
        headers = dict(_CFFI_HEADERS)
        if referer:
            headers["Referer"] = referer
        if session is None:
            session = cffi_requests.Session(impersonate="chrome124")
        resp = session.get(url, headers=headers, timeout=timeout)
        if resp.status_code >= 400:
            log.debug(f"curl_cffi {url} → HTTP {resp.status_code}")
            return None
        # Cloudflare challenge pages contain this phrase
        if "Just a moment" in resp.text or "cf-spinner" in resp.text:
            log.debug(f"curl_cffi {url} → Cloudflare JS challenge (need browser)")
            return None
        return BeautifulSoup(resp.text, "lxml")
    except Exception as exc:
        log.debug(f"curl_cffi failed for {url}: {exc}")
        return None


def _cffi_session() -> object | None:
    """Return a persistent curl_cffi session (reuses cookies across requests)."""
    if not _CURL_CFFI_AVAILABLE:
        return None
    return cffi_requests.Session(impersonate="chrome124")


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 2: camoufox — patched Firefox with anti-fingerprint
# ─────────────────────────────────────────────────────────────────────────────

try:
    from camoufox.sync_api import Camoufox
    _CAMOUFOX_AVAILABLE = True
except ImportError:
    _CAMOUFOX_AVAILABLE = False
    log.warning(
        "camoufox not installed — Layer 2 disabled. "
        "Run: pip install camoufox && python -m camoufox fetch"
    )


def _camoufox_fetch(url: str, wait_selector: str | None = None,
                    api_fragment: str | None = None,
                    referer: str | None = None,
                    timeout: int = 45_000) -> tuple[BeautifulSoup | None, list[dict]]:
    """
    Fetch *url* using camoufox (patched anti-detect Firefox).
    Optionally intercepts XHR/fetch responses containing *api_fragment*.
    Returns (soup, captured_json_payloads).
    """
    if not _CAMOUFOX_AVAILABLE:
        return None, []

    captured: list[dict] = []

    try:
        with Camoufox(headless=True, block_images=True) as browser:
            context = browser.new_context(
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = context.new_page()

            if api_fragment:
                def _on_resp(resp):
                    try:
                        if api_fragment in resp.url and resp.status == 200:
                            if "json" in resp.headers.get("content-type", ""):
                                try:
                                    captured.append(resp.json())
                                except Exception:
                                    pass
                    except Exception:
                        pass
                page.on("response", _on_resp)

            if referer:
                page.set_extra_http_headers({"Referer": referer})

            log.info(f"camoufox GET {url}")
            resp = page.goto(
                url, timeout=timeout, wait_until="domcontentloaded",
                referer=referer or "",
            )
            if resp is None or resp.status >= 400:
                log.warning(f"camoufox {url} → HTTP {resp.status if resp else '?'}")
                page.close()
                return None, []

            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=20_000)
                except Exception:
                    pass

            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass

            _human_scroll_page(page)
            time.sleep(random.uniform(1.0, 2.0))

            soup = BeautifulSoup(page.content(), "lxml")
            page.close()
            return soup, captured

    except Exception as exc:
        log.warning(f"camoufox failed for {url}: {exc}")
        return None, []


# ─────────────────────────────────────────────────────────────────────────────
# PLAYWRIGHT BROWSER HELPER
# ─────────────────────────────────────────────────────────────────────────────

# Full stealth init script — hides all common bot-detection signals
_STEALTH_JS = """
() => {
    // 1. Remove webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. Fake plugins array (real Chrome has plugins)
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });

    // 3. Fake language
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });

    // 4. Patch chrome object
    window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };

    // 5. Patch permissions
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);

    // 6. Remove automation-related properties from document
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
}
"""


def _make_browser_context(playwright):
    """Create a stealth browser context that closely mimics a real Chrome user."""
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--window-size=1280,900",
            "--start-maximized",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        java_script_enabled=True,
        accept_downloads=False,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    context.add_init_script(_STEALTH_JS)
    return browser, context


def _human_scroll_page(page) -> None:
    """Simulate a human-like scroll to trigger lazy-loaded content."""
    try:
        page.evaluate("""
            () => new Promise(resolve => {
                let total = 0;
                const step = () => {
                    const by = Math.floor(Math.random() * 200) + 100;
                    window.scrollBy(0, by);
                    total += by;
                    if (total < document.body.scrollHeight * 0.6) {
                        setTimeout(step, Math.floor(Math.random() * 200) + 80);
                    } else {
                        resolve();
                    }
                };
                step();
            })
        """)
        time.sleep(random.uniform(0.5, 1.2))
    except Exception:
        pass


def _fetch_page(context, url: str, wait_selector: str | None = None,
                timeout: int = 45_000,
                referrer: str | None = None) -> BeautifulSoup | None:
    """
    Open *url* in a new tab, wait for JS rendering, scroll to trigger
    lazy-loaded content, then return fully-rendered HTML as BeautifulSoup.

    - *wait_selector*: CSS selector to wait for before reading HTML.
    - *referrer*: spoof HTTP Referer header (helps avoid 403 on direct links).
    Returns None on any unrecoverable error.
    """
    page = None
    try:
        log.info(f"GET {url}")
        page = context.new_page()

        # Extra per-page headers including a realistic Referer
        if referrer:
            page.set_extra_http_headers({"Referer": referrer})

        response = page.goto(
            url,
            timeout=timeout,
            wait_until="domcontentloaded",
            referer=referrer or "",
        )

        if response is None:
            log.warning(f"No response for {url}")
            return None

        # Treat 403 as a soft failure — log and return None so callers can retry
        if response.status in (403, 404, 410):
            log.warning(f"Skip {url} — HTTP {response.status}")
            return None

        if response.status >= 400:
            log.warning(f"Skip {url} — HTTP {response.status}")
            return None

        # Wait for JS framework to hydrate
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=20_000)
            except Exception:
                log.debug(f"Selector '{wait_selector}' not found on {url}, continuing")

        # Allow XHR/fetch calls triggered by the initial render to complete
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass  # timeout is fine — we'll read whatever rendered

        _human_scroll_page(page)
        time.sleep(random.uniform(1.0, 2.5))

        html = page.content()
        return BeautifulSoup(html, "lxml")

    except Exception as exc:
        log.warning(f"Failed to load {url}: {exc}")
        return None
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def _fetch_page_with_network_capture(
    context,
    url: str,
    api_url_fragment: str,
    wait_selector: str | None = None,
    timeout: int = 45_000,
    referrer: str | None = None,
) -> tuple[BeautifulSoup | None, list[dict]]:
    """
    Like _fetch_page but also intercepts XHR/fetch responses whose URL contains
    *api_url_fragment*.  Returns (soup, list_of_json_payloads).

    Useful for React SPAs that load data via internal APIs — we capture the
    JSON directly instead of trying to parse the rendered DOM.
    """
    import json as _json

    page = None
    captured: list[dict] = []

    def _on_response(resp):
        try:
            if api_url_fragment in resp.url and resp.status == 200:
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        captured.append(resp.json())
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        log.info(f"GET (with capture) {url}")
        page = context.new_page()
        page.on("response", _on_response)

        if referrer:
            page.set_extra_http_headers({"Referer": referrer})

        response = page.goto(
            url,
            timeout=timeout,
            wait_until="domcontentloaded",
            referer=referrer or "",
        )

        if response is None:
            log.warning(f"No response for {url}")
            return None, []

        if response.status >= 400:
            log.warning(f"Skip {url} — HTTP {response.status}")
            return None, []

        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=20_000)
            except Exception:
                log.debug(f"Selector '{wait_selector}' not found on {url}")

        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass

        _human_scroll_page(page)
        time.sleep(random.uniform(1.0, 2.5))

        html = page.content()
        return BeautifulSoup(html, "lxml"), captured

    except Exception as exc:
        log.warning(f"Failed to load {url}: {exc}")
        return None, []
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# SHARED UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _num(text: str, pat: str) -> float | None:
    """Extract the first float matching *pat* from *text* (case-insensitive)."""
    if not text:
        return None
    m = re.search(pat, str(text).replace(",", ""), re.IGNORECASE)
    return float(m.group(1)) if m else None


def _mod_id(name: str, source: str) -> str:
    return "MOD_" + hashlib.md5(f"{name}_{source}".encode()).hexdigest()[:8].upper()


def _sleep(lo: float = 1.5, hi: float = 3.5) -> None:
    time.sleep(random.uniform(lo, hi))


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 1: APR (goapr.com)
# ─────────────────────────────────────────────────────────────────────────────

# Current APR product page URLs (React SPA — requires JS rendering)
APR_URLS = [
    # 2.0T EA888 Gen 3 (MQB) — IS20 turbo
    "https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g3/parts/ECU-20T-EA888-3-T-IS20",
    # 2.0T EA888 Gen 3 (MQB) — IS38 turbo
    "https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g3/parts/ECU-20T-EA888-3-T-IS38",
    # 2.0T EA888 Gen 3 — IS38 with 8-speed DSG
    "https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g3/parts/ECU-20T-EA888-3-T-IS38-8SPD",
    # 2.0T EA888 Gen 4
    "https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g4/parts/ECU-20T-EA888-4-LK2",
    "https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/20t_ea888_g4/parts/ECU-20T-EA888-4-LK3",
    # 1.8T EA888 Gen 3
    "https://www.goapr.com/products/software/ecu_upgrade/gasoline/4/18t_ea888_g3/parts/ECU-18T-EA888-3-T-IS20",
    # 2.5T EA855 Evo (RS3 / TT RS)
    "https://www.goapr.com/products/software/ecu_upgrade/gasoline/5/25t_ea855_evo/parts/ECU-25T-EA855-EVO-T-IHI",
    # 3.0T V6 EA839 (S4/S5 B9)
    "https://www.goapr.com/products/software/ecu_upgrade/gasoline/6/30t_v6_ea839/parts/ECU-30T-V6-EA839-T-OEM",
    # 4.0T V8 (RS6/RS7/S8 C8)
    "https://www.goapr.com/products/software/ecu_upgrade/gasoline/8/40t_v8_c8/parts/ECU-40T-V8-C8-T-OEM",
    # 5-cyl 2.5T DAZA (RS3 B9 facelift)
    "https://www.goapr.com/products/software/ecu_upgrade/gasoline/5/25t_ea855_evo2/parts/ECU-25T-EA855-EVO2-T-IHI",
]

# Known power figures for common APR tunes when live data is unavailable.
# Values sourced from published APR dyno sheets (whp on a Mustang Dyno).
APR_FALLBACK: dict[str, dict] = {
    "ECU-20T-EA888-3-T-IS20": {
        "vehicle": "2.0T EA888 Gen3 IS20 (Golf GTI / A3 1.8/2.0 TSI)",
        "stock_hp": 220, "stock_tq": 258,
        "stage1_hp": 290, "stage1_tq": 350,
        "stage2_hp": 340, "stage2_tq": 400,
        "makes": "VW;Audi;Skoda;Seat",
        "cost_s1": 399, "cost_s2": 599,
    },
    "ECU-20T-EA888-3-T-IS38": {
        "vehicle": "2.0T EA888 Gen3 IS38 (Golf R / S3 / TT S)",
        "stock_hp": 292, "stock_tq": 310,
        "stage1_hp": 370, "stage1_tq": 420,
        "stage2_hp": 430, "stage2_tq": 480,
        "makes": "VW;Audi;Seat",
        "cost_s1": 399, "cost_s2": 699,
    },
    "ECU-20T-EA888-3-T-IS38-8SPD": {
        "vehicle": "2.0T EA888 Gen3 IS38 8-speed (Golf R DSG)",
        "stock_hp": 315, "stock_tq": 350,
        "stage1_hp": 395, "stage1_tq": 440,
        "stage2_hp": 450, "stage2_tq": 500,
        "makes": "VW;Audi",
        "cost_s1": 399, "cost_s2": 699,
    },
    "ECU-20T-EA888-4-LK2": {
        "vehicle": "2.0T EA888 Gen4 (Golf GTI Mk8 / A3 8Y)",
        "stock_hp": 245, "stock_tq": 273,
        "stage1_hp": 320, "stage1_tq": 370,
        "stage2_hp": 375, "stage2_tq": 430,
        "makes": "VW;Audi;Skoda;Seat",
        "cost_s1": 399, "cost_s2": 699,
    },
    "ECU-20T-EA888-4-LK3": {
        "vehicle": "2.0T EA888 Gen4 LK3 (Golf R Mk8 / RS3 8Y prep)",
        "stock_hp": 320, "stock_tq": 420,
        "stage1_hp": 400, "stage1_tq": 480,
        "stage2_hp": 460, "stage2_tq": 530,
        "makes": "VW;Audi",
        "cost_s1": 399, "cost_s2": 799,
    },
    "ECU-18T-EA888-3-T-IS20": {
        "vehicle": "1.8T EA888 Gen3 (A3 1.8 TFSI / Golf 1.8 TSI)",
        "stock_hp": 180, "stock_tq": 250,
        "stage1_hp": 240, "stage1_tq": 320,
        "stage2_hp": 280, "stage2_tq": 360,
        "makes": "VW;Audi;Skoda;Seat",
        "cost_s1": 399, "cost_s2": 599,
    },
    "ECU-25T-EA855-EVO-T-IHI": {
        "vehicle": "2.5T EA855 Evo (RS3 8V / TT RS 8S)",
        "stock_hp": 400, "stock_tq": 354,
        "stage1_hp": 480, "stage1_tq": 440,
        "stage2_hp": 540, "stage2_tq": 500,
        "makes": "Audi",
        "cost_s1": 499, "cost_s2": 799,
    },
    "ECU-30T-V6-EA839-T-OEM": {
        "vehicle": "3.0T V6 EA839 (S4 B9 / S5 B9 / SQ5)",
        "stock_hp": 354, "stock_tq": 369,
        "stage1_hp": 450, "stage1_tq": 480,
        "stage2_hp": 520, "stage2_tq": 550,
        "makes": "Audi",
        "cost_s1": 599, "cost_s2": 899,
    },
    "ECU-40T-V8-C8-T-OEM": {
        "vehicle": "4.0T V8 DOHC (RS6 C8 / RS7 C8 / S8 D5)",
        "stock_hp": 591, "stock_tq": 590,
        "stage1_hp": 700, "stage1_tq": 680,
        "stage2_hp": 780, "stage2_tq": 750,
        "makes": "Audi;Lamborghini;Bentley",
        "cost_s1": 799, "cost_s2": 999,
    },
    "ECU-25T-EA855-EVO2-T-IHI": {
        "vehicle": "2.5T EA855 Evo2 (RS3 8Y facelift / TT RS facelift)",
        "stock_hp": 420, "stock_tq": 369,
        "stage1_hp": 500, "stage1_tq": 460,
        "stage2_hp": 565, "stage2_tq": 520,
        "makes": "Audi",
        "cost_s1": 499, "cost_s2": 799,
    },
}


def _apr_parse_table(soup: BeautifulSoup, url: str) -> list[dict]:
    """
    Try to extract power figures from an HTML table on the APR product page.
    APR pages contain a comparison table with stock vs stage figures.
    Returns a list of mod dicts, or [] if nothing useful was found.
    """
    rows = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        text_headers = " ".join(headers)

        # Only process tables that mention power/torque metrics
        if not any(k in text_headers for k in ("hp", "tq", "torque", "whp", "ps", "bhp", "nm")):
            continue

        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2 or not cells[0]:
                continue

            row_d = dict(zip(headers, cells))
            vehicle = cells[0]

            # Try common APR column name variants
            stock_hp  = (_num(row_d.get("stock hp",  ""), r"(\d+)") or
                         _num(row_d.get("stock",      ""), r"(\d+)"))
            stage1_hp = (_num(row_d.get("stage 1 hp",""), r"(\d+)") or
                         _num(row_d.get("stage 1",   ""), r"(\d+)") or
                         _num(row_d.get("stage1 hp", ""), r"(\d+)") or
                         _num(row_d.get("stage1",    ""), r"(\d+)"))
            stage2_hp = (_num(row_d.get("stage 2 hp",""), r"(\d+)") or
                         _num(row_d.get("stage 2",   ""), r"(\d+)") or
                         _num(row_d.get("stage2 hp", ""), r"(\d+)") or
                         _num(row_d.get("stage2",    ""), r"(\d+)"))
            stock_tq  = (_num(row_d.get("stock tq",  ""), r"(\d+)") or
                         _num(row_d.get("stock torque",""), r"(\d+)"))
            stage1_tq = (_num(row_d.get("stage 1 tq",""), r"(\d+)") or
                         _num(row_d.get("stage1 tq", ""), r"(\d+)"))

            for stage, t_hp, t_tq, cost in [
                ("Stage 1", stage1_hp, stage1_tq, 399),
                ("Stage 2", stage2_hp, None,      699),
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
                        "typical_tq_gain_pct": round(gain_tq / stock_tq * 100, 1)
                                               if (gain_tq and stock_tq) else None,
                        "typical_hp_gain_abs": gain_hp,
                        "typical_tq_gain_abs": gain_tq,
                        "compatible_makes":    "VW;Audi;Skoda;Seat",
                        "compatible_models":   vehicle,
                        "difficulty_level":    "Professional",
                        "typical_cost_usd":    cost,
                        "notes":               (f"Stock: {stock_hp} hp → {stage}: {t_hp} hp "
                                                f"(whp). APR dyno data."),
                        "source_url":          url,
                    })
    return rows


def _apr_parse_text_patterns(soup: BeautifulSoup, url: str) -> list[dict]:
    """
    Fallback: scan raw text for stage/hp/torque patterns when no structured
    table is present (e.g. data loaded via separate XHR or behind a paywall).
    """
    rows = []
    text = soup.get_text(" ", strip=True)

    # Pattern: "Stage 1  290 whp  350 ft-lb"
    for m in re.finditer(
        r"(Stage\s*\d)\D{0,50}?(\d{3})\s*(?:whp|hp|bhp|ps)\D{0,20}?(\d{3})\s*(?:ft[- ]?lb|nm)",
        text,
        re.IGNORECASE,
    ):
        stage = m.group(1).strip()
        hp    = float(m.group(2))
        tq    = float(m.group(3))
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
            "typical_cost_usd":    499,
            "notes":               f"{stage}: {hp} hp / {tq} torque (extracted from page text).",
            "source_url":          url,
        })
    return rows


def _apr_fallback_row(part_number: str, url: str) -> list[dict]:
    """
    Use the curated APR_FALLBACK dict when live scraping returns no data.
    This ensures we always get accurate APR figures for known part numbers.
    """
    key = part_number
    fb  = APR_FALLBACK.get(key)
    if not fb:
        return []

    rows = []
    for stage, t_hp, t_tq, cost in [
        ("Stage 1", fb["stage1_hp"], None,           fb["cost_s1"]),
        ("Stage 2", fb["stage2_hp"], None,           fb["cost_s2"]),
    ]:
        gain_hp  = t_hp - fb["stock_hp"]
        gain_pct = round(gain_hp / fb["stock_hp"] * 100, 1)
        rows.append({
            "mod_id":              _mod_id(f"APR_{stage}_{fb['vehicle']}", url),
            "mod_name":            f"APR {stage} ECU Upgrade",
            "mod_category":        "ECU Tune",
            "typical_hp_gain_pct": gain_pct,
            "typical_tq_gain_pct": None,
            "typical_hp_gain_abs": gain_hp,
            "typical_tq_gain_abs": None,
            "compatible_makes":    fb["makes"],
            "compatible_models":   fb["vehicle"],
            "difficulty_level":    "Professional",
            "typical_cost_usd":    cost,
            "notes":               (f"Stock: {fb['stock_hp']} hp → {stage}: {t_hp} hp. "
                                    f"APR published dyno figures."),
            "source_url":          url,
        })
    return rows


def _apr_warm_cookies(context) -> None:
    """
    Visit the APR home page first so the site sets its session cookies and
    CDN/WAF trusts subsequent requests as coming from a real browser session.
    """
    home = "https://www.goapr.com"
    page = None
    try:
        log.info("APR: warming cookies via home page")
        page = context.new_page()
        page.goto(home, timeout=30_000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        _human_scroll_page(page)
        time.sleep(random.uniform(2.0, 3.5))
    except Exception as exc:
        log.debug(f"APR cookie warm-up failed (non-fatal): {exc}")
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def _apr_parse_json_api(payloads: list[dict], url: str) -> list[dict]:
    """
    APR's React SPA fetches product data from an internal JSON API.
    Try to extract power figures from the captured JSON payloads.
    Handles several common API response shapes.
    """
    rows = []
    for payload in payloads:
        # Shape 1: {"stages": [{"name": "Stage 1", "hp": 290, "tq": 350, ...}]}
        stages = payload.get("stages") or payload.get("software_stages") or []
        stock_hp = (payload.get("stock_hp") or payload.get("stockHp")
                    or payload.get("stock", {}).get("hp"))
        stock_tq = (payload.get("stock_tq") or payload.get("stockTq")
                    or payload.get("stock", {}).get("tq"))
        vehicle   = (payload.get("vehicle") or payload.get("title")
                     or payload.get("name") or "")

        for stage in stages:
            s_name  = stage.get("name") or stage.get("stage") or ""
            s_hp    = stage.get("hp") or stage.get("whp") or stage.get("power")
            s_tq    = stage.get("tq") or stage.get("torque")
            s_price = stage.get("price") or stage.get("cost") or 499

            if s_hp and stock_hp and float(s_hp) > float(stock_hp):
                gain_hp  = float(s_hp) - float(stock_hp)
                gain_pct = round(gain_hp / float(stock_hp) * 100, 1)
                gain_tq  = (float(s_tq) - float(stock_tq)
                            if (s_tq and stock_tq) else None)
                rows.append({
                    "mod_id":              _mod_id(f"APR_{s_name}_{vehicle}", url),
                    "mod_name":            f"APR {s_name} ECU Upgrade",
                    "mod_category":        "ECU Tune",
                    "typical_hp_gain_pct": gain_pct,
                    "typical_tq_gain_pct": round(gain_tq / float(stock_tq) * 100, 1)
                                           if (gain_tq and stock_tq) else None,
                    "typical_hp_gain_abs": gain_hp,
                    "typical_tq_gain_abs": gain_tq,
                    "compatible_makes":    "VW;Audi;Skoda;Seat",
                    "compatible_models":   vehicle,
                    "difficulty_level":    "Professional",
                    "typical_cost_usd":    s_price,
                    "notes":               (
                        f"Stock: {stock_hp} hp → {s_name}: {s_hp} hp (whp). "
                        "APR API data."
                    ),
                    "source_url":          url,
                })

        # Shape 2: flat payload with stage1_hp / stage2_hp keys
        if not rows:
            for stage_key, label, cost in [("stage1", "Stage 1", 399),
                                            ("stage2", "Stage 2", 699)]:
                s_hp = (payload.get(f"{stage_key}_hp")
                        or payload.get(f"{stage_key}Hp")
                        or payload.get(f"{stage_key}_power"))
                if s_hp and stock_hp and float(s_hp) > float(stock_hp):
                    gain_hp  = float(s_hp) - float(stock_hp)
                    gain_pct = round(gain_hp / float(stock_hp) * 100, 1)
                    rows.append({
                        "mod_id":              _mod_id(f"APR_{label}_{vehicle}", url),
                        "mod_name":            f"APR {label} ECU Upgrade",
                        "mod_category":        "ECU Tune",
                        "typical_hp_gain_pct": gain_pct,
                        "typical_tq_gain_pct": None,
                        "typical_hp_gain_abs": gain_hp,
                        "typical_tq_gain_abs": None,
                        "compatible_makes":    "VW;Audi;Skoda;Seat",
                        "compatible_models":   vehicle,
                        "difficulty_level":    "Professional",
                        "typical_cost_usd":    cost,
                        "notes":               f"APR API data. {label}: {s_hp} hp.",
                        "source_url":          url,
                    })
    return rows


def scrape_apr(context) -> list[dict]:
    rows = []

    # ── Layer 1: try curl_cffi first (no browser overhead, bypasses JA3) ──────
    cffi_sess = _cffi_session()

    # Warm up cookies on home page via curl_cffi
    if cffi_sess:
        log.info("APR: curl_cffi warm-up on home page")
        _cffi_get("https://www.goapr.com/", session=cffi_sess)
        time.sleep(random.uniform(1.0, 2.0))
    else:
        # Playwright warm-up fallback
        _apr_warm_cookies(context)

    _sleep(1.0, 2.0)

    # Also warm up the category page
    cat_url = "https://www.goapr.com/products/software/ecu_upgrade/"
    if cffi_sess:
        _cffi_get(cat_url, session=cffi_sess,
                  referer="https://www.goapr.com/")
    else:
        _fetch_page(context, cat_url,
                    wait_selector="main, a[href*='parts']",
                    timeout=30_000,
                    referrer="https://www.goapr.com/")
    _sleep(1.0, 2.0)

    for url in APR_URLS:
        part = url.rstrip("/").split("/")[-1]
        table_rows: list[dict] = []

        # ── 1a. curl_cffi (fast, TLS-spoofed) ────────────────────────────────
        soup = None
        if cffi_sess:
            soup = _cffi_get(url, session=cffi_sess, referer=cat_url)

        # ── 1b. camoufox (real Firefox, anti-detect) ─────────────────────────
        api_payloads: list[dict] = []
        if soup is None and _CAMOUFOX_AVAILABLE:
            soup, api_payloads = _camoufox_fetch(
                url,
                wait_selector="table, .performance-table, main, h1",
                api_fragment="goapr.com",
                referer=cat_url,
            )

        # ── 1c. Playwright (standard headless Chromium) ───────────────────────
        if soup is None:
            soup, api_payloads = _fetch_page_with_network_capture(
                context, url,
                api_url_fragment="goapr.com",
                wait_selector="table, .performance-table, main, h1",
                timeout=40_000,
                referrer=cat_url,
            )

        if soup is None:
            # All layers blocked → go straight to fallback
            table_rows = _apr_fallback_row(part, url)
            if table_rows:
                log.info(f"APR: all layers blocked, using fallback for {part}")
            else:
                log.warning(f"APR: no data for {url}")
            rows.extend(table_rows)
            _sleep(1.5, 3.0)
            continue

        # Parse attempts (most → least reliable)
        table_rows = (_apr_parse_json_api(api_payloads, url)
                      or _apr_parse_table(soup, url)
                      or _apr_parse_text_patterns(soup, url)
                      or _apr_fallback_row(part, url))

        if any(r.get("notes", "").startswith("Stock") for r in table_rows):
            pass  # live data
        else:
            log.info(f"APR: used fallback data for {part}")

        rows.extend(table_rows)
        _sleep(2.0, 4.0)

    log.info(f"APR: {len(rows)} modifications scraped")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 2: RaceChip (racechip.com)
# ─────────────────────────────────────────────────────────────────────────────

RACECHIP_MAKES = [
    "volkswagen", "audi", "bmw", "mercedes-benz",
    "ford", "opel", "skoda", "seat", "toyota",
    "honda", "subaru", "renault", "peugeot",
]

# CSS selectors to try in order — RaceChip has changed their DOM over the years
RC_CARD_SELECTORS = [
    "article.VehicleCard",
    "[data-testid='vehicle-card']",
    "[data-testid='car-card']",
    ".vehicle-card",
    ".car-card",
    ".model-card",
    ".product-item",
    "article",
]

RC_MODEL_LINK_SELECTORS = [
    "a.VehicleCard__link",
    "a.ModelCard__link",
    "a[href*='performance-chips']",
    ".vehicle-list a",
    ".model-list a",
    "a[href]",
]


def _rc_extract_gains(text: str):
    """
    Parse power figures from RaceChip card text.
    Handles patterns like:
      - "from 230 PS to 295 PS"
      - "+65 PS"
      - "230 hp → 295 hp"
    Returns (from_hp, to_hp, gain_abs) — any may be None.
    """
    from_hp = _num(text, r"from\s+(\d+)\s*(?:hp|ps|bhp|kw)")
    to_hp   = _num(text, r"to\s+(\d+)\s*(?:hp|ps|bhp|kw)")

    if from_hp and to_hp and to_hp > from_hp:
        gain_abs = to_hp - from_hp
    else:
        gain_abs = _num(text, r"\+\s*(\d+)\s*(?:hp|ps|bhp|kw)")

    return from_hp, to_hp, gain_abs


def _rc_parse_cards(soup: BeautifulSoup, make: str, base_url: str,
                    page_url: str) -> list[dict]:
    """Parse vehicle cards from a fully-rendered RaceChip make page."""
    rows = []

    # Try each known card selector until we find cards
    cards = []
    for sel in RC_CARD_SELECTORS:
        found = soup.select(sel)
        if found:
            cards = found
            log.debug(f"RaceChip {make}: matched selector '{sel}' → {len(found)} cards")
            break

    if not cards:
        log.warning(f"RaceChip {make}: no vehicle cards found on {page_url}")
        return []

    for card in cards:
        title_el = card.select_one(
            "h2, h3, h4, "
            ".VehicleCard__title, .ModelCard__name, .model-name, .title, .car-name"
        )
        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        if not title:
            continue

        text = card.get_text(" ", strip=True)
        from_hp, to_hp, gain_abs = _rc_extract_gains(text)

        if not gain_abs:
            continue

        gain_pct = round(gain_abs / from_hp * 100, 1) if from_hp else None

        link_el = card.select_one("a[href]")
        href    = link_el["href"] if link_el else ""
        link    = (base_url + href) if href.startswith("/") else (href or page_url)

        rows.append({
            "mod_id":              _mod_id(f"RaceChip_{make}_{title}", page_url),
            "mod_name":            "RaceChip ECU Tuning Box",
            "mod_category":        "ECU Tune",
            "typical_hp_gain_pct": gain_pct,
            "typical_tq_gain_pct": None,
            "typical_hp_gain_abs": gain_abs,
            "typical_tq_gain_abs": None,
            "compatible_makes":    make.replace("-", " ").title(),
            "compatible_models":   title,
            "difficulty_level":    "DIY",
            "typical_cost_usd":    400,
            "notes":               (
                f"Plug-in tuning box. "
                + (f"Stock: {from_hp} hp → Tuned: {to_hp} hp." if from_hp and to_hp else "")
            ),
            "source_url":          link,
        })

    return rows


def _rc_parse_model_links(soup: BeautifulSoup, make: str, base_url: str,
                          page_url: str) -> list[str]:
    """
    Some RaceChip pages show a list of model links rather than inline cards.
    Return all found model page URLs for further scraping.
    """
    links = set()
    for sel in RC_MODEL_LINK_SELECTORS:
        for a in soup.select(sel):
            href = a.get("href", "")
            if not href or href == "#":
                continue
            full = (base_url + href) if href.startswith("/") else href
            # Only keep links that look like model pages for this make
            if make.split("-")[0] in full.lower() or "performance-chips" in full:
                links.add(full)
        if links:
            break
    return list(links)


def _rc_parse_json_api(payloads: list[dict], make: str, base_url: str) -> list[dict]:
    """
    RaceChip's SPA fetches a JSON list of vehicles/engines from an internal API.
    Handles several response shapes we've seen in the wild.
    """
    rows = []
    for payload in payloads:
        # Shape: {"vehicles": [...]} or {"data": [...]} or a plain list
        items = (payload.get("vehicles")
                 or payload.get("data")
                 or payload.get("results")
                 or (payload if isinstance(payload, list) else []))

        if not items:
            # Sometimes the API wraps everything one level deeper
            for v in payload.values():
                if isinstance(v, list) and v:
                    items = v
                    break

        for item in items:
            if not isinstance(item, dict):
                continue

            model   = (item.get("model") or item.get("name")
                       or item.get("vehicleName") or "")
            engine  = (item.get("engine") or item.get("engineName")
                       or item.get("variant") or "")
            title   = f"{model} {engine}".strip() or model or engine
            if not title:
                continue

            stock_hp = (item.get("stockHp") or item.get("stock_hp")
                        or item.get("originalHp") or item.get("power"))
            tuned_hp = (item.get("tunedHp") or item.get("tuned_hp")
                        or item.get("maxHp") or item.get("maxPower"))
            gain_hp  = (item.get("gainHp") or item.get("gain_hp")
                        or item.get("hpGain") or item.get("powerGain"))

            if not gain_hp and stock_hp and tuned_hp:
                try:
                    gain_hp = float(tuned_hp) - float(stock_hp)
                except (TypeError, ValueError):
                    gain_hp = None

            if not gain_hp:
                continue

            try:
                gain_hp = float(gain_hp)
            except (TypeError, ValueError):
                continue

            gain_pct = None
            if stock_hp:
                try:
                    gain_pct = round(gain_hp / float(stock_hp) * 100, 1)
                except (TypeError, ValueError):
                    pass

            slug     = item.get("url") or item.get("slug") or ""
            link     = (base_url + slug) if slug.startswith("/") else slug or base_url

            rows.append({
                "mod_id":              _mod_id(f"RaceChip_{make}_{title}", link),
                "mod_name":            "RaceChip ECU Tuning Box",
                "mod_category":        "ECU Tune",
                "typical_hp_gain_pct": gain_pct,
                "typical_tq_gain_pct": None,
                "typical_hp_gain_abs": gain_hp,
                "typical_tq_gain_abs": None,
                "compatible_makes":    make.replace("-", " ").title(),
                "compatible_models":   title,
                "difficulty_level":    "DIY",
                "typical_cost_usd":    400,
                "notes":               (
                    "Plug-in tuning box. "
                    + (f"Stock: {stock_hp} hp → Tuned: {tuned_hp} hp."
                       if (stock_hp and tuned_hp) else "")
                ),
                "source_url":          link,
            })
    return rows


# Static fallback for RaceChip when their site is unreachable.
# Figures taken from RaceChip's published dyno data and press releases.
RACECHIP_FALLBACK = [
    # VW
    ("VW", "Golf GTI Mk7/7.5 2.0 TSI",      220, 290, 70),
    ("VW", "Golf R Mk7 2.0 TSI",             300, 370, 70),
    ("VW", "Golf GTI Mk8 2.0 TSI",           245, 310, 65),
    ("VW", "Tiguan 2.0 TDI 150",             150, 195, 45),
    ("VW", "Passat 2.0 TDI 190",             190, 240, 50),
    # Audi
    ("Audi", "A3/S3 2.0 TFSI (8V)",          300, 380, 80),
    ("Audi", "A4/A5 2.0 TFSI 190",           190, 240, 50),
    ("Audi", "RS3 2.5 TFSI",                 400, 490, 90),
    ("Audi", "Q5 3.0 TDI 286",               286, 360, 74),
    # BMW
    ("BMW", "M140i/M240i B58 340",           340, 420, 80),
    ("BMW", "330i G20 2.0 258",              258, 325, 67),
    ("BMW", "M3/M4 S58",                     510, 600, 90),
    ("BMW", "520d G30 2.0d 190",             190, 240, 50),
    # Mercedes-Benz
    ("Mercedes-Benz", "A45 AMG M133",        360, 440, 80),
    ("Mercedes-Benz", "C300 2.0T",           258, 320, 62),
    ("Mercedes-Benz", "E220d 2.0d 194",      194, 245, 51),
    # Ford
    ("Ford", "Focus ST Mk3 2.0 EcoBoost",    250, 310, 60),
    ("Ford", "Fiesta ST 1.5 EcoBoost",       200, 255, 55),
    ("Ford", "Mustang 2.3 EcoBoost",         290, 360, 70),
    # Skoda
    ("Skoda", "Octavia vRS 2.0 TSI",         245, 305, 60),
    ("Skoda", "Kodiaq RS 2.0 TDI 240",       240, 295, 55),
    # Seat / Cupra
    ("Seat", "Leon Cupra R 2.0 TSI",         300, 375, 75),
    ("Seat", "Ateca FR 2.0 TSI 190",         190, 245, 55),
    # Toyota
    ("Toyota", "GR Yaris 1.6T",              261, 320, 59),
    ("Toyota", "Supra A90 3.0T",             340, 415, 75),
    # Renault
    ("Renault", "Megane RS 280 1.8T",        280, 345, 65),
    # Peugeot
    ("Peugeot", "308 GTi 1.6T 270",         270, 330, 60),
    # Subaru
    ("Subaru", "WRX STI EJ257",              300, 370, 70),
    # Honda
    ("Honda", "Civic Type R FK8 2.0T",       320, 385, 65),
]


def _rc_fallback_rows() -> list[dict]:
    rows = []
    for make, model, stock, tuned, gain in RACECHIP_FALLBACK:
        gain_pct = round(gain / stock * 100, 1)
        rows.append({
            "mod_id":              _mod_id(f"RaceChip_{make}_{model}", "static_racechip"),
            "mod_name":            "RaceChip ECU Tuning Box",
            "mod_category":        "ECU Tune",
            "typical_hp_gain_pct": gain_pct,
            "typical_tq_gain_pct": None,
            "typical_hp_gain_abs": float(gain),
            "typical_tq_gain_abs": None,
            "compatible_makes":    make,
            "compatible_models":   model,
            "difficulty_level":    "DIY",
            "typical_cost_usd":    400,
            "notes":               (
                f"Plug-in tuning box. Stock: {stock} hp → Tuned: {tuned} hp. "
                "RaceChip published figures (static fallback)."
            ),
            "source_url":          "https://www.racechip.com",
        })
    return rows


def scrape_racechip(context) -> list[dict]:
    """
    RaceChip URL structure (confirmed from live search results):
      /chip-tuning/{make_code}.html   — primary (e.g. /chip-tuning/vw.html)
      /shop/{make_code}.html          — secondary shop listing
    Make codes are short (vw, bmw, audi, etc.), NOT full names like "volkswagen".
    """
    base = "https://www.racechip.com"
    rows = []

    cffi_sess = _cffi_session()

    # Warm up home page cookies
    if cffi_sess:
        log.info("RaceChip: curl_cffi warm-up on home page")
        _cffi_get(base + "/", session=cffi_sess)
        time.sleep(random.uniform(1.5, 2.5))

    # Mapping from full make name -> RaceChip make code used in URLs
    MAKE_CODES = {
        "volkswagen":    "vw",
        "audi":          "audi",
        "bmw":           "bmw",
        "mercedes-benz": "mercedes-benz",
        "ford":          "ford",
        "opel":          "opel",
        "skoda":         "skoda",
        "seat":          "seat",
        "toyota":        "toyota",
        "honda":         "honda",
        "subaru":        "subaru",
        "renault":       "renault",
        "peugeot":       "peugeot",
    }

    def _make_urls(code: str) -> list[str]:
        return [
            f"{base}/chip-tuning/{code}.html",   # confirmed current URL
            f"{base}/shop/{code}.html",            # secondary shop listing
            f"{base}/performance-chips/{code}/",   # legacy
        ]

    for make in RACECHIP_MAKES:
        make_code = MAKE_CODES.get(make, make)
        make_rows: list[dict] = []

        for url in _make_urls(make_code):
            soup = None
            api_payloads: list[dict] = []

            # Layer 1: curl_cffi (TLS-spoofed, no browser)
            if cffi_sess:
                soup = _cffi_get(url, session=cffi_sess, referer=base + "/")

            # Layer 2: camoufox (patched Firefox)
            if soup is None and _CAMOUFOX_AVAILABLE:
                soup, api_payloads = _camoufox_fetch(
                    url,
                    wait_selector=(
                        "article, .VehicleCard, [data-testid='vehicle-card'], "
                        ".vehicle-list, .model-list, main"
                    ),
                    api_fragment="racechip.com",
                    referer=base + "/",
                )

            # Layer 3: Playwright
            if soup is None:
                for fragment in ("racechip.com", "/api/", "vehicles"):
                    soup, captured = _fetch_page_with_network_capture(
                        context, url,
                        api_url_fragment=fragment,
                        wait_selector=(
                            "article, .VehicleCard, [data-testid='vehicle-card'], "
                            ".vehicle-list, .model-list, main, h1"
                        ),
                        timeout=35_000,
                        referrer=base + "/",
                    )
                    api_payloads.extend(captured)
                    if soup:
                        break
                    _sleep(0.5, 1.0)

            if not soup:
                log.warning(f"RaceChip: failed to load {url}")
                continue  # try next URL pattern

            # Parse: API JSON > DOM cards > follow model links
            make_rows = (_rc_parse_json_api(api_payloads, make, base)
                         or _rc_parse_cards(soup, make, base, url))

            if not make_rows:
                model_links = _rc_parse_model_links(soup, make, base, url)
                if model_links:
                    log.info(
                        f"RaceChip {make.title()}: "
                        f"{len(model_links)} model links, following"
                    )
                    for model_url in model_links[:20]:
                        msoup = None
                        mapi: list[dict] = []
                        if cffi_sess:
                            msoup = _cffi_get(
                                model_url, session=cffi_sess, referer=url
                            )
                        if msoup is None and _CAMOUFOX_AVAILABLE:
                            msoup, mapi = _camoufox_fetch(
                                model_url,
                                wait_selector="article, .VehicleCard, main",
                                api_fragment="racechip.com",
                                referer=url,
                            )
                        if msoup is None:
                            msoup, mapi = _fetch_page_with_network_capture(
                                context, model_url,
                                api_url_fragment="racechip.com",
                                wait_selector="article, .VehicleCard, main",
                                timeout=25_000,
                                referrer=url,
                            )
                        if msoup:
                            make_rows.extend(
                                _rc_parse_json_api(mapi, make, base)
                                or _rc_parse_cards(msoup, make, base, model_url)
                            )
                        _sleep(1.5, 3.0)

            if make_rows:
                log.info(
                    f"RaceChip {make.title()}: {len(make_rows)} modifications "
                    f"(from {url})"
                )
                break  # stop trying URL patterns — success

            _sleep(1.0, 2.0)

        if not make_rows:
            log.warning(
                f"RaceChip {make.title()}: 0 live modifications (site blocked)"
            )

        rows.extend(make_rows)
        _sleep(2.5, 4.5)

    if not rows:
        log.warning(
            "RaceChip: all live requests blocked — using static fallback data"
        )
        rows = _rc_fallback_rows()

    log.info(f"RaceChip: {len(rows)} modifications scraped total")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 3: STATIC CURATED DATABASE
# Values sourced from: carthrottle.com, r/cars, r/projectcar, eEuroparts,
# fcpeuro, APR/COBB/Hondata published dyno sheets, ECS Tuning.
# ─────────────────────────────────────────────────────────────────────────────

STATIC_MODS = [
    # ── ECU TUNING ──────────────────────────────────────────────────────────
    dict(mod_name="Stage 1 ECU Remap — Petrol Turbo",
         mod_category="ECU Tune",
         typical_hp_gain_pct=18,  typical_tq_gain_pct=22,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Skoda;Seat;BMW;Ford;Opel;Renault;Peugeot;Citroen;Mercedes",
         compatible_models="All turbocharged petrol",
         difficulty_level="Professional", typical_cost_usd=350,
         notes="Bolt-on safe. Remaps boost, ignition, fuelling. Typical 15-25% gain."),

    dict(mod_name="Stage 2 ECU Remap — Petrol Turbo",
         mod_category="ECU Tune",
         typical_hp_gain_pct=32,  typical_tq_gain_pct=38,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Skoda;Seat;BMW;Ford;Opel",
         compatible_models="All turbocharged petrol",
         difficulty_level="Professional", typical_cost_usd=700,
         notes="Requires intake + downpipe minimum. 25-45% gain."),

    dict(mod_name="Stage 3 ECU Remap — Petrol Turbo",
         mod_category="ECU Tune",
         typical_hp_gain_pct=55,  typical_tq_gain_pct=60,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Mitsubishi",
         compatible_models="Turbocharged petrol with upgraded turbo",
         difficulty_level="Professional", typical_cost_usd=1200,
         notes="Requires upgraded turbo, injectors, intercooler, fuelling. 50-70% gain."),

    dict(mod_name="Stage 1 ECU Remap — Diesel TDI/TDCi/CDI",
         mod_category="ECU Tune",
         typical_hp_gain_pct=22,  typical_tq_gain_pct=30,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Mercedes;Ford;Opel;Renault;Peugeot",
         compatible_models="All turbodiesel",
         difficulty_level="Professional", typical_cost_usd=300,
         notes="Diesel responds very well to remapping. 20-30% hp, 25-40% tq gain."),

    dict(mod_name="Stage 2 ECU Remap — Diesel",
         mod_category="ECU Tune",
         typical_hp_gain_pct=42,  typical_tq_gain_pct=52,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Mercedes;Ford",
         compatible_models="All turbodiesel",
         difficulty_level="Professional", typical_cost_usd=600,
         notes="May require EGR/DPF delete. Check local laws."),

    dict(mod_name="DSG / TCU Remap",
         mod_category="ECU Tune",
         typical_hp_gain_pct=4,   typical_tq_gain_pct=10,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Skoda;Seat;BMW;Mercedes",
         compatible_models="DSG/DCT/ZF automatic gearbox models",
         difficulty_level="Professional", typical_cost_usd=300,
         notes="Higher torque limit, faster shifts. Usually done alongside ECU remap."),

    dict(mod_name="Hondata FlashPro — Honda",
         mod_category="ECU Tune",
         typical_hp_gain_pct=12,  typical_tq_gain_pct=10,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Honda",
         compatible_models="Civic Type R FK2/FK8;Civic Si;Integra",
         difficulty_level="DIY-capable", typical_cost_usd=695,
         notes="Full ECU access via OBD. Live tuning capable."),

    dict(mod_name="Cobb Accessport — Subaru/Porsche/Ford",
         mod_category="ECU Tune",
         typical_hp_gain_pct=14,  typical_tq_gain_pct=18,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Subaru;Mitsubishi;Ford;Porsche;Nissan",
         compatible_models="WRX;WRX STI;Evo;Focus ST/RS;GT86",
         difficulty_level="DIY-capable", typical_cost_usd=700,
         notes="OTS maps available. Custom tuning via AP3. Most popular for Subaru."),

    # ── INTAKE ──────────────────────────────────────────────────────────────
    dict(mod_name="Cold Air Intake / Induction Kit",
         mod_category="Intake",
         typical_hp_gain_pct=3,   typical_tq_gain_pct=2,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=200,
         notes="Modest standalone gain. Better with a remap. Improves sound."),

    dict(mod_name="High-Flow Panel Air Filter",
         mod_category="Intake",
         typical_hp_gain_pct=1,   typical_tq_gain_pct=1,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=60,
         notes="Drop-in OEM replacement. K&N / BMC type. Minimal power gain, reusable."),

    dict(mod_name="Turbo Inlet Pipe Upgrade",
         mod_category="Intake",
         typical_hp_gain_pct=2,   typical_tq_gain_pct=2,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru",
         compatible_models="Turbocharged models",
         difficulty_level="DIY", typical_cost_usd=130,
         notes="Removes OEM corrugated inlet. Better compressor flow. Popular EA888 mod."),

    # ── EXHAUST ─────────────────────────────────────────────────────────────
    dict(mod_name="Cat-Back Exhaust System",
         mod_category="Exhaust",
         typical_hp_gain_pct=3,   typical_tq_gain_pct=2,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=700,
         notes="Reduced back pressure + improved sound. Bigger gain with a remap."),

    dict(mod_name="Full Exhaust System (Header-Back)",
         mod_category="Exhaust",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=6,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1200,
         notes="Significant flow improvement. Decat downpipe + cat-back."),

    dict(mod_name="Downpipe / Decat Pipe",
         mod_category="Exhaust",
         typical_hp_gain_pct=6,   typical_tq_gain_pct=5,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Mitsubishi;Ford",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=350,
         notes="Removes catalyst restriction on turbo outlet. Needs remap for full benefit."),

    dict(mod_name="Sports Cat (200 cell) Downpipe",
         mod_category="Exhaust",
         typical_hp_gain_pct=4,   typical_tq_gain_pct=4,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Ford;Subaru",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=280,
         notes="Road-legal alternative to full decat. Reduces back pressure significantly."),

    # ── INTERCOOLER ─────────────────────────────────────────────────────────
    dict(mod_name="Front-Mount Intercooler Upgrade (FMIC)",
         mod_category="Intercooler",
         typical_hp_gain_pct=5,   typical_tq_gain_pct=5,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Ford;Opel;Mitsubishi",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=500,
         notes="Reduces IAT, more consistent power. Required for Stage 2+."),

    dict(mod_name="Top-Mount Intercooler Upgrade (TMIC)",
         mod_category="Intercooler",
         typical_hp_gain_pct=4,   typical_tq_gain_pct=4,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Subaru;Mitsubishi;Saab",
         compatible_models="Impreza WRX/STI;Evo;9-3 Turbo",
         difficulty_level="Professional", typical_cost_usd=450,
         notes="Reduces heat soak. Popular on Subaru EJ engines."),

    dict(mod_name="Intercooler Spray Kit (Water Mist)",
         mod_category="Intercooler",
         typical_hp_gain_pct=3,   typical_tq_gain_pct=3,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="Turbocharged models",
         difficulty_level="DIY", typical_cost_usd=120,
         notes="Sprays water mist on intercooler. Reduces IAT by 5-15 C. Cheap effective solution."),

    # ── TURBO UPGRADES ──────────────────────────────────────────────────────
    dict(mod_name="Hybrid Turbocharger Upgrade",
         mod_category="Turbo Upgrade",
         typical_hp_gain_pct=25,  typical_tq_gain_pct=30,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru;Mitsubishi",
         compatible_models="IS20;IS38;N54;N55;EJ257;4B11T",
         difficulty_level="Professional", typical_cost_usd=1200,
         notes="Upgraded compressor/turbine in OEM housing. Bolt-on. Needs remap + fuelling."),

    dict(mod_name="Full Turbo Swap (Larger Turbocharger)",
         mod_category="Turbo Upgrade",
         typical_hp_gain_pct=55,  typical_tq_gain_pct=50,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Subaru;Mitsubishi;Ford",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=2500,
         notes="Full turbo swap. Requires fuelling, FMIC, remap, possibly forged internals."),

    dict(mod_name="IS38 Turbo Upgrade (for IS20 cars)",
         mod_category="Turbo Upgrade",
         typical_hp_gain_pct=30,  typical_tq_gain_pct=35,
         typical_hp_gain_abs=70,  typical_tq_gain_abs=90,
         compatible_makes="VW;Audi;Skoda;Seat",
         compatible_models="Golf GTI Mk7;Polo GTI;A3 2.0 TFSI;Leon Cupra",
         difficulty_level="Professional", typical_cost_usd=900,
         notes="IS38 is a direct bolt-on to IS20 cars. Common upgrade on Mk7 GTI. Needs remap."),

    # ── FUELLING ────────────────────────────────────────────────────────────
    dict(mod_name="High-Flow Fuel Injectors",
         mod_category="Fuelling",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="High-power turbocharged",
         difficulty_level="Professional", typical_cost_usd=400,
         notes="Enabling mod — allows higher power levels. No standalone gain."),

    dict(mod_name="High-Pressure Fuel Pump (HPFP) Upgrade",
         mod_category="Fuelling",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;BMW;Subaru",
         compatible_models="Direct injection turbocharged",
         difficulty_level="Professional", typical_cost_usd=300,
         notes="Required for E30/E85 tunes or Stage 3+. Enabling mod."),

    dict(mod_name="E85 / Ethanol Flex Fuel Conversion",
         mod_category="Fuelling",
         typical_hp_gain_pct=18,  typical_tq_gain_pct=15,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Subaru;Mitsubishi;BMW",
         compatible_models="Turbocharged with upgraded fuelling",
         difficulty_level="Professional", typical_cost_usd=600,
         notes="E85 allows more boost and timing advance. Requires HPFP + injector upgrades."),

    # ── SUPERCHARGER ────────────────────────────────────────────────────────
    dict(mod_name="Supercharger Kit — Roots/Twin-Screw",
         mod_category="Supercharger",
         typical_hp_gain_pct=42,  typical_tq_gain_pct=38,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Ford;Chevrolet;BMW;Toyota;Honda;Mazda",
         compatible_models="Naturally aspirated V6/V8",
         difficulty_level="Professional", typical_cost_usd=4200,
         notes="Bolt-on complete kit. Linear power delivery. No lag. Popular on NA V8s."),

    dict(mod_name="Supercharger Pulley Upgrade",
         mod_category="Supercharger",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=7,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Ford;Chevrolet;Toyota",
         compatible_models="Supercharged models (Mustang GT500, Camaro ZL1, Corolla GR)",
         difficulty_level="Professional", typical_cost_usd=300,
         notes="Smaller pulley = more boost. Cheap and effective on existing supercharged cars."),

    # ── NITROUS ─────────────────────────────────────────────────────────────
    dict(mod_name="Nitrous Oxide System — Dry Kit (50 hp)",
         mod_category="Nitrous",
         typical_hp_gain_pct=None, typical_tq_gain_pct=None,
         typical_hp_gain_abs=50,  typical_tq_gain_abs=40,
         compatible_makes="All",
         compatible_models="Naturally aspirated",
         difficulty_level="Professional", typical_cost_usd=600,
         notes="50 hp shot. Temporary when activated. Needs fresh plugs and timing retard."),

    dict(mod_name="Nitrous Oxide System — Wet Kit (100 hp)",
         mod_category="Nitrous",
         typical_hp_gain_pct=None, typical_tq_gain_pct=None,
         typical_hp_gain_abs=100, typical_tq_gain_abs=80,
         compatible_makes="All",
         compatible_models="Strong engine required",
         difficulty_level="Professional", typical_cost_usd=950,
         notes="Injects fuel + N2O simultaneously. Requires forged internals for reliability."),

    # ── WATER/METHANOL ──────────────────────────────────────────────────────
    dict(mod_name="Water/Methanol Injection Kit",
         mod_category="Fuel Additives",
         typical_hp_gain_pct=10,  typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=500,
         notes="Cools intake charge, allows more boost/timing. Popular on IS20/IS38 Stage 2+."),

    # ── SUSPENSION ──────────────────────────────────────────────────────────
    dict(mod_name="Coilover Suspension Kit",
         mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1100,
         notes="Improves handling, not power. Adjustable ride height and damping."),

    dict(mod_name="Sway Bar / Anti-Roll Bar Upgrade",
         mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=220,
         notes="Reduces body roll significantly. No power gain."),

    dict(mod_name="Strut Brace / Chassis Brace",
         mod_category="Suspension",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=150,
         notes="Improves chassis rigidity. Better turn-in and steering feel."),

    # ── BRAKES ──────────────────────────────────────────────────────────────
    dict(mod_name="Big Brake Kit (BBK) — 4-piston",
         mod_category="Brakes",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1600,
         notes="Larger rotors + multi-piston calipers. Needed for 300+ hp track use."),

    # ── ENGINE INTERNALS ────────────────────────────────────────────────────
    dict(mod_name="Forged Pistons + Connecting Rods",
         mod_category="Engine Internals",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=2200,
         notes="Enabling mod for 400+ hp builds. Required for high boost reliability."),

    dict(mod_name="Ported and Polished Cylinder Head",
         mod_category="Engine Internals",
         typical_hp_gain_pct=10,  typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1300,
         notes="Improves airflow through head. Best combined with cam upgrades."),

    dict(mod_name="Performance Camshaft Upgrade",
         mod_category="Engine Internals",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=6,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Honda;Toyota;Subaru;BMW;Ford;Mazda",
         compatible_models="High-rev naturally aspirated",
         difficulty_level="Professional", typical_cost_usd=850,
         notes="More lift/duration. Moves power band higher. Needs remap. Popular on K-series/B-series."),

    dict(mod_name="Stroker Kit (Increased Displacement)",
         mod_category="Engine Internals",
         typical_hp_gain_pct=15,  typical_tq_gain_pct=20,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="Honda;Subaru;Ford;Chevrolet",
         compatible_models="B16;B18;EJ20;EJ25;LS",
         difficulty_level="Professional", typical_cost_usd=3500,
         notes="Increases engine displacement via longer stroke crankshaft."),

    # ── BOOST CONTROL ───────────────────────────────────────────────────────
    dict(mod_name="Electronic Boost Controller",
         mod_category="Boost Control",
         typical_hp_gain_pct=8,   typical_tq_gain_pct=8,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="Turbocharged models",
         difficulty_level="Professional", typical_cost_usd=250,
         notes="Precise boost control. Safer than manual boost controller. Needs custom tune."),

    dict(mod_name="Blow-Off Valve / Diverter Valve Upgrade",
         mod_category="Boost Control",
         typical_hp_gain_pct=1,   typical_tq_gain_pct=1,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="VW;Audi;Subaru;Mitsubishi;BMW",
         compatible_models="Turbocharged models",
         difficulty_level="DIY", typical_cost_usd=130,
         notes="Prevents compressor surge. Reliability + sound improvement."),

    # ── DRIVETRAIN / TRANSMISSION ───────────────────────────────────────────
    dict(mod_name="Limited Slip Differential (LSD)",
         mod_category="Drivetrain",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="Professional", typical_cost_usd=1600,
         notes="Reduces wheelspin, improves exit traction. Essential for high-power FWD/RWD."),

    dict(mod_name="Short-Throw Shifter",
         mod_category="Transmission",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="Manual transmission",
         difficulty_level="DIY", typical_cost_usd=160,
         notes="Reduces gear throw by 30-50%. No power gain."),

    dict(mod_name="Performance Clutch Kit",
         mod_category="Transmission",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All",
         compatible_models="Manual transmission",
         difficulty_level="Professional", typical_cost_usd=700,
         notes="Handles higher torque for Stage 2+ builds. Required at 350+ Nm."),

    # ── AERODYNAMICS ────────────────────────────────────────────────────────
    dict(mod_name="Front Splitter",
         mod_category="Aerodynamics",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=200,
         notes="Increases front downforce. Effect significant only above 150 km/h."),

    dict(mod_name="Rear Wing / Spoiler",
         mod_category="Aerodynamics",
         typical_hp_gain_pct=0,   typical_tq_gain_pct=0,
         typical_hp_gain_abs=None, typical_tq_gain_abs=None,
         compatible_makes="All", compatible_models="All",
         difficulty_level="DIY", typical_cost_usd=320,
         notes="Adds rear downforce. Can slightly increase drag."),

    dict(mod_name="Diffuser (Rear Underbody)",
         mod_category="Aerodynamics",
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


def scrape_all(static_only: bool = False) -> pd.DataFrame:
    rows = get_static_mods()

    if not static_only:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error(
                "Playwright is not installed. "
                "Run: pip install playwright && python -m playwright install chromium"
            )
            log.warning("Falling back to static-only mode.")
            static_only = True

    if not static_only:
        with sync_playwright() as pw:
            browser, context = _make_browser_context(pw)
            try:
                rows.extend(scrape_apr(context))
                rows.extend(scrape_racechip(context))
            finally:
                context.close()
                browser.close()

    df = pd.DataFrame(rows)
    df.drop_duplicates(subset=["mod_name", "compatible_models"], keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)

    for c in COLS:
        if c not in df.columns:
            df[c] = None
    df = df[COLS]

    df.to_csv(OUTPUT_CSV, index=False)
    log.info(f"Saved {len(df)} modifications to {OUTPUT_CSV}")
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scrape car modification data to CSV")
    ap.add_argument(
        "--static-only",
        action="store_true",
        help="Use only the built-in static list (no internet required)",
    )
    args = ap.parse_args()
    df = scrape_all(static_only=args.static_only)
    print(f"\nDone. {len(df)} modifications written to {OUTPUT_CSV}")
    print(
        df[["mod_name", "mod_category", "typical_hp_gain_pct", "compatible_makes"]]
        .to_string(index=False)
    )
