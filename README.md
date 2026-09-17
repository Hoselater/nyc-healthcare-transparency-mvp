# NYC Healthcare Transparency MVP — Orthopedic Value Index

An end-to-end data pipeline for a direct-to-consumer healthcare transparency
marketplace, scoped to elective total knee and hip replacement
(APR-DRG 324/326; MS-DRG 470) in the five boroughs of New York City.

It joins public clinical outcomes (NY SPARCS) to federally mandated pricing
files (CMS Hospital Price Transparency) and computes a risk-adjusted **Value
Index** for each health system.

<!-- Fill these in once deployed -- see docs/DEPLOYMENT.md -->
**[Live app](#)** · **[Tableau dashboard](#)** · **[Deployment guide](docs/DEPLOYMENT.md)** · **[Engineering notes](docs/ERRATA.md)**

> ### ⚠️ Portfolio project — not medical or financial advice
>
> The Value Index is a **reasoned heuristic, never calibrated against patient
> outcomes**. Length of stay is a proxy, not a quality measure, and published
> rates are not what a patient pays. **Do not use this to choose a hospital.**
> Read **[DISCLAIMER.md](DISCLAIMER.md)** before drawing any conclusion from the
> numbers.

---

## Architecture

| Layer | What it does |
|---|---|
| **Python ETL** | Pulls SPARCS via the Socrata API; crawls `cms-hpt.txt` to discover MRFs and streams them into staging |
| **PostgreSQL** | Stages raw data, computes Observed-to-Expected risk ratios, normalises charges, executes the scoring algorithm |
| **Export** | Client-side CSV for Tableau Public, joined to NYC ZCTA shapefiles |

```
SPARCS API ─┐
            ├─► stg_sparcs ──► ortho_cohort ──► severity_benchmarks ─┐
            │                                                        ├─► facility_clinical_metrics ─┐
cms-hpt.txt ┘                                                        ┘                              │
     │                                                                                              ├─► master_orthopedic_market
     └─► MRF (CSV tall/wide, JSON) ──► stg_cms_mrf ──► facility_pricing ──► market_benchmark ────────┘
                                                              │
                                            facility_crosswalk ┘                    └─► vw_facility_scores ──► CSV ──► Tableau
```

---

## The Value Index

```
                  1                market median cost
Value Index =  ─────────  ×  ────────────────────────────  ×  ln(1 + volume)
               clinical O/E        facility procedure cost
```

**Clinical multiplier** — the inverse of the risk-adjusted Observed-to-Expected
ratio. Expected rates are derived by *indirect standardization*: each discharge
is matched to the average for its own APR severity tier (1 Minor → 4 Extreme)
and discharge year, so a facility's expected values reflect its actual case mix.
An O/E below 1.0 means better than expected, which raises the index.

By default the clinical term is a **composite**: the geometric mean of the
length-of-stay O/E and the adverse-discharge-disposition O/E. Geometric rather
than arithmetic because both terms are ratios centred on 1.0, so the mean must
be scale-free — twice the expected LOS and half the expected adverse rate should
cancel to 1.0, which only the geometric mean does. Set
`pipeline_config.clinical_oe_metric` to `los` to score on length of stay alone.

**Financial multiplier** — the facility's median MS-DRG 470 negotiated rate
against the market median. Below-median pricing raises the index; outlier
pricing degrades it.

**Experience modifier** — `ln(1 + volume)` of elective joint-replacement discharges.
Logarithmic so that high-volume academic centres are rewarded without
overwhelming the cost and quality terms. The `1 +` matters: plain `ln(volume)`
is zero at a volume of one, which would zero the entire product.

---

## Setup

### 1. Database

```bash
createdb -U postgres nyc_healthcare_mvp
```

### 2. Python environment

```bash
python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt
```

### 3. Credentials

```bash
copy .env.example .env
```

Fill in `DB_PASSWORD`, and register a free Socrata app token at
<https://health.data.ny.gov/profile/edit/developer_settings> — anonymous API
requests are throttled hard enough to make a full pull impractical.

### 4. Verify the SQL before loading anything

Run the smoke test. It builds the whole pipeline in a throwaway schema against
synthetic data whose expected answers are computed by hand, asserts the
arithmetic, and drops the schema. No Python and no real data required.

```bash
psql -d postgres -v ON_ERROR_STOP=1 -f sql/99_smoke_test.sql
```

It must print `ALL SMOKE TESTS PASSED`. Run it from the repository root —
the script uses relative `\i` includes.

---

## Running the pipeline

Each stage is separately runnable, because they fail for different reasons and
have very different runtimes — a throttled API call should not force you to
re-crawl every multi-gigabyte MRF.

```bash
python run_pipeline.py schema      # create tables and pipeline_config
python run_pipeline.py sparcs      # load clinical data  (~10-40 min)
python run_pipeline.py mrf         # crawl and load pricing  (hours)
python run_pipeline.py transform   # cohort, benchmarks, O/E ratios
python run_pipeline.py crosswalk   # propose SPARCS <-> CMS facility mappings
python run_pipeline.py score       # value index and export views
python run_pipeline.py export      # write the Tableau CSV
python run_pipeline.py publish     # write the snapshot the web app reads
```

Or `python run_pipeline.py all`.

To run the web app locally: `streamlit run app.py`. To put it online, see
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

### The crosswalk step needs a human

SPARCS and CMS rarely spell a hospital the same way. `crosswalk` proposes
pairings — exact matches on a normalised name are trusted automatically, fuzzy
matches above 97 are auto-accepted, and everything between 80 and 97 is written
with `reviewed = false` and dumped to `data/facility_crosswalk_review.csv`.

Open that file, correct the `cms_facility_name` column, set `reviewed` to
`true` on the rows you have checked, then:

```bash
python run_pipeline.py crosswalk --import-csv data/facility_crosswalk_review.csv
```

Reviewed rows are never overwritten by a later automated rebuild.

### Check the output before you trust it

```bash
psql -d nyc_healthcare_mvp -f sql/05_qa_checks.sql
```

Check 8 (the fan-out guard) must return zero rows. Checks 5 and 7 tell you how
much of the market you actually have.

---

## Tuning

All tunable parameters live in the `pipeline_config` table, so the `.sql` files
run unchanged from psql, DBeaver, or Python:

```sql
SELECT key, value, notes FROM pipeline_config ORDER BY key;
UPDATE pipeline_config SET value = 'composite' WHERE key = 'clinical_oe_metric';
```

| Key | Default | Effect |
|---|---|---|
| `elective_only` | `true` | Restrict to `type_of_admission = Elective` |
| `benchmark_scope` | `statewide` | Reference population for expected LOS |
| `clinical_oe_metric` | `composite` | Geometric mean of the LOS and adverse-disposition O/E ratios; set to `los` for LOS alone |
| `min_facility_volume` | `10` | Volume floor for scoring (counted across all loaded years) |
| `exclude_extreme_mortality_risk` | `true` | Drop catastrophic outliers |
| `los_censor_value` | `120` | Value substituted for the `120+` bucket |

Release years are set in `.env` via `SPARCS_YEARS` (default `2022,2024`).
Stacking years raises per-facility volume, which stabilises the O/E ratios for
smaller hospitals. Benchmarks are stratified by `(severity, discharge_year)`, so
pooling years does not smear year-over-year drift in average length of stay into
the comparison — average LOS for joint replacement has fallen steadily as
same-day discharge protocols spread, and an unstratified benchmark would flatter
every facility in the later year purely as a calendar artefact.

---

## Connecting to Tableau Public

Tableau Public cannot connect live to a local PostgreSQL database on the free
tier, so bridge with the CSV export.

**1. Export.** `python run_pipeline.py export` writes
`exports/nyc_ortho_scores_YYYYMMDD.csv`.

From psql instead, use `\copy` — client-side, and needs no elevated grant:

```
\copy (SELECT * FROM vw_tableau_export) TO 'nyc_ortho_scores.csv' WITH CSV HEADER
```

Note that server-side `COPY ... TO '/path'` writes to the *database host's*
filesystem and requires superuser, which breaks as soon as the database moves to
Supabase or Neon.

**2. Connect.** Tableau Public → Connect to Data → Text File → your CSV.

**3. Overlay geography.** Download the NYC ZCTA shapefile from NYC Open Data. In
the Data Source tab, click **Add** next to Connections → Spatial file. Relate the
shapefile's ZCTA field to `primary_zip3`.

Because SPARCS truncates ZIP codes to three digits, `primary_zip3` is the modal
3-digit ZIP of a facility's patients, not the facility's own address. It is a
catchment indicator. For precise hospital pins, geocode `facility_name` instead
and use the ZCTA layer only as a background choropleth.

**4. Build.** Double-click the Geometry pill to draw the map. `value_index` to
Colour, `patient_volume` to Size, and `facility_name`,
`median_negotiated_charge`, `oe_ratio_los`, `star_rating` to Tooltip.

---

## Publishing to GitHub

```bash
git init
git add .
git commit -m "End-to-end healthcare transparency data pipeline"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

`.gitignore` already excludes `.env`, raw CSVs, and `exports/`. Verify with
`git status` before the first push that no `.env` or multi-gigabyte data file is
staged.

---

## Known limitations

These are honest constraints of the data, worth stating explicitly in a
portfolio context rather than papering over:

- **LOS is a proxy, not an outcome.** SPARCS public-use files carry no
  readmission, revision, or complication data. Length of stay and discharge
  disposition are the best available stand-ins, and both are influenced by
  discharge-planning practice as much as by surgical quality.
- **The `120+` bucket is censored.** Stays of 120 days or more are all recorded
  as `120+`, so observed LOS is a floor for facilities with long-stay outliers.
- **Three-digit ZIP only.** Patient geography is coarse by design, and blanked
  entirely for small-population ZIP prefixes.
- **MRF compliance is partial.** Independent audits put proper `cms-hpt.txt`
  deployment at roughly 30–60% of hospitals. Non-compliant facilities need a
  `manual_mrf_url` in `data/target_hospitals.csv`, sourced from the hospital's
  price-transparency footer link.
- **Posted rates are not out-of-pocket cost.** Negotiated rates ignore
  deductibles, coinsurance, and benefit design. A consumer product would need a
  benefits API to convert these into a true patient liability.
- **Stacked years are not uniformly observed.** A facility that appears in only
  one of the loaded SPARCS releases has its volume counted over a shorter window
  than its peers, which understates its experience modifier. `years_observed` is
  exported so you can filter or normalise; a hospital that opened, closed, or
  changed name mid-window will show a short span.
- **The Value Index weights are a judgement call**, not an empirically validated
  model. The three terms are multiplied unweighted; there is no calibration
  against patient-reported outcomes. The composite clinical term in particular
  weights length of stay and discharge disposition equally, which is a choice,
  not a finding.

---

## Repository layout

```
app.py                     Public Streamlit web app
config.py                  Environment-driven configuration
run_pipeline.py            CLI runner
requirements.txt
.env.example               Copy to .env
LICENSE                    MIT
DISCLAIMER.md              Scope, limitations, and what this is not
.streamlit/                Theme and secrets template for the web app
sql/
  01_staging_schema.sql    Tables, indexes, pipeline_config
  02_transformations.sql   Cohort, benchmarks, O/E ratios
  03_pricing_and_value_index.sql
  04_export_views.sql      Ranking, star ratings, Tableau view
  05_qa_checks.sql         Diagnostics (run with psql)
  99_smoke_test.sql        Self-verifying synthetic-data test
etl/
  db.py                    Engine, SQL runner, COPY bulk loader
  sparcs.py                Socrata ingestion + CSV fallback
  cms_hpt.py               cms-hpt.txt discovery
  mrf.py                   MRF parsing (CSV tall/wide, JSON)
  crosswalk.py             Entity resolution
  export.py                Tableau CSV export
  nycdot/                  NYC DOT traffic scraper (standalone; see docs)
tests/
  test_nycdot.py           Offline tests for the traffic scraper
data/
  target_hospitals.csv           Crawl list -- edit to widen the market
  nyc_ortho_scores_public.csv    Published snapshot read by the web app
docs/
  DEPLOYMENT.md            Free hosting, start to finish
  NYCDOT_TRAFFIC.md        Live traffic speeds and cameras, East Side focus
  Healthcare Transparency MVP - Master Plan v3.docx / .pdf
  ERRATA.md                Defects found in the original scripts
```
