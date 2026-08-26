# Project handoff — read this first

Everything a new session needs to continue work on this project without
re-deriving it. Written 26 Aug 2026.

---

## What this is

A data pipeline and public web app scoring NYC hospitals on a risk-adjusted
**Value Index** for elective hip and knee replacement, built from public data.
Portfolio project, owner: Owen Ferris (`Hoselater` on GitHub).

**Repo:** `https://github.com/Hoselater/nyc-healthcare-transparency-mvp`
**Branch: `master`** (not `main`)
**Local:** `D:\Claude\nyc-healthcare-transparency-mvp`

```
Value Index = (1/clinical O/E)^2.0  ×  (market median / facility cost)^1.0  ×  ln(1+volume)^1.0
```

---

## Environment (all working)

| Thing | State |
|---|---|
| Python 3.14.7 | `venv\Scripts\python.exe`, all deps installed |
| PostgreSQL 18 | running; database `nyc_healthcare_mvp`; password in `.env` |
| Git | configured, pushes work, noreply email |
| LibreOffice | installed (used to build the Word/PDF plan) |
| Socrata app token | **NOT SET** — user will do it; throttled without it |

Run the apps (venv activation not required):
```
.\run_app.ps1            # public app, port 8501
.\run_app.ps1 review     # crosswalk review console, port 8502
```

Pipeline stages: `schema, sparcs, mrf, cost, quality, transform, crosswalk, score, export, publish, all`

Verify SQL with no data: `psql -d postgres -v ON_ERROR_STOP=1 -f sql/99_smoke_test.sql`
→ must print `ALL SMOKE TESTS PASSED`.

---

## Current state

**41 NYC facilities scored**, 27,021 procedures, clinical years 2022 + 2024.

| Cost basis | Facilities | Volume |
|---|---|---|
| MRF negotiated rate | 18 | 19,987 |
| SPARCS list charge (fallback) | 23 | 7,760 |

CMS outcome measures on 30/41. App has a hip/knee/combined selector.
Deployed: **not yet** (Streamlit Cloud steps in `docs/DEPLOYMENT.md`).

---

## Hard-won facts — do not re-derive these

**1. APR-DRG 301/302 do not exist in SPARCS.** The original planning documents
said 301=hip, 302=knee. Verified against 2022 and 2024: zero rows; 303 is a
lumbar fusion. SPARCS uses APR-DRG v38: **324 = elective hip, 326 = elective
knee** (323/325 are the non-elective counterparts, excluded).

**2. …but NYU Langone's chargemaster genuinely uses 301/302.** Confirmed from
their own description column: `APR301-1 HIP JOINT REPLACEMENT`. They are on an
older APR-DRG version. Both numbering schemes are accepted on the *pricing*
side; legacy codes only count when the row description confirms the procedure.

**3. APR-DRG 470 ≠ MS-DRG 470.** Different classification systems, different
procedures. NYU publishes `APR470-1` with a declared type of just `DRG`. Codes
are resolved to a `(system, number)` pair and the systems are never pooled.

**4. Facility identity is the PFI, never the name.** Every facility name changed
case in 2024, and some changed form (`New York - Presbyterian/Queens` →
`NEWYORK-PRESBYTERIAN/QUEENS`). PFI is 100% populated and stable. Grouping by
name splits hospitals in half.

**5. The borough is `Manhattan` in 2022 and `New York` in 2024.** Also
`St Lawrence`/`Saint Lawrence`. The cohort filter accepts both plus the service
area (`New York City`), which is stable — but its *column* is named
`hospital_service_area` in 2022 and `health_service_area` in 2024.

**6. `round(double precision, integer)` does not exist in PostgreSQL.**
`percentile_cont` returns double. Cast to numeric at the point of aggregation.

**7. The CMS provider-data API returns an EMPTY result set — not an error — when
`limit` exceeds its cap.** 2000 → 0 rows for a query returning 163 at 500. Keep
`MAX_PAGE = 500`.

**8. `patient_disposition ILIKE '%Home%'` is wrong in both directions.**
`Skilled Nursing Home` and `Hospice - Home` both contain "Home". Use the
explicit allow-list in `fn_is_adverse_disposition`.

**9. List charge ≈ 2–4× negotiated rate.** Never pool them in one market median.
Benchmarks are computed per `cost_basis`.

**10. Retrieving a file ≠ obtaining data.** ~25% of successful crawls yield zero
usable rows. Montefiore's file is fully retrievable and contains no prices at
all (only a flat $6,330 de-identified minimum on every row) — deliberately
discarded.

---

## Key design decisions and why

- **Weights are 2.0 / 1.0 / 1.0** (clinical / financial / experience). Measured:
  under 1/1/1 the worst O/E in the top 10 was **2.03**; under 2/1/1 it is
  **0.82**. A 2/0.5/1 variant scored marginally better on quality but admitted a
  $182k facility, defeating an affordability tool.
- **CMS outcomes are shown beside the Value Index, never inside it.** They cover
  only 30/41 facilities; scoring some hospitals on real outcomes and the rest on
  a proxy would make one number mean different things per row.
- **LOS O/E correlates 0.44 with real CMS complication rates** (n=27). Real
  signal, not a substitute. Publish this number — it is the project's most
  honest and most interesting finding.
- **Benchmarks stratify on (procedure, severity, discharge_year).** Year matters
  because LOS drifts; procedure matters because hip ≠ knee.
- **Clinical metric defaults to `composite`** — geometric mean of LOS O/E and
  adverse-disposition O/E. Geometric because both are ratios centred on 1.0.
- **The app falls back to a committed CSV** when no database is attached, so the
  public demo cannot break. Cache is keyed on file mtime.

All tunables live in the `pipeline_config` table, not in code.

---

## Blocked, needs the user

| Item | Status |
|---|---|
| **Socrata app token** | health.data.ny.gov → Developer Settings. Priority. |
| SPARCS Ambulatory Surgery data | Not on the open portal; needs a formal NYSDOH request |
| Mount Sinai, NYC H+H prices | CDNs block every automated agent tried; needs manual download |
| Streamlit Cloud / Tableau / Figma | Instructions in `docs/DEPLOYMENT.md`, `docs/NEXT_STEPS.md` |

---

## Next steps, in priority order

1. **CPT-based outpatient pricing.** HSS alone publishes 13 outpatient centres
   already being crawled and ignored — they use **CPT 27130 (hip) / 27447
   (knee)** rather than DRGs. Parser change only. Joint replacement is moving
   outpatient fast, so the inpatient-only view degrades every year.
2. Deploy to Streamlit Cloud (repo is ready; branch is `master`).
3. Tableau dashboard, then Figma wireframes.
4. Review the fuzzy crosswalk matches in the review console.
5. Consider Turquoise Health (often free for research) for MRF coverage.

---

## Document map

| File | Contents |
|---|---|
| `README.md` | Overview, setup, algorithm |
| `docs/HANDOFF.md` | This file |
| `docs/DATA_SOURCES.md` | Four sources, cost meanings, user-facing guidance |
| `docs/ERRATA.md` | 40 defects found in the original scripts, with reasoning |
| `docs/DEPLOYMENT.md` | Free hosting, start to finish |
| `docs/NEXT_STEPS.md` | Tokens, Tableau, Figma, paid data sources |
| `docs/Healthcare Transparency MVP - Master Plan v3.docx/.pdf` | The plan document |
| `DISCLAIMER.md` | Not medical advice; what the index is not |

---

## Working style that has been effective

Keep iterating without asking unless genuinely blocked. Verify against live data
rather than assuming. Report failures plainly — several of the most valuable
findings here came from things that did not work. State caveats in the same
breath as results.
