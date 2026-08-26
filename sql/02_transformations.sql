-- =============================================================================
-- 02_transformations.sql
-- Clean SPARCS, build the indirect-standardization benchmarks, and compute
-- facility-level risk-adjusted clinical metrics.
--
-- Run AFTER stg_sparcs is loaded. Idempotent: safe to re-run.
--   psql -d nyc_healthcare_mvp -f sql/02_transformations.sql
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Helper: parse the HIPAA-redacted length_of_stay field.
--
-- SPARCS aggregates every stay of 120+ days into the literal string '120+', and
-- leaves the field blank for suppressed small-cell strata. A bare
-- CAST(length_of_stay AS NUMERIC) therefore aborts the whole query on the first
-- redacted row. Returning NULL for unparseable input lets AVG() skip them.
--
-- Note the censoring assumption: '120+' becomes exactly 120, so observed LOS for
-- facilities with long-stay outliers is a FLOOR, not a point estimate.
-- -----------------------------------------------------------------------------
-- STABLE, not IMMUTABLE: it reads los_censor_value out of pipeline_config.
CREATE OR REPLACE FUNCTION fn_parse_los(raw TEXT)
RETURNS NUMERIC
LANGUAGE sql STABLE PARALLEL SAFE AS $fn$
    SELECT CASE
        WHEN raw IS NULL                                          THEN NULL
        WHEN btrim(raw) ~ '^[0-9]+ *[+]$'                         THEN fn_cfg_num('los_censor_value')
        WHEN btrim(replace(raw, ',', '')) ~ '^[0-9]+(\.[0-9]+)?$' THEN btrim(replace(raw, ',', ''))::numeric
        ELSE NULL
    END;
$fn$;


-- -----------------------------------------------------------------------------
-- Helper: adverse discharge disposition flag.
--
-- The naive `patient_disposition ILIKE '%Home%'` test is wrong in both
-- directions against real SPARCS values: it scores 'Skilled Nursing Home' and
-- 'Hospice - Home' as GOOD outcomes because both contain the substring 'Home'.
--
-- For an elective joint replacement the only non-adverse destinations are
-- discharge to self-care or to home with home-health support. Everything else
-- (SNF, inpatient rehab, acute transfer, hospice, AMA, expired) signals a
-- recovery trajectory that did not go to plan. Unknown values return NULL so
-- they are excluded from the rate rather than silently counted as good.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_is_adverse_disposition(d TEXT)
RETURNS INTEGER
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT CASE
        WHEN d IS NULL OR btrim(d) = '' THEN NULL
        WHEN lower(btrim(d)) IN (
                'home or self care',
                'home w/ home health services',
                'home with home health services',
                'home health care svc'
             ) THEN 0
        WHEN lower(btrim(d)) IN (
                'unknown',
                'not available',
                'another type not listed'
             ) THEN NULL
        ELSE 1
    END;
$fn$;


-- -----------------------------------------------------------------------------
-- Helper: config list of APR-DRG codes as integers.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_cfg_int_list(k TEXT)
RETURNS INTEGER[]
LANGUAGE sql STABLE AS $fn$
    SELECT array(SELECT t::integer FROM unnest(fn_cfg_list(k)) AS t);
$fn$;


-- =============================================================================
-- STEP 1: The orthopedic cohort
--
-- Filters the full statewide SPARCS staging table down to elective primary hip
-- and knee replacement discharges, with LOS and disposition already normalised.
-- Kept STATEWIDE (with an is_nyc flag) so the severity benchmarks can be built
-- from the full New York reference population while scoring stays 5-borough.
-- =============================================================================
DROP TABLE IF EXISTS ortho_cohort CASCADE;

CREATE TABLE ortho_cohort AS
SELECT
    s.id                                            AS sparcs_id,
    s.permanent_facility_id,
    s.operating_certificate_number,
    btrim(s.facility_name)                          AS facility_name,
    s.hospital_county,
    s.hospital_service_area,
    CASE WHEN s.zip_code_3_digit ~ '^[0-9]{3}$'
         THEN s.zip_code_3_digit END                AS zip3,
    s.age_group,
    s.apr_drg_code,
    s.apr_severity_of_illness_code                  AS severity,
    s.apr_risk_of_mortality,
    fn_parse_los(s.length_of_stay_raw)              AS los_days,
    fn_is_adverse_disposition(s.patient_disposition) AS adverse_flag,
    s.patient_disposition,
    s.discharge_year,
    -- Either signal qualifies. The county list alone is not enough: the 2022
    -- release labels the borough 'Manhattan' and the 2024 release labels it
    -- 'New York', so a single-spelling list silently drops a whole borough
    -- from one year. The service area is the more stable of the two.
    (    s.hospital_county = ANY (fn_cfg_list('nyc_counties'))
      OR s.hospital_service_area = fn_cfg('nyc_service_area')
    )                                               AS is_nyc
FROM stg_sparcs s
WHERE s.apr_drg_code = ANY (fn_cfg_int_list('apr_drg_codes'))
  AND s.apr_severity_of_illness_code BETWEEN 1 AND 4
  AND fn_parse_los(s.length_of_stay_raw) IS NOT NULL
  AND s.permanent_facility_id IS NOT NULL
  AND btrim(coalesce(s.facility_name, '')) <> ''
  -- Required: benchmarks are stratified by (severity, discharge_year), so a row
  -- with no year has no benchmark to be compared against.
  AND s.discharge_year IS NOT NULL
  AND (NOT fn_cfg_bool('elective_only')
       OR s.type_of_admission ILIKE 'Elective')
  AND (NOT fn_cfg_bool('exclude_extreme_mortality_risk')
       OR coalesce(s.apr_risk_of_mortality, '') NOT ILIKE 'Extreme');

CREATE INDEX ix_ortho_cohort_pfi      ON ortho_cohort (permanent_facility_id);
CREATE INDEX ix_ortho_cohort_severity ON ortho_cohort (severity);
CREATE INDEX ix_ortho_cohort_nyc      ON ortho_cohort (is_nyc);


-- =============================================================================
-- STEP 2: Severity-tier benchmarks (the "Expected" side of the O/E ratio)
--
-- One expected LOS and one expected adverse-disposition rate per APR severity
-- tier, computed across the reference population.
--
-- Stratified by (severity, discharge_year) rather than severity alone. Average
-- length of stay for joint replacement has fallen steadily year over year as
-- same-day discharge protocols spread, so pooling release years into a single
-- benchmark would flatter every facility in the later year and penalise every
-- facility in the earlier one -- an artefact of the calendar, not of quality.
-- With a single year loaded this degenerates to plain severity stratification.
-- =============================================================================
DROP TABLE IF EXISTS severity_benchmarks CASCADE;

-- Also stratified by PROCEDURE. Hip and knee replacement have materially
-- different length-of-stay profiles, so a single pooled benchmark quietly
-- penalises hospitals whose orthopedic mix leans toward the slower procedure.
-- Stratifying costs nothing and makes the risk adjustment strictly better.
CREATE TABLE severity_benchmarks AS
SELECT
    c.apr_drg_code,
    c.severity,
    c.discharge_year,
    count(*)                                                AS benchmark_discharges,
    round(avg(c.los_days), 4)                               AS expected_los,
    round(avg(c.adverse_flag::numeric), 6)                  AS expected_adverse_rate
FROM ortho_cohort c
WHERE fn_cfg('benchmark_scope') = 'statewide' OR c.is_nyc
GROUP BY c.apr_drg_code, c.severity, c.discharge_year;

ALTER TABLE severity_benchmarks
    ADD PRIMARY KEY (apr_drg_code, severity, discharge_year);


-- =============================================================================
-- STEP 3: Facility-level risk-adjusted clinical metrics
--
-- Indirect standardization: each NYC discharge is joined to the benchmark for
-- ITS OWN severity tier, so avg(expected_los) over a facility's discharges is
-- that facility's case-mix-weighted expected LOS. Dividing observed by expected
-- yields the O/E ratio. Below 1.0 = better than expected.
--
-- Grouped by permanent_facility_id, NOT facility_name. SPARCS re-spells
-- facilities between release years -- 'New York - Presbyterian/Queens' becomes
-- 'NEWYORK-PRESBYTERIAN/QUEENS', and every name changes case in 2024 -- so
-- grouping on the name splits a single hospital into two half-volume rows and
-- corrupts both its O/E ratio and its experience modifier. The PFI is stable
-- and, verified across both releases, 100% populated.
-- =============================================================================
DROP TABLE IF EXISTS facility_clinical_metrics CASCADE;

CREATE TABLE facility_clinical_metrics AS
WITH scored AS (
    SELECT
        c.*,
        b.expected_los,
        b.expected_adverse_rate
    FROM ortho_cohort c
    JOIN severity_benchmarks b
      ON b.apr_drg_code   = c.apr_drg_code
     AND b.severity       = c.severity
     AND b.discharge_year = c.discharge_year
    WHERE c.is_nyc
),
agg AS (
    SELECT
        s.permanent_facility_id                                 AS pfi_number,
        -- Display name taken from the most recent release year, so the app
        -- shows current branding rather than a superseded spelling.
        (array_agg(s.facility_name
                   ORDER BY s.discharge_year DESC, s.facility_name))[1]
                                                                AS facility_name,
        mode() WITHIN GROUP (ORDER BY s.operating_certificate_number)
                                                                AS operating_certificate_number,
        mode() WITHIN GROUP (ORDER BY s.hospital_county)        AS hospital_county,
        mode() WITHIN GROUP (ORDER BY s.zip3)                   AS primary_zip3,
        min(s.discharge_year)                                   AS first_discharge_year,
        max(s.discharge_year)                                   AS last_discharge_year,
        count(*)                                                AS patient_volume,
        count(*) FILTER (WHERE s.apr_drg_code = 324)            AS hip_volume,
        count(*) FILTER (WHERE s.apr_drg_code = 326)            AS knee_volume,
        round(avg(s.severity::numeric), 3)                      AS avg_severity,
        avg(s.los_days)                                         AS observed_los,
        avg(s.expected_los)                                     AS expected_los,
        avg(s.adverse_flag::numeric)                            AS observed_adverse,
        avg(s.expected_adverse_rate)                            AS expected_adverse
    FROM scored s
    GROUP BY s.permanent_facility_id
)
SELECT
    a.pfi_number,
    a.facility_name,
    a.operating_certificate_number,
    a.hospital_county,
    a.primary_zip3,
    a.first_discharge_year,
    a.last_discharge_year,
    a.patient_volume,
    a.hip_volume,
    a.knee_volume,
    a.avg_severity,
    round(a.observed_los, 2)                                        AS observed_avg_los,
    round(a.expected_los, 2)                                        AS expected_avg_los,
    round(a.observed_los / nullif(a.expected_los, 0), 3)            AS oe_ratio_los,
    round(a.observed_adverse * 100, 2)                              AS observed_adverse_pct,
    round(a.expected_adverse * 100, 2)                              AS expected_adverse_pct,
    round(a.observed_adverse / nullif(a.expected_adverse, 0), 3)    AS oe_ratio_adverse,
    -- Composite clinical O/E: geometric mean of the two ratios. Geometric rather
    -- than arithmetic because both terms are ratios centred on 1.0, so the mean
    -- must be scale-free -- a 2x worse LOS and a 0.5x better disposition rate
    -- should cancel to 1.0, which only the geometric mean does.
    CASE
        WHEN a.expected_los IS NULL OR a.expected_los = 0 THEN NULL
        WHEN a.observed_adverse IS NULL
          OR a.expected_adverse IS NULL
          OR a.expected_adverse = 0
          OR a.observed_adverse = 0
            THEN round(a.observed_los / nullif(a.expected_los, 0), 3)
        ELSE round(
            sqrt(
                (a.observed_los / a.expected_los)
                * (a.observed_adverse / a.expected_adverse)
            )::numeric, 3)
    END                                                             AS oe_ratio_composite
FROM agg a
WHERE a.patient_volume >= fn_cfg_num('min_facility_volume');

ALTER TABLE facility_clinical_metrics ADD PRIMARY KEY (pfi_number);

-- Single column the Value Index reads from, selected by pipeline_config.
ALTER TABLE facility_clinical_metrics ADD COLUMN clinical_oe NUMERIC(10,3);

UPDATE facility_clinical_metrics
SET clinical_oe = CASE
        WHEN fn_cfg('clinical_oe_metric') = 'composite' THEN oe_ratio_composite
        ELSE oe_ratio_los
    END;


-- =============================================================================
-- STEP 4: Per-procedure clinical metrics
--
-- The combined table above answers "how good is this hospital at joint
-- replacement". This one answers "how good is it at MY operation", which is the
-- question a patient actually has. Hip and knee are different procedures with
-- different recovery profiles, and a hospital can be strong at one and weak at
-- the other -- information the combined figure averages away entirely.
--
-- Grouped by (PFI, APR-DRG). 324 = elective hip, 326 = elective knee.
-- =============================================================================
DROP TABLE IF EXISTS facility_procedure_metrics CASCADE;

CREATE TABLE facility_procedure_metrics AS
WITH scored AS (
    SELECT c.*, b.expected_los, b.expected_adverse_rate
    FROM ortho_cohort c
    JOIN severity_benchmarks b
      ON b.apr_drg_code   = c.apr_drg_code
     AND b.severity       = c.severity
     AND b.discharge_year = c.discharge_year
    WHERE c.is_nyc
),
agg AS (
    SELECT
        s.permanent_facility_id                                 AS pfi_number,
        s.apr_drg_code,
        (array_agg(s.facility_name
                   ORDER BY s.discharge_year DESC, s.facility_name))[1]
                                                                AS facility_name,
        mode() WITHIN GROUP (ORDER BY s.hospital_county)        AS hospital_county,
        mode() WITHIN GROUP (ORDER BY s.zip3)                   AS primary_zip3,
        min(s.discharge_year)                                   AS first_discharge_year,
        max(s.discharge_year)                                   AS last_discharge_year,
        count(*)                                                AS patient_volume,
        round(avg(s.severity::numeric), 3)                      AS avg_severity,
        avg(s.los_days)                                         AS observed_los,
        avg(s.expected_los)                                     AS expected_los,
        avg(s.adverse_flag::numeric)                            AS observed_adverse,
        avg(s.expected_adverse_rate)                            AS expected_adverse
    FROM scored s
    GROUP BY s.permanent_facility_id, s.apr_drg_code
)
SELECT
    a.pfi_number,
    a.apr_drg_code,
    CASE a.apr_drg_code
        WHEN 324 THEN 'hip'
        WHEN 326 THEN 'knee'
        ELSE 'other'
    END                                                             AS procedure,
    CASE a.apr_drg_code
        WHEN 324 THEN 'Hip replacement'
        WHEN 326 THEN 'Knee replacement'
        ELSE 'Other'
    END                                                             AS procedure_label,
    a.facility_name,
    a.hospital_county,
    a.primary_zip3,
    a.first_discharge_year,
    a.last_discharge_year,
    a.patient_volume,
    a.avg_severity,
    round(a.observed_los, 2)                                        AS observed_avg_los,
    round(a.expected_los, 2)                                        AS expected_avg_los,
    round(a.observed_los / nullif(a.expected_los, 0), 3)            AS oe_ratio_los,
    round(a.observed_adverse * 100, 2)                              AS observed_adverse_pct,
    round(a.expected_adverse * 100, 2)                              AS expected_adverse_pct,
    round(a.observed_adverse / nullif(a.expected_adverse, 0), 3)    AS oe_ratio_adverse,
    CASE
        WHEN a.expected_los IS NULL OR a.expected_los = 0 THEN NULL
        WHEN a.observed_adverse IS NULL OR a.expected_adverse IS NULL
          OR a.expected_adverse = 0 OR a.observed_adverse = 0
            THEN round(a.observed_los / nullif(a.expected_los, 0), 3)
        ELSE round(sqrt((a.observed_los / a.expected_los)
                      * (a.observed_adverse / a.expected_adverse))::numeric, 3)
    END                                                             AS oe_ratio_composite
FROM agg a
-- A lower floor than the combined table: splitting one facility's volume across
-- two procedures roughly halves each, and the production floor of 10 would drop
-- facilities that are perfectly adequately measured on the combined view.
WHERE a.patient_volume >= greatest(fn_cfg_num('min_facility_volume') / 2, 5);

ALTER TABLE facility_procedure_metrics ADD PRIMARY KEY (pfi_number, apr_drg_code);

ALTER TABLE facility_procedure_metrics ADD COLUMN clinical_oe NUMERIC(10,3);

UPDATE facility_procedure_metrics
SET clinical_oe = CASE
        WHEN fn_cfg('clinical_oe_metric') = 'composite' THEN oe_ratio_composite
        ELSE oe_ratio_los
    END;
