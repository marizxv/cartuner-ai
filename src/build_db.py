"""
build_db.py — assemble data/cartuner.db from the raw CSVs.

Reproducible database build: applies schema/schema.sql, then loads each CSV.
Run from the repo root with the venv active:

    python src/build_db.py

Re-run any time the CSVs change — it rebuilds from scratch.
"""

import sqlite3
from pathlib import Path

import pandas as pd

ROOT   = Path(__file__).resolve().parent.parent
DB     = ROOT / "data" / "cartuner.db"
SCHEMA = ROOT / "schema" / "schema.sql"

# table name -> source CSV
TABLES = {
    "cars":             ROOT / "data" / "raw" / "cars_raw.csv",
    "modifications":    ROOT / "data" / "raw" / "modifications_raw.csv",
    "builds":           ROOT / "data" / "raw" / "builds_raw.csv",
    "builds_augmented": ROOT / "data" / "raw" / "builds_raw_augmented.csv",
}


def main() -> None:
    if DB.exists():
        DB.unlink()  # rebuild from scratch

    conn = sqlite3.connect(DB)

    # Apply the documented schema (cars, modifications, builds)
    conn.executescript(SCHEMA.read_text())

    # Load each CSV. if_exists="replace" lets pandas define columns for tables
    # not covered by the schema (e.g. builds_augmented) and keeps types simple.
    for table, csv_path in TABLES.items():
        if not csv_path.exists():
            print(f"  skip {table}: {csv_path.name} not found")
            continue
        df = pd.read_csv(csv_path)
        df.to_sql(table, conn, if_exists="replace", index=False)
        print(f"  loaded {table:<18} {len(df):>6} rows")

    conn.commit()
    conn.close()
    print(f"\nDatabase built → {DB.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
