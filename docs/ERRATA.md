# Errata: defects found in the original scripts

Audit of `database_schema_analytics.sql` and `python_etl_pipeline.py` against
the Master Project Plan, Data Plan, Implementation Roadmap, and README.

Severity key — **Fatal**: the script cannot complete. **Silent**: it completes
and produces wrong numbers, which is worse. **Gap**: the documented requirement
was never implemented.

---

## SQL — `database_schema_analytics.sql`

### 1. `ROUND(double precision, integer)` does not exist — Fatal

`PERCENTILE_CONT` has no `numeric` overload in PostgreSQL. It coerces its input
to `double precision` and returns `double precision`, so `median_negotiated_charge`
and `market_median_cost` are both doubles. The Value Index expression is
therefore a double, and:

```sql
ROUND( (1.0 / ...) * (m.market_median_cost / ...) * LN(...), 3 )
```

fails with `function round(double precision, integer) does not exist`. **The
master table could never be built.** Fixed by casting percentile results to
`numeric` at the point of aggregation.

### 2. `LN(NULLIF(c.patient_volume, 1))` nullifies low-volume facilities — Silent

`NULLIF(volume, 1)` returns NULL when volume is exactly 1, and NULL propagates
through the whole product — so a single-case facility gets *no score at all*
rather than a low one. The guard was presumably meant to dodge `LN(1) = 0`, but
zeroing is the correct behaviour for a multiplicative term and nullifying is not.
Replaced with `LN(1 + volume)`, the standard log1p safeguard, plus an explicit
`min_facility_volume` floor so small-n facilities are excluded deliberately
rather than by accident.

### 3. Pricing fan-out in the master join — Silent, and severe

`transformed_cms_pricing` was grouped by `(facility_name, billing_code)`, but the
master join matched on `facility_name` alone:

```sql
LEFT JOIN transformed_cms_pricing p ON x.cms_facility_name = p.facility_name
```

Any facility with more than one matching billing code produced one master row
per code. Every facility's clinical metrics were duplicated, and because the
market median was then computed across those duplicated rows, the benchmark
itself shifted toward whichever hospitals published the most codes. Fixed by
collapsing pricing to exactly one row per facility, enforced with a primary key.

### 4. `billing_code LIKE '%470%'` is a substring test — Silent

Matches `4700`, `1470`, `14700`, and the CPT code `47010` (hepatic
lobectomy — nothing to do with joint replacement). Those charges landed in the
DRG-470 price median. Replaced with `fn_normalize_drg()`, which parses the DRG
properly (`470`, `0470`, `MS-DRG 470`, `DRG-470`) and additionally requires the
file's own `code_type` column to say DRG.

### 5. `patient_disposition ILIKE '%Home%'` misclassifies outcomes — Silent

Real SPARCS disposition values include **`Skilled Nursing Home`** and
**`Hospice - Home`**. Both contain the substring "Home", so both were scored as
*good* outcomes. A discharge to hospice was counted as a clinical success.
Replaced with `fn_is_adverse_disposition()`, an explicit allow-list of
`Home or Self Care` and `Home w/ Home Health Services`, with unknown values
returning NULL so they are excluded from the rate rather than assumed good.

### 6. `CAST(length_of_stay AS NUMERIC)` aborts on redacted rows — Fatal

The `CASE` handled the literal `'120+'` but nothing else. SPARCS also blanks the
field for suppressed small-cell strata, and a single blank or malformed value
raises `invalid input syntax for type numeric` and kills the query. This is
called out in the Master Plan (§4.1) as the reason the column is VARCHAR, but
the transformation only handled one of the two redaction forms. Replaced with a
regex-guarded `fn_parse_los()` returning NULL on anything unparseable.

### 7. `apr_risk_of_mortality INTEGER` is the wrong type — Fatal on load

The Data Plan describes this field as a categorical classification
("Minor to Extreme"), and the API returns strings. Declaring it INTEGER makes
every insert fail. Changed to TEXT. It is now also *used*, to exclude
Extreme-mortality-risk cases from the elective cohort, as the Data Plan
specifies.

### 8. Scripts are not re-runnable — Fatal on second run

No `IF NOT EXISTS` / `DROP ... IF EXISTS` anywhere, so the second execution dies
on `relation already exists`. Every object is now idempotent.

### 9. Crosswalk `INNER JOIN` silently drops facilities — Silent

```sql
INNER JOIN facility_crosswalk x ON c.facility_name = x.sparcs_facility_name
```

Every plan document specifies a LEFT JOIN "without data loss". The inner join
deletes any facility missing from the crosswalk, with no record that it existed.
Changed to LEFT JOIN with an explicit `data_status` column, plus a QA query
listing unmapped facilities by volume.

### 10. Nothing enforced crosswalk uniqueness — Silent

`facility_crosswalk` had `pfi_number` as its primary key but no constraint on
`sparcs_facility_name`, so a duplicate name would fan out the join exactly as in
defect 3. Added unique indexes on both name columns.

### 11. `adverse_disposition_pct` computed, then discarded — Gap

Calculated in `transformed_sparcs_nyc` and never referenced again. The Data Plan
describes the clinical multiplier as rewarding facilities that discharge
"faster **and with fewer adverse dispositions** than statistically expected", so
it should feed the score. Now computed as a proper O/E ratio against a
severity-tier expected rate and folded into `oe_ratio_composite`, the geometric
mean of the two ratios, which is the shipped default for
`pipeline_config.clinical_oe_metric`. Geometric rather than arithmetic because
both terms are ratios centred on 1.0, so the mean must be scale-free. Set the
key to `los` to score on length of stay alone, which is the narrower reading of
the Master Plan formula.

### 12. No geography reached the output — Gap

`zip_code_3_digit` was selected into the `cleaned_sparcs` CTE and then dropped.
`master_orthopedic_market` had no geographic column at all — which makes the
README's Tableau instruction ("link the shapefile's ZCTA field to your dataset's
`zip_code_3_digit`") impossible to follow. The export now carries
`primary_zip3` and `hospital_county` per facility.

### 13. Market median weighted by MRF verbosity — Silent

The market benchmark was the median across all `(facility, billing_code)` rows,
so a hospital publishing 40 payer contracts outvoted one publishing 3. Now taken
across facility-level medians, one vote per hospital.

### 14. Elective scope never enforced — Gap

Every plan document scopes the MVP to **elective** joint replacement. The SQL
filtered on DRG and county but never on `type_of_admission`, so emergency and
trauma admissions — which have systematically longer stays — sat in the same
cohort and inflated the LOS of hospitals with large emergency departments.
Now controlled by `pipeline_config.elective_only`, default true.

### 15. Benchmark scope ambiguity — Resolved

The Data Plan says expected rates are computed "across the entire New York State
dataset"; the Master Plan calls it "the regional average". The original code
computed benchmarks from the NYC-only cohort *after* the borough filter. Now
explicit and configurable via `pipeline_config.benchmark_scope`, defaulting to
`statewide` — the larger reference population gives more stable tier estimates,
and the ETL already downloads statewide data.

### 32. Benchmarks not stratified by year — Silent, once years are stacked

Not a defect in the original (which loaded a single hard-coded year), but it
becomes one the moment more than one SPARCS release is loaded. Average length of
stay for joint replacement has fallen steadily as same-day discharge protocols
spread, so a benchmark pooled across release years flatters every facility in
the later year and penalises every facility in the earlier one — a calendar
artefact masquerading as a quality difference. `severity_benchmarks` is now
keyed on `(severity, discharge_year)`. With a single year loaded this degenerates
to plain severity stratification, so nothing changes for single-year runs.

---

## Python — `python_etl_pipeline.py`

### 16. DataFrame index misalignment silently nulls three columns — Silent

```python
db_records = pd.DataFrame()
db_records['facility_name'] = [facility_name] * len(filtered)   # RangeIndex 0..n-1
db_records['description']   = filtered[desc_col]                # index 50003, 50017, ...
```

`db_records` gets a fresh `RangeIndex`, while `filtered` retains its original
row labels from the middle of the chunk. Pandas aligns on index during
assignment, so **`description`, `payer_name`, `discounted_cash_price` and
`payer_specific_negotiated_charge` all become NaN** for every chunk after the
first. The pipeline reports success and inserts a table of nulls. The rewrite
builds plain dicts and never relies on index alignment.

### 17. `to_sql(method='multi')` exceeds the parameter limit — Fatal

150,000 rows × 13 columns is roughly 2 million placeholders in a single INSERT;
psycopg2 caps at 65,535. Replaced with `COPY ... FROM STDIN`, which is also
substantially faster.

### 18. `cms-hpt.txt` parsing raises IndexError on uppercase keys — Fatal per host

```python
if line.lower().startswith("mrf-url:"):
    mrf_link = line.split("mrf-url:", 1)[1].strip()
```

The test is case-insensitive but the split is not. A file written
`MRF-URL: https://...` passes the test, splits into a one-element list, and
raises `IndexError` — aborting that hospital. Now parsed with a case-folded
`partition(":")`.

### 19. Only the first `mrf-url` was read — Gap

`break` after the first match. The Data Plan explicitly describes multi-campus
systems publishing "sequential, repeating blocks of these attributes for each
individual facility" — so for NewYork-Presbyterian, Mount Sinai and Northwell,
all campuses but one were discarded. Now parses every block.

### 20. CMS CSV header row is row 3, not row 1 — Fatal

The CY2024 CSV template puts file metadata on rows 1–2 and the real column
headers on row 3. `pd.read_csv(url)` takes `hospital_name` and `last_updated_on`
as the column names, so every subsequent `'code' in c` lookup misses and the
`if not code_col: continue` guard skips the entire file — silently, reporting
"0 rows". Now sniffs the first 10 rows for the genuine header.

### 21. `'negotiated' in c or 'rate' in c` selects the wrong column — Silent

The CMS tall schema has `standard_charge|negotiated_dollar`,
`standard_charge|negotiated_percentage`, and `standard_charge|negotiated_algorithm`.
Dict ordering decides which one `next()` returns, so a *percentage* like `65`
could be written into a dollar column and then treated as a $65 knee
replacement — dragging the market median down catastrophically. Now matches the
exact `negotiated_dollar` column name.

### 22. Wide-format and JSON MRFs unsupported — Gap

CMS permits three formats; the script handled one, and `pd.read_csv` on a JSON
file yields garbage. Both are now parsed, JSON via streaming `ijson`.

### 23. No pagination — Silent truncation

`limit=150000` with no offset loop. If the cohort exceeds the limit the result
is silently clipped, with no way to distinguish a complete pull from a truncated
one. Now pages with a stable `:id` ordering.

### 24. Hard-coded database password — Gap

`DB_PASS = "your_password"` in a file the README tells you to commit to a public
GitHub repository, in the same breath as warning against committing credentials.
Moved to `.env`, which `.gitignore` excludes.

### 25. PFI never extracted, defeating the documented join strategy — Gap

Every plan document specifies resolving facilities via the Permanent Facility
Identifier. The script selected only `facility_name`, so the PFI the crosswalk
is keyed on was never pulled from the API. Now extracted and carried through.

### 26. Fixed column list breaks across SPARCS years — Fatal

`df[['facility_name', ..., 'zip_code_3_digits', ...]]` raises `KeyError` on any
release year where a field name differs (the ZIP field has appeared both with
and without the trailing `s`). Now resolved through a candidate-name map.

### 27. No User-Agent on the MRF download — Fatal per host

`pd.read_csv(url)` uses urllib with no headers; a large share of hospital CDNs
answer that with 403. The header was set for the `cms-hpt.txt` request but not
for the file it points at. Now streamed via `requests` with headers throughout.

### 28. `except Exception` swallowed everything as success — Silent

`ingest_mrf_chunks` caught every exception, printed a warning, and returned
normally, so the pipeline reported completion regardless. Errors are now logged
per-facility with the failure surfaced in the run summary and QA checks.

### 29. No app token — throttling

`Socrata("health.data.ny.gov", None)` is an anonymous client. Health Data NY
throttles those aggressively. Now reads `SOCRATA_APP_TOKEN` and warns when absent.

---

## Found by running against live data

Everything above was found by reading the code. The following were found only by
executing it against the real SPARCS API and real hospital MRFs.

### 33. APR-DRG 301/302 do not exist — Fatal, and it originates in the plan docs

Every planning document specifies APR-DRG **301 (hip)** and **302 (knee)**.
Queried against both the 2022 and 2024 SPARCS releases, those codes return
**zero rows**; 303 is a lumbar fusion procedure. The APR-DRG v38 classification
SPARCS actually uses is:

| Code | Description |
|---|---|
| 323 | NON-ELECTIVE OR COMPLEX HIP JOINT REPLACEMENT |
| **324** | **ELECTIVE HIP JOINT REPLACEMENT** |
| 325 | NON-ELECTIVE OR COMPLEX KNEE JOINT REPLACEMENT |
| **326** | **ELECTIVE KNEE JOINT REPLACEMENT** |

The cohort is now 324/326, which also carries the elective distinction in the
code itself. This is a correction to the source research, not just to the code:
with 301/302 the pipeline would have run cleanly and produced an empty market.

Real cohort size: **16,679 NYC discharges across 45 facilities (2022)** and
**14,661 across 47 (2024)**.

### 34. Borough naming changes between release years — Silent

`hospital_county` is `'Manhattan'` in the 2022 release and `'New York'` in the
2024 release (likewise `'St Lawrence'` / `'Saint Lawrence'`). A single-spelling
county list silently drops an entire borough from one year. The filter now
accepts both spellings, and additionally matches the service-area column, which
is `'New York City'` in both years — though it is named `hospital_service_area`
in 2022 and `health_service_area` in 2024.

### 35. Facility names drift between years, so name-keyed grouping splits hospitals — Silent

Every facility name changed case in 2024, and some changed form entirely:
`New York - Presbyterian/Queens` became `NEWYORK-PRESBYTERIAN/QUEENS`. Grouping
on the name turns one hospital into two half-volume rows, computing both O/E
ratios against the wrong denominators and halving the experience modifier.

`permanent_facility_id` was verified 100% populated in both releases, with all
45 of the 2022 facilities present in 2024. Facility metrics, the crosswalk, and
the master table are now keyed on the PFI, with the display name taken from the
most recent year. This is what the planning documents recommended; the original
code never even retrieved the column.

### 36. A validation that validated nothing — Silent

`_where_clause` accepted a candidate SoQL filter if it did not raise. But
`apr_drg_code in ('301','302')` is valid SoQL against a text column and simply
returns an empty set, so the probe reported success on a filter matching nothing.
It now requires the probe to return at least one row.

### 37. APR-DRG 470 is not MS-DRG 470 — Silent, and would have imported wrong prices

NYU Langone publishes every DRG line as `APR470-1 … APR470-4` (the `-1..-4`
suffixes are APR severity subclasses) with a declared type of merely `DRG`. The
original substring test, and even a corrected MS-DRG regex that honours a bare
`DRG` type, would read that as MS-DRG 470 and import an unrelated procedure's
price into the joint-replacement median.

Codes are now classified into a `(system, number)` pair, the two DRG systems are
never pooled, and APR-DRG 324/326 is accepted as a genuine alternative — it maps
to exactly the cohort the clinical side already uses. Facilities publishing
neither are reported as `no_published_price` rather than silently mispriced.

### 38. Streaming from `response.raw` drops mid-file — Fatal per file

Reading `response.raw` directly failed with `ValueError: I/O operation on closed
file` several seconds into NYU's 3,733-column MRF, losing the whole file.
Replaced with a `readinto` adapter over `requests.iter_content`, which is the
supported streaming path. A drop now also keeps the rows already parsed and
reports the result as PARTIAL rather than discarding them.

### 39. Format detection by URL suffix mislabels real files — Fatal per file

Maimonides publishes its MRF behind `download.aspx?pi=…` — no extension, served
as `application/octet-stream`, and actually JSON. Format is now decided by
sniffing the first non-whitespace byte, falling back to Content-Disposition and
Content-Type. That single fix recovered the file.

### 40. Several hospital CDNs reject non-browser agents — Fatal per host

A 403 now triggers one retry with a browser User-Agent. This recovered
Maimonides. Mount Sinai's CDN rejects both agents and needs a `manual_mrf_url`.

**Live crawl result:** 14 of 18 domains resolved, 70+ MRF locations. The
multi-block parsing fix is what makes that number possible — HSS alone publishes
13 locations and Northwell 24, where the original single-block parser would have
found one each. The four failures are external: an expired TLS certificate
(Montefiore), a hostname mismatch (SUNY Downstate), IP-based blocking
(NYC Health + Hospitals), and one hospital that never deployed the file
(Wyckoff).

---

## Documentation

### 30. README filenames do not match the repository — Gap

The README instructs `git add README.md etl_pipeline.py database_schema.sql`,
but the files are `python_etl_pipeline.py` and `database_schema_analytics.sql`,
so the commit silently omits the code. Filenames reconciled.

### 31. `COPY ... TO '/path/to/desktop'` runs on the server — Gap

Server-side `COPY TO` requires superuser (or `pg_write_server_files`) and writes
to the *database host's* filesystem — the wrong machine the moment the database
moves to Supabase or Neon, as the Master Plan recommends for V2. Replaced with a
client-side export (`run_pipeline.py export`); `\copy` is documented as the psql
alternative.
