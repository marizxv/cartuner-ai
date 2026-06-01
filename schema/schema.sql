-- CarTuner AI — SQLite schema (data dictionary)
-- This file documents the structure of the three core tables.
-- The database is built from the raw CSVs by:  python src/build_db.py
--
-- Column names here match the actual CSV headers exactly. Units are noted inline.
-- NOTE: `zero_hundred_stock` / `result_0_100` hold 0–100 km/h in seconds, sourced from
--       Parkers' "0–60 mph" figure (the two differ by ~0.1–0.2 s — a documented assumption).

-- ─────────────────────────────────────────────────────────────────────────────
-- cars — one row per stock car variant (source: parkers.co.uk)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cars (
    car_id              TEXT PRIMARY KEY,   -- e.g. "CAR_91E03531"
    make                TEXT NOT NULL,
    model               TEXT,
    variant             TEXT,
    raw_title           TEXT,               -- scraped page title (human reference)
    year_from           INTEGER,
    year_to             REAL,               -- mostly null; kept for completeness
    displacement_cc     REAL,
    cylinders           REAL,
    stock_hp            REAL,               -- manufacturer crank bhp
    stock_torque_nm     REAL,
    weight_kg           REAL,               -- kerb weight
    drivetrain          TEXT,               -- Front/Rear/4 wheel drive
    transmission        TEXT,               -- Manual / Automatic
    engine_type         TEXT,               -- Naturally Aspirated / Turbocharged / Diesel / Hybrid / EV
    fuel_type           TEXT,
    drag_coef           REAL,               -- almost always null (Parkers omits it)
    frontal_area_m2     REAL,               -- always null (kept to mirror source schema)
    top_speed_stock     REAL,               -- km/h (converted from mph)
    top_speed_mph       REAL,
    zero_hundred_stock  REAL,               -- 0–100 km/h in seconds (see header note)
    co2_gkm             REAL,
    source_url          TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- modifications — catalogue of tuning modifications (curated + APR/RaceChip)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS modifications (
    mod_id              TEXT PRIMARY KEY,   -- e.g. "MOD_3BD6FDD6"
    mod_name            TEXT NOT NULL,
    mod_category        TEXT,               -- ECU Tune / Exhaust / Forced Induction / ...
    typical_hp_gain_pct REAL,
    typical_tq_gain_pct REAL,
    typical_hp_gain_abs REAL,               -- mostly null (percentages preferred)
    typical_tq_gain_abs REAL,               -- mostly null
    compatible_makes    TEXT,               -- semicolon-separated string, e.g. "VW;Audi;BMW"
    compatible_models   TEXT,               -- free text, e.g. "All turbocharged petrol"
    difficulty_level    TEXT,
    typical_cost_usd    INTEGER,
    notes               TEXT,
    source_url          TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- builds — one row per real or synthetic modified car (the training data for Model B)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS builds (
    build_id            TEXT PRIMARY KEY,   -- e.g. "BUILD_A958C3A3"
    car_id              TEXT,               -- FK → cars.car_id (valid only for synthetic rows)
    make                TEXT,
    model               TEXT,
    variant             TEXT,
    year                REAL,
    mods_applied        TEXT,               -- FK → modifications.mod_id (synthetic) or free text (real)
    mod_description     TEXT,               -- raw text copied from the source
    result_hp           REAL,               -- post-mod hp (wheel hp for forum/dyno sources)
    result_torque       REAL,
    result_0_100        REAL,               -- post-mod 0–100 km/h in seconds
    result_top_speed    REAL,
    result_quarter_mile REAL,               -- seconds (dragtimes source)
    measurement_type    TEXT,               -- dyno / measured / synthetic
    source_type         TEXT,               -- community / dragtimes / synthetic
    source_url          TEXT,
    confidence          REAL                -- 0.40 synthetic, 0.60 dragtimes, 0.75 community
);
