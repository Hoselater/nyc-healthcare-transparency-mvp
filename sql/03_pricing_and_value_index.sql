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
-- STEP 1b: SPARCS Cost Transparency, as a fallback cost source
--
-- Keyed on the PFI, so it needs no crosswalk and covers every Article 28
-- facility -- where MRF crawling reaches a fraction of the market. Takes the
-- most recent year available per facility, summed across severity tiers and
-- weighted by discharge count, so a facility's figure reflects its actual mix
-- rather than an unweighted average of tiers it barely uses.
-- =============================================================================
DROP TABLE IF EXISTS facility_cost_sparcs CASCADE;

CREATE TABLE facility_cost_sparcs AS
WITH latest AS (
    SELECT pfi_number, max(data_year) AS data_year
    FROM stg_sparcs_cost
    WHERE median_charge > 0 OR median_cost > 0
    GROUP BY pfi_number
)
SELECT
    s.pfi_number,
    l.data_year,
    sum(s.discharges)                                             AS cost_discharges,
    round(sum(s.median_charge * s.discharges)
          / nullif(sum(s.discharges) FILTER (WHERE s.median_charge > 0), 0), 2)
                                                                  AS median_charge,
    round(sum(s.median_cost * s.discharges)
          / nullif(sum(s.discharges) FILTER (WHERE s.median_cost > 0), 0), 2)
                                                                  AS median_cost
FROM stg_sparcs_cost s
JOIN latest l ON l.pfi_number = s.pfi_number AND l.data_year = s.data_year
GROUP BY s.pfi_number, l.data_year;

ALTER TABLE facility_cost_sparcs ADD PRIMARY KEY (pfi_number);


-- =============================================================================
-- STEP 2: Market benchmark
--
-- Median of the FACILITY-level medians, not of all raw price lines. Taking the
-- median over raw lines lets a hospital that publishes 40 payer contracts
-- outvote one that publishes 3, so the "market median" drifts toward whichever
-- facilities happen to have the most verbose MRFs.
-- =============================================================================
-- One benchmark PER COST BASIS. A published list charge runs two to four times
-- a negotiated rate for the same procedure, so pooling them into a single
-- "market median" would make every MRF-priced hospital look like a bargain and
-- every charge-priced one look extortionate -- an artefact of which source the
-- figure came from, not of what anyone pays. Each facility is therefore
-- compared against the median of facilities measured the same way.
DROP TABLE IF EXISTS market_benchmark CASCADE;

CREATE TABLE market_benchmark AS
WITH all_costs AS (
    SELECT 'mrf_negotiated'::text AS cost_basis, facility_procedure_cost AS cost
    FROM facility_pricing
    WHERE facility_procedure_cost > 0
    UNION ALL
    SELECT 'sparcs_charge', median_charge
    FROM facility_cost_sparcs
    WHERE median_charge > 0
)
SELECT
    cost_basis,
    count(*)                                                    AS facilities_priced,
    (percentile_cont(0.5)  WITHIN GROUP (ORDER BY cost))::numeric(14,2) AS market_median_cost,
    (percentile_cont(0.25) WITHIN GROUP (ORDER BY cost))::numeric(14,2) AS market_p25_cost,
    (percentile_cont(0.75) WITHIN GROUP (ORDER BY cost))::numeric(14,2) AS market_p75_cost,
    min(cost)::numeric(14,2)                                    AS market_min_cost,
    max(cost)::numeric(14,2)                                    AS market_max_cost
FROM all_costs
GROUP BY cost_basis;

ALTER TABLE market_benchmark ADD PRIMARY KEY (cost_basis);


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
joined AS (
    SELECT
        m.*,
        p.median_cash_price,
        p.median_negotiated_charge,
        p.min_negotiated_charge,
        p.max_negotiated_charge,
        p.facility_procedure_cost   AS mrf_cost,
        p.cost_basis                AS mrf_cost_basis,
        p.pricing_code_system,
        p.payer_count,
        p.negotiated_line_count,
        p.cms_certification_number,
        sc.median_charge            AS sparcs_median_charge,
        sc.median_cost              AS sparcs_median_cost,
        sc.data_year                AS sparcs_cost_year
    FROM mapped m
    LEFT JOIN facility_pricing p
           ON lower(btrim(p.facility_name)) = lower(btrim(m.cms_facility_name))
    LEFT JOIN facility_cost_sparcs sc ON sc.pfi_number = m.pfi_number
),
priced AS (
    -- A negotiated rate is what a payer actually pays, so it wins wherever it
    -- exists. The SPARCS list charge is the documented fallback, and the basis
    -- is carried through so nothing downstream compares the two directly.
    SELECT
        j.*,
        coalesce(j.mrf_cost, j.sparcs_median_charge)  AS facility_procedure_cost,
        CASE
            WHEN j.mrf_cost IS NOT NULL             THEN 'mrf_negotiated'
            WHEN j.sparcs_median_charge IS NOT NULL THEN 'sparcs_charge'
            ELSE NULL
        END                                          AS cost_basis
    FROM joined j
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

    -- Each term carries a configurable exponent. With all three at 1.0 this is
    -- the plain product the plan specifies. Raising weight_clinical makes
    -- quality dominate; the first live run showed a facility with an O/E of
    -- 1.59 -- 59% worse length of stay than its case mix predicts -- placing
    -- second purely on price, which is a defensible formula producing an
    -- indefensible ranking.
    round(
        power(1.0 / nullif(pr.clinical_oe, 0), fn_cfg_num('weight_clinical'))
        * power(mb.market_median_cost / nullif(pr.facility_procedure_cost, 0),
                fn_cfg_num('weight_financial'))
        * power(ln(1 + pr.patient_volume::numeric), fn_cfg_num('weight_experience'))
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
-- Each facility is benchmarked against facilities measured the same way.
LEFT JOIN market_benchmark mb ON mb.cost_basis = pr.cost_basis;

ALTER TABLE master_orthopedic_market ADD PRIMARY KEY (pfi_number);


-- =============================================================================
-- STEP 4: PER-PROCEDURE pricing and scoring
--
-- A patient having a knee replaced does not care about the hip average. The
-- clinical side separates cleanly -- APR-DRG 324 and 326 are distinct codes.
--
-- Pricing separates only PARTLY, and the reason is worth stating plainly:
-- MS-DRG 470 is defined as "major hip OR knee joint replacement", one code for
-- both procedures, so a hospital pricing on MS-DRG has no hip/knee split to
-- give. APR-coded sources -- SPARCS Cost Transparency, and the hospitals whose
-- MRFs use APR-DRG -- do separate them. Each facility therefore carries the
-- most specific price available, labelled so the difference is visible rather
-- than implied.
-- =============================================================================
DROP TABLE IF EXISTS procedure_pricing CASCADE;

CREATE TABLE procedure_pricing AS
WITH drg_to_procedure(code, procedure) AS (
    VALUES ('324','hip'), ('301','hip'), ('326','knee'), ('302','knee')
),
-- (a) MRF rows carrying a procedure-specific APR code
mrf_proc AS (
    SELECT
        x.pfi_number,
        d.procedure,
        (percentile_cont(0.5) WITHIN GROUP (
            ORDER BY CASE WHEN m.payer_specific_negotiated_charge > 0
                          THEN m.payer_specific_negotiated_charge END))::numeric(14,2)
            AS cost
    FROM stg_cms_mrf m
    JOIN drg_to_procedure d
      ON d.code = m.billing_code AND m.billing_code_type = 'APR-DRG'
    JOIN facility_crosswalk x
      ON lower(btrim(x.cms_facility_name)) = lower(btrim(m.facility_name))
    WHERE (m.setting IS NULL OR btrim(m.setting) = '' OR m.setting ~* 'inpatient')
    GROUP BY x.pfi_number, d.procedure
),
-- (b) SPARCS Cost Transparency, which always carries the procedure code
sparcs_proc AS (
    SELECT
        s.pfi_number,
        d.procedure,
        round(sum(s.median_charge * s.discharges)
              / nullif(sum(s.discharges) FILTER (WHERE s.median_charge > 0), 0), 2)
            AS cost
    FROM stg_sparcs_cost s
    JOIN drg_to_procedure d ON d.code = lpad(s.apr_drg_code::text, 3, '0')
    JOIN (SELECT pfi_number, max(data_year) y FROM stg_sparcs_cost GROUP BY 1) l
      ON l.pfi_number = s.pfi_number AND l.y = s.data_year
    GROUP BY s.pfi_number, d.procedure
)
SELECT
    coalesce(mp.pfi_number, sp.pfi_number)        AS pfi_number,
    coalesce(mp.procedure,  sp.procedure)         AS procedure,
    coalesce(mp.cost,       sp.cost)              AS procedure_cost,
    CASE WHEN mp.cost IS NOT NULL THEN 'mrf_negotiated'
         WHEN sp.cost IS NOT NULL THEN 'sparcs_charge' END AS cost_basis
FROM mrf_proc mp
FULL OUTER JOIN sparcs_proc sp
  ON sp.pfi_number = mp.pfi_number AND sp.procedure = mp.procedure;

ALTER TABLE procedure_pricing ADD PRIMARY KEY (pfi_number, procedure);


-- Benchmarks per (procedure, basis): a knee list charge must be compared only
-- against other knee list charges.
DROP TABLE IF EXISTS procedure_benchmark CASCADE;

CREATE TABLE procedure_benchmark AS
SELECT
    procedure,
    cost_basis,
    count(*)                                                     AS facilities_priced,
    (percentile_cont(0.5) WITHIN GROUP (ORDER BY procedure_cost))::numeric(14,2)
                                                                 AS market_median_cost
FROM procedure_pricing
WHERE procedure_cost > 0
GROUP BY procedure, cost_basis;

ALTER TABLE procedure_benchmark ADD PRIMARY KEY (procedure, cost_basis);


DROP TABLE IF EXISTS master_procedure_market CASCADE;

CREATE TABLE master_procedure_market AS
SELECT
    m.pfi_number,
    m.procedure,
    m.procedure_label,
    m.facility_name,
    m.hospital_county,
    m.primary_zip3,
    m.patient_volume,
    m.avg_severity,
    m.observed_avg_los,
    m.expected_avg_los,
    m.oe_ratio_los,
    m.observed_adverse_pct,
    m.oe_ratio_adverse,
    m.clinical_oe,
    p.procedure_cost,
    p.cost_basis,
    b.market_median_cost,
    round(power(1.0 / nullif(m.clinical_oe, 0), fn_cfg_num('weight_clinical'))
        * power(b.market_median_cost / nullif(p.procedure_cost, 0),
                fn_cfg_num('weight_financial'))
        * power(ln(1 + m.patient_volume::numeric), fn_cfg_num('weight_experience'))
    , 3)                                                         AS value_index
FROM facility_procedure_metrics m
LEFT JOIN procedure_pricing p
       ON p.pfi_number = m.pfi_number AND p.procedure = m.procedure
LEFT JOIN procedure_benchmark b
       ON b.procedure = p.procedure AND b.cost_basis = p.cost_basis;

ALTER TABLE master_procedure_market ADD PRIMARY KEY (pfi_number, procedure);


-- =============================================================================
-- STEP 5: CMS Care Compare outcomes, pivoted to one row per facility
--
-- The genuine outcome measures the SPARCS public file does not contain:
-- risk-standardised complication rate and 30-day readmission rate for elective
-- hip and knee replacement. These do not feed the Value Index -- they are not
-- available for every facility, and silently scoring some hospitals on richer
-- evidence than others would be worse than not using them. They are reported
-- alongside it, which is what a reader actually needs to judge the index.
-- =============================================================================
DROP TABLE IF EXISTS facility_quality CASCADE;

CREATE TABLE facility_quality AS
SELECT
    x.pfi_number,
    x.cms_certification_number,
    max(q.score) FILTER (WHERE q.measure_id = 'COMP_HIP_KNEE')      AS cms_complication_rate,
    max(q.compared_to_national) FILTER (WHERE q.measure_id = 'COMP_HIP_KNEE')
                                                                    AS cms_complication_vs_national,
    max(q.denominator) FILTER (WHERE q.measure_id = 'COMP_HIP_KNEE') AS cms_complication_cases,
    max(q.score) FILTER (WHERE q.measure_id = 'READM_30_HIP_KNEE')  AS cms_readmission_rate,
    max(q.compared_to_national) FILTER (WHERE q.measure_id = 'READM_30_HIP_KNEE')
                                                                    AS cms_readmission_vs_national,
    max(q.score) FILTER (WHERE q.measure_id = 'CMS_OVERALL_RATING') AS cms_overall_rating
FROM facility_ccn_crosswalk x
JOIN stg_cms_quality q
  ON q.cms_certification_number = x.cms_certification_number
GROUP BY x.pfi_number, x.cms_certification_number;

ALTER TABLE facility_quality ADD PRIMARY KEY (pfi_number);
