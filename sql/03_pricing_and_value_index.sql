-- =============================================================================
-- 03_pricing_and_value_index.sql
-- Normalise CMS MRF pricing, establish the market benchmark, execute the master
-- join, and compute the Value Index.
--
-- Run AFTER stg_cms_mrf and facility_crosswalk are populated. Idempotent.
--   psql -d nyc_healthcare_mvp -f sql/03_pricing_and_value_index.sql
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Helper: normalise a billing code to a zero-padded 3-digit DRG.
--
-- Replaces the `billing_code LIKE '%470%'` substring test, which also matches
-- 1470, 4701, 47012 and any CPT/HCPCS code containing those digits -- pulling
-- unrelated line items into the price median.
--
-- Accepts the formats hospitals actually publish: '470', '0470', 'MS-DRG 470',
-- 'MSDRG470', 'DRG-470'. Returns NULL when no DRG can be identified.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_normalize_drg(code TEXT)
RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT lpad(NULLIF(x, ''), 3, '0')
    FROM (
        SELECT CASE
            WHEN code IS NULL THEN NULL
            WHEN btrim(code) ~ '^0*[0-9]{1,3}$' THEN ltrim(btrim(code), '0')
            ELSE (regexp_match(upper(btrim(code)),
                               '(?:MS[ _-]?DRG|DRG)[^0-9]{0,4}([0-9]{1,3})'))[1]
        END AS x
    ) t;
$fn$;


-- =============================================================================
-- STEP 1: Facility-level pricing for the target MS-DRG
--
-- Collapses every payer/plan line for the DRG down to ONE row per facility.
-- That single-row-per-facility guarantee is what stops the master join from
-- fanning out: joining on facility_name alone while the pricing table still
-- held one row per (facility, billing_code) multiplied each facility's clinical
-- metrics by its number of matched codes.
-- =============================================================================
DROP TABLE IF EXISTS facility_pricing CASCADE;

CREATE TABLE facility_pricing AS
WITH target_rows AS (
    -- billing_code holds the normalised 3-digit number and billing_code_type
    -- holds the DRG SYSTEM, both resolved by the Python parser. The two systems
    -- must never be pooled: APR-DRG 470 and MS-DRG 470 are different procedures.
    SELECT
        *,
        CASE WHEN billing_code_type = 'MS-DRG' THEN 1 ELSE 2 END AS system_rank
    FROM stg_cms_mrf
    WHERE (
            (billing_code_type = 'MS-DRG'
             AND billing_code = lpad(fn_cfg('target_ms_drg'), 3, '0'))
         OR (billing_code_type = 'APR-DRG'
             AND billing_code = ANY (
                 SELECT lpad(t, 3, '0') FROM unnest(fn_cfg_list('target_apr_drgs')) AS t))
          )
      AND (setting IS NULL
           OR btrim(setting) = ''
           OR setting ~* 'inpatient')
),
-- A facility that publishes MS-DRG pricing uses only that; APR-DRG is the
-- fallback for facilities (NYU Langone among them) that publish nothing else.
preferred AS (
    SELECT facility_name, min(system_rank) AS use_rank
    FROM target_rows
    GROUP BY facility_name
),
chosen AS (
    SELECT t.*
    FROM target_rows t
    JOIN preferred p
      ON p.facility_name = t.facility_name
     AND p.use_rank = t.system_rank
)
SELECT
    facility_name,
    mode() WITHIN GROUP (ORDER BY billing_code_type)         AS pricing_code_system,
    mode() WITHIN GROUP (ORDER BY cms_certification_number)  AS cms_certification_number,
    count(*)                                                 AS price_line_count,
    count(DISTINCT payer_name)
        FILTER (WHERE payer_name IS NOT NULL)                AS payer_count,
    count(*) FILTER (WHERE payer_specific_negotiated_charge > 0)
                                                             AS negotiated_line_count,
    -- percentile_cont() has no numeric overload: it coerces its input to double
    -- precision and returns double precision. The explicit ::numeric cast is
    -- required, because round(double precision, integer) does not exist in
    -- PostgreSQL and errors out at runtime.
    (percentile_cont(0.5) WITHIN GROUP (
        ORDER BY CASE WHEN discounted_cash_price > 0
                      THEN discounted_cash_price END))::numeric(14,2)
                                                             AS median_cash_price,
    (percentile_cont(0.5) WITHIN GROUP (
        ORDER BY CASE WHEN payer_specific_negotiated_charge > 0
                      THEN payer_specific_negotiated_charge END))::numeric(14,2)
                                                             AS median_negotiated_charge,
    (min(payer_specific_negotiated_charge)
        FILTER (WHERE payer_specific_negotiated_charge > 0))::numeric(14,2)
                                                             AS min_negotiated_charge,
    (max(payer_specific_negotiated_charge)
        FILTER (WHERE payer_specific_negotiated_charge > 0))::numeric(14,2)
                                                             AS max_negotiated_charge
FROM chosen
GROUP BY facility_name;

ALTER TABLE facility_pricing ADD PRIMARY KEY (facility_name);

-- The single cost figure the algorithm compares against the market.
-- Negotiated rate preferred; discounted cash price is the documented fallback.
ALTER TABLE facility_pricing
    ADD COLUMN facility_procedure_cost NUMERIC(14,2),
    ADD COLUMN cost_basis TEXT;

UPDATE facility_pricing
SET facility_procedure_cost = coalesce(median_negotiated_charge, median_cash_price),
    cost_basis = CASE
        WHEN median_negotiated_charge IS NOT NULL THEN 'median_negotiated_charge'
        WHEN median_cash_price       IS NOT NULL THEN 'median_cash_price'
        ELSE NULL
    END;


-- =============================================================================
-- STEP 2: Market benchmark
--
-- Median of the FACILITY-level medians, not of all raw price lines. Taking the
-- median over raw lines lets a hospital that publishes 40 payer contracts
-- outvote one that publishes 3, so the "market median" drifts toward whichever
-- facilities happen to have the most verbose MRFs.
-- =============================================================================
DROP TABLE IF EXISTS market_benchmark CASCADE;

CREATE TABLE market_benchmark AS
SELECT
    count(*)                                                    AS facilities_priced,
    (percentile_cont(0.5) WITHIN GROUP (
        ORDER BY facility_procedure_cost))::numeric(14,2)        AS market_median_cost,
    (percentile_cont(0.25) WITHIN GROUP (
        ORDER BY facility_procedure_cost))::numeric(14,2)        AS market_p25_cost,
    (percentile_cont(0.75) WITHIN GROUP (
        ORDER BY facility_procedure_cost))::numeric(14,2)        AS market_p75_cost,
    min(facility_procedure_cost)                                AS market_min_cost,
    max(facility_procedure_cost)                                AS market_max_cost
FROM facility_pricing
WHERE facility_procedure_cost > 0;


-- =============================================================================
-- STEP 3: The master join + Value Index
--
--   Value Index = (1 / clinical O/E) * (market median / facility cost) * ln(1 + volume)
--
-- LEFT JOINs throughout, per the blueprint's "perfect LEFT JOIN without data
-- loss" requirement: every scored NYC facility survives to the output even if it
-- has no crosswalk entry or no published price, carrying explicit NULLs and a
-- data_status label instead of vanishing.
-- =============================================================================
DROP TABLE IF EXISTS master_orthopedic_market CASCADE;

CREATE TABLE master_orthopedic_market AS
WITH mapped AS (
    SELECT
        c.*,
        x.cms_facility_name,
        x.match_method,
        x.match_score,
        x.reviewed                     AS crosswalk_reviewed
    FROM facility_clinical_metrics c
    -- Joined on the PFI, not the name. SPARCS re-spells facilities between
    -- release years, so a name-keyed crosswalk goes stale the moment a new
    -- year is loaded.
    LEFT JOIN facility_crosswalk x ON x.pfi_number = c.pfi_number
),
priced AS (
    SELECT
        m.*,
        p.median_cash_price,
        p.median_negotiated_charge,
        p.min_negotiated_charge,
        p.max_negotiated_charge,
        p.facility_procedure_cost,
        p.cost_basis,
        p.pricing_code_system,
        p.payer_count,
        p.negotiated_line_count,
        p.cms_certification_number
    FROM mapped m
    LEFT JOIN facility_pricing p
           ON lower(btrim(p.facility_name)) = lower(btrim(m.cms_facility_name))
)
SELECT
    pr.pfi_number,
    pr.facility_name                AS sparcs_facility_name,
    pr.operating_certificate_number,
    pr.cms_facility_name,
    pr.cms_certification_number,
    pr.hospital_county,
    pr.primary_zip3,
    pr.first_discharge_year,
    pr.last_discharge_year,

    -- Clinical
    pr.patient_volume,
    pr.hip_volume,
    pr.knee_volume,
    pr.avg_severity,
    pr.observed_avg_los,
    pr.expected_avg_los,
    pr.oe_ratio_los,
    pr.observed_adverse_pct,
    pr.expected_adverse_pct,
    pr.oe_ratio_adverse,
    pr.oe_ratio_composite,
    pr.clinical_oe,

    -- Financial
    pr.median_cash_price,
    pr.median_negotiated_charge,
    pr.min_negotiated_charge,
    pr.max_negotiated_charge,
    pr.facility_procedure_cost,
    pr.cost_basis,
    pr.pricing_code_system,
    pr.payer_count,
    mb.market_median_cost,

    -- ----------------------------------------------------------------------
    -- Value Index components
    -- ----------------------------------------------------------------------
    round(1.0 / nullif(pr.clinical_oe, 0), 4)                       AS clinical_multiplier,
    round(mb.market_median_cost / nullif(pr.facility_procedure_cost, 0), 4)
                                                                    AS financial_multiplier,
    -- ln(1 + volume), not ln(volume). ln(volume) is 0 at a volume of 1, which
    -- zeroes the entire product; the original NULLIF(volume, 1) guard was worse
    -- still, turning single-case facilities into NULL rather than a low score.
    round(ln(1 + pr.patient_volume::numeric), 4)                    AS experience_modifier,

    round(
        (1.0 / nullif(pr.clinical_oe, 0))
        * (mb.market_median_cost / nullif(pr.facility_procedure_cost, 0))
        * ln(1 + pr.patient_volume::numeric)
    , 3)                                                            AS value_index,

    -- Quality-and-experience score for facilities with no published price, so
    -- non-compliant hospitals are still rankable on the clinical axis.
    round(
        (1.0 / nullif(pr.clinical_oe, 0))
        * ln(1 + pr.patient_volume::numeric)
    , 3)                                                            AS clinical_only_index,

    CASE
        WHEN pr.cms_facility_name IS NULL          THEN 'unmapped_facility'
        WHEN pr.facility_procedure_cost IS NULL    THEN 'no_published_price'
        WHEN pr.clinical_oe IS NULL                THEN 'no_clinical_benchmark'
        ELSE 'complete'
    END                                                             AS data_status,
    pr.match_method,
    pr.match_score,
    pr.crosswalk_reviewed,
    now()                                                           AS computed_at
FROM priced pr
-- market_benchmark is an ungrouped aggregate, so it always yields exactly one
-- row; ON TRUE keeps that explicit rather than relying on it.
LEFT JOIN market_benchmark mb ON TRUE;

ALTER TABLE master_orthopedic_market ADD PRIMARY KEY (pfi_number);
