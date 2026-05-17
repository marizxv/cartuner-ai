-- CarTuner AI — SQLite schema
-- Load with: sqlite3 data/cartuner.db < schema/schema.sql

CREATE TABLE IF NOT EXISTS cars (
    car_id      TEXT PRIMARY KEY,          -- e.g. "vw_golf_gti_mk7_220"
    make        TEXT NOT NULL,
    model       TEXT NOT NULL,
    variant     TEXT,
    year_from   INTEGER,
    year_to     INTEGER,
    engine_cc   INTEGER,
    hp_stock    INTEGER,
    torque_nm_stock INTEGER,
    weight_kg   INTEGER,
    drivetrain  TEXT CHECK(drivetrain IN ('FWD', 'RWD', 'AWD', '4WD'),
    zero_to_100_stock REAL,
    top_speed_stock   INTEGER,
    drag_coefficient  REAL,
    source_url  TEXT
);

CREATE TABLE IF NOT EXISTS modifications (
    mod_id      TEXT PRIMARY KEY,          -- e.g. "apr_stage1_is20"
    name        TEXT NOT NULL,
    category    TEXT,                      -- ecu_tune / turbo / exhaust / intercooler / ...
    compatible_cars TEXT,                  -- JSON array of car_ids
    hp_gain_pct    REAL,
    torque_gain_pct REAL,
    hp_type     TEXT CHECK(hp_type IN ('whp', 'crank', 'unknown')),
    source_url  TEXT
);

CREATE TABLE IF NOT EXISTS builds (
    build_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id      TEXT REFERENCES cars(car_id),
    mod_description TEXT,                  -- raw text copied from source, parsed later
    mods_applied    TEXT,                  -- JSON array of mod_ids (filled in cleaning stage)
    result_hp       REAL,
    result_torque_nm REAL,
    result_0_100    REAL,
    result_top_speed INTEGER,
    measurement_type TEXT,                 -- dyno / manufacturer / estimate / synthetic
    hp_type     TEXT CHECK(hp_type IN ('whp', 'crank', 'unknown')),
    source_type TEXT,                      -- forum / tuner_site / youtube / reddit
    confidence  REAL CHECK(confidence BETWEEN 0.0 AND 1.0),
    source_url  TEXT NOT NULL
);
