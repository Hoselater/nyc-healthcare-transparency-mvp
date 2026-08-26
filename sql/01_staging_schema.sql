-- =============================================================================
-- 01_staging_schema.sql
-- NYC Healthcare Transparency MVP -- Orthopedic Value Index
--
-- Staging tables + pipeline configuration. Idempotent: safe to re-run.
-- Run FIRST, before any ETL load:
--   psql -d nyc_healthcare_mvp -f sql/01_staging_schema.sql
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Pipeline configuration
--
-- Every tunable knob lives here rather than being hard-coded into the queries,
-- so the same .sql files run unchanged from psql, DBeaver, or the Python runner.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    notes       TEXT
);

INSERT INTO pipeline_config (key, value, notes) VALUES
    ('nyc_counties',        'New York,Manhattan,Kings,Queens,Bronx,Richmond',
        'SPARCS hospital_county values for the 5 boroughs. BOTH Manhattan and New York are required: the 2022 release labels the borough Manhattan, the 2024 release labels it New York.'),
    ('nyc_service_area',    'New York City',
        'Value of hospital_service_area / health_service_area that isolates NYC. Used together with the county list; either match qualifies.'),
    ('apr_drg_codes',       '324,326',
        'APR-DRG 324 = ELECTIVE HIP JOINT REPLACEMENT, 326 = ELECTIVE KNEE JOINT REPLACEMENT. NOT 301/302: those codes do not exist in SPARCS (verified against the 2022 and 2024 releases); 303 is a lumbar fusion. The non-elective counterparts 323/325 are deliberately excluded.'),
    ('target_apr_drgs',     '324,326,301,302',
        'APR-DRG codes to price when a hospital publishes APR-DRG rather than MS-DRG (NYU Langone does). Same cohort as apr_drg_codes on the clinical side. APR-DRG 470 is a DIFFERENT procedure from MS-DRG 470 and the two are never pooled.'),
    ('target_ms_drg',       '470',
        'MS-DRG 470 = major hip/knee arthroplasty WITHOUT MCC (the elective cohort).'),
    ('elective_only',       'true',
        'Restrict the clinical cohort to type_of_admission = Elective.'),
    ('exclude_extreme_mortality_risk', 'true',
        'Drop apr_risk_of_mortality = Extreme (catastrophic outliers).'),
    ('benchmark_scope',     'statewide',
        'statewide | nyc. Reference population for expected LOS per severity tier.'),
    ('clinical_oe_metric',  'composite',
        'los | composite. composite = geometric mean of the LOS O/E and the adverse-disposition O/E, matching the Data Plan prose that quality means discharging faster AND with fewer adverse dispositions than expected.'),
    ('min_facility_volume', '10',
        'Minimum elective joint-replacement discharges (APR-DRG 324/326) for a facility to be scored, counted across all loaded years.'),
    ('weight_clinical',     '2.0',
        'Exponent on the clinical multiplier. Raise above 1 to make risk-adjusted quality dominate price.'),
    ('weight_financial',    '1.0',
        'Exponent on the financial multiplier. Lower below 1 to stop a cheap-but-slow hospital outranking a fast one.'),
    ('weight_experience',   '1.0',
        'Exponent on the log-volume experience modifier.'),
    ('los_censor_value',    '120',
        'Numeric value substituted for the HIPAA-redacted 120+ length_of_stay bucket.')
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE FUNCTION fn_cfg(k TEXT)
RETURNS TEXT
LANGUAGE sql STABLE AS $fn$
    SELECT value FROM pipeline_config WHERE key = k;
$fn$;

CREATE OR REPLACE FUNCTION fn_cfg_bool(k TEXT)
RETURNS BOOLEAN
LANGUAGE sql STABLE AS $fn$
    SELECT lower(coalesce(fn_cfg(k), 'false')) IN ('true', 't', 'yes', 'y', '1');
$fn$;

CREATE OR REPLACE FUNCTION fn_cfg_num(k TEXT)
RETURNS NUMERIC
LANGUAGE sql STABLE AS $fn$
    SELECT NULLIF(btrim(fn_cfg(k)), '')::numeric;
$fn$;

CREATE OR REPLACE FUNCTION fn_cfg_list(k TEXT)
RETURNS TEXT[]
LANGUAGE sql STABLE AS $fn$
    SELECT array(
        SELECT btrim(t)
        FROM unnest(string_to_array(coalesce(fn_cfg(k), ''), ',')) AS t
        WHERE btrim(t) <> ''
    );
$fn$;


-- -----------------------------------------------------------------------------
-- stg_sparcs -- NY SPARCS de-identified inpatient discharges
--
-- Typing follows the HIPAA de-identification rules described in the Data Plan:
--   * length_of_stay arrives as TEXT because stays >= 120 days are aggregated to
--     the literal string "120+". Casting at load time throws; we cast in 02_.
--   * apr_risk_of_mortality is CATEGORICAL text (Minor/Moderate/Major/Extreme),
--     not an integer.
--   * zip_code_3_digit is text: truncated to 3 digits, blanked entirely for
--     small-cell strata, and set to the literal 'OOS' for out-of-state patients.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stg_sparcs (
    id                              BIGSERIAL PRIMARY KEY,
    permanent_facility_id           INTEGER,        -- PFI: the reliable join key
    operating_certificate_number    TEXT,
    facility_name                   TEXT,
    hospital_county                 TEXT,
    hospital_service_area           TEXT,           -- 'New York City', 'Long Island', ...
    zip_code_3_digit                TEXT,
    age_group                       TEXT,
    type_of_admission               TEXT,
    apr_drg_code                    INTEGER,
    apr_drg_description             TEXT,
    apr_severity_of_illness_code    SMALLINT,       -- 1 Minor .. 4 Extreme
    apr_risk_of_mortality           TEXT,           -- categorical, NOT integer
    length_of_stay_raw              TEXT,           -- '3', '120+', '' ...
    patient_disposition             TEXT,
    discharge_year                  INTEGER,
    source_dataset_id               TEXT,           -- Socrata 4x4, e.g. 5dtw-tffi
    loaded_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_stg_sparcs_drg      ON stg_sparcs (apr_drg_code);
CREATE INDEX IF NOT EXISTS ix_stg_sparcs_county   ON stg_sparcs (hospital_county);
CREATE INDEX IF NOT EXISTS ix_stg_sparcs_pfi      ON stg_sparcs (permanent_facility_id);
CREATE INDEX IF NOT EXISTS ix_stg_sparcs_severity ON stg_sparcs (apr_severity_of_illness_code);


-- -----------------------------------------------------------------------------
-- stg_cms_mrf -- CMS Hospital Price Transparency machine-readable file rows
--
-- One row per (facility, billing code, payer, plan) as published in the CMS v2.0
-- schema. NUMERIC(14,2) because gross charges on complex DRGs routinely exceed
-- the 99,999,999.99 ceiling of NUMERIC(10,2).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stg_cms_mrf (
    id                                  BIGSERIAL PRIMARY KEY,
    facility_name                       TEXT NOT NULL,
    cms_certification_number            TEXT,
    billing_code                        TEXT,
    billing_code_type                   TEXT,       -- 'MS-DRG', 'APR-DRG', 'CPT' ...
    description                         TEXT,
    setting                             TEXT,       -- inpatient / outpatient
    payer_name                          TEXT,
    plan_name                           TEXT,
    gross_charge                        NUMERIC(14,2),
    discounted_cash_price               NUMERIC(14,2),
    payer_specific_negotiated_charge    NUMERIC(14,2),
    min_negotiated_charge               NUMERIC(14,2),
    max_negotiated_charge               NUMERIC(14,2),
    source_url                          TEXT,
    loaded_at                           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_stg_cms_mrf_code     ON stg_cms_mrf (billing_code);
CREATE INDEX IF NOT EXISTS ix_stg_cms_mrf_facility ON stg_cms_mrf (facility_name);


-- -----------------------------------------------------------------------------
-- stg_sparcs_cost -- SPARCS Cost Transparency (Socrata 7dtz-qxmr)
--
-- Facility-level median charge and median cost per APR-DRG and severity tier.
-- Keyed on the PFI, so it joins to the clinical side directly and needs no
-- entity resolution at all -- unlike the CMS price files, which need a
-- crosswalk and are blocked or absent at several major systems.
--
-- These are NOT negotiated rates. median_charge is list price; median_cost is
-- the facility's own reported cost from the Institutional Cost Report. Used as
-- a labelled fallback where no MRF price exists, never silently mixed in.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stg_sparcs_cost (
    id                              BIGSERIAL PRIMARY KEY,
    pfi_number                      INTEGER,
    facility_name                   TEXT,
    apr_drg_code                    INTEGER,
    apr_drg_description             TEXT,
    apr_severity_of_illness_code    SMALLINT,
    medical_surgical_code           TEXT,
    discharges                      INTEGER,
    mean_charge                     NUMERIC(14,2),
    median_charge                   NUMERIC(14,2),
    mean_cost                       NUMERIC(14,2),
    median_cost                     NUMERIC(14,2),
    data_year                       INTEGER,
    loaded_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_stg_sparcs_cost_pfi  ON stg_sparcs_cost (pfi_number);
CREATE INDEX IF NOT EXISTS ix_stg_sparcs_cost_year ON stg_sparcs_cost (data_year);


-- -----------------------------------------------------------------------------
-- facility_crosswalk -- entity resolution between SPARCS and CMS naming
--
-- Keyed on the Permanent Facility Identifier, not the facility name. SPARCS
-- re-spells facilities between release years ('New York - Presbyterian/Queens'
-- became 'NEWYORK-PRESBYTERIAN/QUEENS', and every name changed case in 2024),
-- so a name-keyed crosswalk goes stale the moment another year is loaded.
-- sparcs_facility_name is retained purely as a human-readable label.
--
-- The UNIQUE index on cms_facility_name is load-bearing, not decorative: two
-- facilities mapped to one CMS name silently fan out the master join,
-- multiplying row counts and corrupting the market median.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facility_crosswalk (
    pfi_number                   INTEGER PRIMARY KEY,
    operating_certificate_number TEXT,
    sparcs_facility_name         TEXT NOT NULL,   -- display label, not a key
    cms_facility_name            TEXT,
    cms_certification_number     TEXT,
    match_method                 TEXT,            -- manual | exact | fuzzy
    match_score                  NUMERIC(5,2),    -- 0-100 for fuzzy matches
    reviewed                     BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_crosswalk_cms_name
    ON facility_crosswalk (lower(btrim(cms_facility_name)))
    WHERE cms_facility_name IS NOT NULL;


-- -----------------------------------------------------------------------------
-- stg_cms_quality -- CMS Care Compare outcome measures
--
-- The risk-standardised complication rate (COMP_HIP_KNEE) and 30-day
-- readmission rate (READM_30_HIP_KNEE) for elective primary hip and knee
-- arthroplasty, published per hospital by CMS.
--
-- These are REAL OUTCOMES, not proxies -- which is exactly what the SPARCS
-- public file lacks and what the Value Index's biggest limitation has been.
-- Keyed on the CMS Certification Number, which SPARCS does not carry, so it
-- reaches the pipeline through facility_ccn_crosswalk.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stg_cms_quality (
    id                        BIGSERIAL PRIMARY KEY,
    cms_certification_number  TEXT NOT NULL,
    facility_name             TEXT,
    citytown                  TEXT,
    state                     TEXT,
    zip_code                  TEXT,
    countyparish              TEXT,
    measure_id                TEXT,
    measure_name              TEXT,
    score                     NUMERIC(10,3),
    denominator               NUMERIC(12,1),
    compared_to_national      TEXT,
    start_date                TEXT,
    end_date                  TEXT,
    loaded_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_stg_cms_quality_ccn     ON stg_cms_quality (cms_certification_number);
CREATE INDEX IF NOT EXISTS ix_stg_cms_quality_measure ON stg_cms_quality (measure_id);


-- CCN <-> PFI resolution, built by matching official facility names within NY.
CREATE TABLE IF NOT EXISTS facility_ccn_crosswalk (
    pfi_number                INTEGER PRIMARY KEY,
    cms_certification_number  TEXT NOT NULL,
    sparcs_facility_name      TEXT,
    cms_facility_name         TEXT,
    match_method              TEXT,
    match_score               NUMERIC(5,2),
    reviewed                  BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ccn_crosswalk_ccn
    ON facility_ccn_crosswalk (cms_certification_number);
