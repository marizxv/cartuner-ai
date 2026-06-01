# CarTuner AI — Automotive Performance Dataset & Data-Preparation Pipeline

A documented dataset and end-to-end data-preparation pipeline for predicting vehicle
performance from specifications. Built for the **Data Preparation for Artificial
Intelligence** course, where the focus is *data-centric AI*: the model is deliberately
simple (linear regression), and the story is how data preparation — not model choice —
drives prediction quality.

> **Headline result:** on the same data and the same linear model, careful cleaning and
> feature engineering cut the prediction error nearly in half — cross-validated MAE from
> **1.44 s → 0.68 s**, R² from **0.72 → 0.91**. The single biggest gain came from one
> feature (`log(power_to_weight)`) that matched the data's representation to the underlying
> physics.

---

## What this is

The project builds three related tables and demonstrates two prediction tasks:

- **Model A — stock performance** (the main demonstration): predict a car's 0–100 km/h time
  from its spec sheet. Runs on real, scraped data and carries the full data-prep journey.
- **Model B — post-modification performance** (the project's goal): predict a car's 0–100
  time *after* a tuning modification. Requires joining all three tables, and is presented
  honestly as a pipeline proof-of-concept (see [Known limitations](#known-limitations--assumptions)).

---

## The dataset

Three tables, stored as CSVs in `data/` and assembled into `data/cartuner.db`:

| Table | Rows | Grain | Source |
|-------|-----:|-------|--------|
| `cars` | 2,150 | one stock car variant | parkers.co.uk (scraped) + manual exotics |
| `modifications` | 275 | one tuning modification | curated catalogue + APR / RaceChip |
| `builds` | 372 | one modified car + result | community forums, dragtimes.com, synthetic |

The `builds` table combines three sources of differing quality, each tagged with a
`confidence` score and `source_type`:

| Source | Rows | Confidence | Notes |
|--------|-----:|-----------:|-------|
| `community` | 30 | 0.75 | hand-collected forum builds with dyno results |
| `dragtimes` | 42 | 0.60 | scraped ¼-mile timeslips; rarely include horsepower |
| `synthetic` | 300 | 0.40 | physics-generated (see limitations) |

A larger, fully-synthetic augmentation (14,913 rows) is kept separately in
`data/raw/builds_raw_augmented.csv` / the `builds_augmented` DB table. It is **not** used for
modelling — it exists to show what naïve over-augmentation produces.

The full data dictionary (column names, units, keys) is in
[`schema/schema.sql`](schema/schema.sql).

---

## Repository structure

```
cartuner-ai/
├── data/
│   ├── raw/                     # source data (one CSV per table)
│   │   ├── cars_raw.csv
│   │   ├── modifications_raw.csv
│   │   ├── builds_raw.csv
│   │   └── builds_raw_augmented.csv   # over-augmented set, not used in models
│   ├── interim/                 # cleaned, pre-feature-engineering
│   │   ├── cars_clean.csv
│   │   └── baseline_scores.json       # dirty-model scores, shared between notebooks
│   ├── final/
│   │   └── cars_features.csv          # cleaned + engineered, ML-ready
│   └── cartuner.db              # SQLite build of all tables (regenerable)
├── notebooks/
│   ├── 01_collection.ipynb      # data collection (scraping)
│   ├── 02_baseline_dirty.ipynb  # baseline model on raw data
│   ├── 03_cleaning.ipynb        # audit, MCAR/MAR/MNAR, cleaning
│   ├── 04_features.ipynb        # feature engineering (the big win)
│   └── 05_builds_analysis.ipynb # 3-table integration + Model B
├── src/
│   ├── scraper.py               # single-page parkers/APR scraper (demo)
│   ├── scraper_cars.py          # full parkers crawler (produced cars_raw)
│   ├── scraper_mods.py          # modifications scraper
│   ├── scraper_builds.py        # builds scraper (dragtimes/fastestlaps)
│   └── build_db.py              # assembles cartuner.db from the CSVs
├── schema/
│   └── schema.sql               # data dictionary for the three tables
├── docs/                        # exported figures used in the report
└── requirements.txt
```

---

## The pipeline

Each notebook is self-contained and runnable top-to-bottom; together they form the journey
from raw data to a feature-engineered dataset and two models.

| # | Notebook | What it does | Key output |
|---|----------|--------------|-----------|
| 01 | `01_collection` | Demonstrates the web-scraping approach used to gather specs and mods | raw CSVs |
| 02 | `02_baseline_dirty` | Trains the model on raw data with minimal preprocessing, to set a floor | `baseline_scores.json` |
| 03 | `03_cleaning` | Audits the data, classifies missingness (MCAR/MAR/MNAR), cleans and imputes | `cars_clean.csv` |
| 04 | `04_features` | Engineers physics-informed features, one at a time, measuring each | `cars_features.csv` |
| 05 | `05_builds_analysis` | Audits the builds table, resolves entities, joins all three tables, builds Model B | — |

**Metric:** all notebooks report **5-fold cross-validated MAE** (mean absolute error, in
seconds — lower is better) as the honest headline number, alongside R².

---

## Key results

### Model A — stock 0–100 km/h prediction

| Stage | CV MAE (s) | R² | What changed |
|-------|-----------:|---:|--------------|
| Dirty baseline (nb 02) | 1.44 | 0.72 | raw data, raw features |
| After cleaning (nb 03) | 1.43 | 0.73 | nulls fixed — *barely moves the score* |
| + `log(power_to_weight)` (nb 04) | 0.68 | 0.91 | linearised the power law — **the breakthrough** |
| Final (all features, nb 04) | 0.68 | 0.91 | diminishing returns after the key feature |

The lesson: **cleaning made the data correct; feature engineering made it useful.** Adding
the *raw* power-to-weight ratio actually hurt (the relationship is non-linear); taking its
logarithm turned the curve into a straight line the model could fit.

### Model B — post-modification prediction

Joining `builds ⋈ cars ⋈ modifications` and training on the synthetic builds yields a
near-perfect CV R² of **0.997** — which the analysis flags as a **warning, not a success**:
the synthetic targets were generated by a physics formula, so the model is reverse-engineering
that formula rather than learning from reality. This is the data-centric / GIGO lesson made
concrete.

---

## Data sources

- **Stock specs:** [parkers.co.uk](https://www.parkers.co.uk) — structured UK-market spec pages.
- **Modifications:** curated catalogue informed by APR (goapr.com) and RaceChip published figures.
- **Builds:** community tuning forums (hand-collected), [dragtimes.com](https://www.dragtimes.com)
  ¼-mile timeslips, and physics-based synthesis.

---

## Known limitations & assumptions

Documented honestly, as required for a data-preparation deliverable:

1. **0–100 km/h is approximated from 0–60 mph.** Parkers publishes 0–60 mph; we store it as
   `zero_hundred_stock`. The two differ by roughly 0.1–0.2 s (100 km/h ≈ 62 mph), so all stock
   times are marginally optimistic versus a true 0–100 km/h figure. Consistent across the
   dataset, so it does not affect *relative* comparisons.
2. **Synthetic builds dominate the builds table (300 of 372).** They are clearly labelled
   (`source_type = 'synthetic'`, `confidence = 0.40`) and generated by
   `t_mod = t_stock × (hp_stock / hp_mod)^0.6` with ±5 % noise. Any model trained on them is
   self-consistent by construction — see Model B above.
3. **Foreign keys only link cleanly for synthetic builds.** Real builds required entity
   resolution (make-name normalisation). After it, all 30 community and 37/42 dragtimes rows
   match the cars table; the rest (Tesla, Oldsmobile, and a Buell *motorcycle*) are genuinely
   absent and left unlinked rather than forced.
4. **Mixed horsepower conventions.** `cars.stock_hp` is manufacturer crank bhp; forum
   `builds.result_hp` is typically wheel hp. They are not combined in a single calculation,
   but the distinction matters for any future cross-table hp modelling.
5. **The "dirty" baseline is minimally preprocessed, not literally raw** — it drops rows with a
   missing target and median-fills the rest, the bare minimum to train at all.

---

## Reproducing the pipeline

Requires Python 3.12+ (developed on macOS; `python3` on PATH).

```bash
# 1. Environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. (Optional) rebuild the SQLite database from the CSVs
python3 src/build_db.py

# 3. Run the notebooks in order
jupyter lab            # then run 01 → 05
# or headless:
for nb in 02_baseline_dirty 03_cleaning 04_features 05_builds_analysis; do
    jupyter nbconvert --to notebook --execute --inplace notebooks/$nb.ipynb
done
```

Notebooks 03–05 read the raw CSVs and `baseline_scores.json`, so run **02 before 03/04** to
keep the comparison numbers current after any data change.

---

## Future work

- Train a gradient-boosted regression model on the feature-engineered dataset for comparison.
- Collect more **real** build records to replace the synthetic majority and properly validate Model B.
- Build the constraint-satisfaction recommendation layer ("which mods reach a target 0–100?").
- Normalise `modifications.compatible_makes` from a delimited string into a proper relation.
