-- =============================================================================
-- 99_smoke_test.sql
-- End-to-end verification of the SQL pipeline against synthetic data whose
-- expected answers are computed by hand below. No real data, no Python needed.
--
-- Creates a throwaway schema, runs the whole chain inside it, asserts the
-- arithmetic, and drops the schema. Any failure raises an exception.
--
--   psql -d postgres -v ON_ERROR_STOP=1 -f sql/99_smoke_test.sql
--
-- Each assertion also exercises a specific bug that existed in the original
-- scripts; the ASSERT names say which.
-- =============================================================================

\set ON_ERROR_STOP on

DROP SCHEMA IF EXISTS smoke_test CASCADE;
CREATE SCHEMA smoke_test;
SET search_path TO smoke_test, public;

\echo '--- building schema -------------------------------------------------'
\i sql/01_staging_schema.sql

-- The production volume floor is 10; the fixtures below are deliberately tiny
-- so the arithmetic stays hand-checkable. Drop the floor to 3 so Facilities A
-- and B (volume 4) are scored while Facility C (volume 2) is still excluded --
-- which is what assertion 6 checks.
UPDATE pipeline_config SET value = '3' WHERE key = 'min_facility_volume';

-- Pin the clinical metric to 'los' so the hand-computed value_index in
-- assertion 10 stays deterministic regardless of the shipped default. The
-- composite metric is asserted separately, in assertion 14.
UPDATE pipeline_config SET value = 'los' WHERE key = 'clinical_oe_metric';

\echo '--- seeding synthetic SPARCS ----------------------------------------'

-- Facility A (Manhattan): 4 elective discharges.
--   severity 1: LOS 2, 4   severity 2: LOS 6, 8   -> observed mean LOS = 5.0
-- Facility B (Brooklyn): 4 elective discharges, all severity 1, LOS 1,1,1,1
--   -> observed mean LOS = 1.0
-- Facility C (Queens): 2 discharges -> below min_facility_volume, must drop.
-- Facility D (Albany, non-NYC): 4 severity-2 discharges, LOS 10 each, plus one
--   HIPAA-censored '120+' discharge parked at severity 4.
--   Contributes to the STATEWIDE benchmark but must not be scored. The censored
--   row sits here, at a severity tier no scored facility uses, so that it proves
--   '120+' parses and enters the cohort without perturbing the tier-1/tier-2
--   benchmarks that the hand-computed O/E ratios below depend on.
-- Plus deliberately hostile rows: a blank LOS, an Emergency admission, an
-- Extreme mortality-risk case, and an out-of-scope DRG with an 'OOS' ZIP.

-- APR-DRG 324 = ELECTIVE HIP, 326 = ELECTIVE KNEE (NOT 301/302, which do not
-- exist in SPARCS). Facility A is deliberately spelled two different ways
-- across the two discharge years, mirroring the real SPARCS name drift, to
-- prove that grouping is keyed on the PFI rather than the name.
INSERT INTO stg_sparcs (permanent_facility_id, facility_name, hospital_county,
    hospital_service_area, zip_code_3_digit, type_of_admission, apr_drg_code,
    apr_severity_of_illness_code, apr_risk_of_mortality, length_of_stay_raw,
    patient_disposition, discharge_year)
VALUES
    -- Facility A -- 2022 spelling, borough labelled 'Manhattan'
    (1001,'Facility A Hospital','Manhattan','New York City','100','Elective',326,1,'Minor','2','Home or Self Care',2022),
    (1001,'Facility A Hospital','Manhattan','New York City','100','Elective',326,1,'Minor','4','Home or Self Care',2022),
    -- Facility A -- 2024 spelling (upper case), borough labelled 'New York'
    (1001,'FACILITY A HOSPITAL','New York','New York City','100','Elective',324,2,'Moderate','6','Skilled Nursing Home',2024),
    (1001,'FACILITY A HOSPITAL','New York','New York City','100','Elective',324,2,'Moderate','8','Home w/ Home Health Services',2024),
    -- Facility B
    (1002,'FACILITY B MEDICAL CENTER','Kings','New York City','112','Elective',326,1,'Minor','1','Home or Self Care',2022),
    (1002,'FACILITY B MEDICAL CENTER','Kings','New York City','112','Elective',326,1,'Minor','1','Home or Self Care',2022),
    (1002,'FACILITY B MEDICAL CENTER','Kings','New York City','112','Elective',326,1,'Minor','1','Home or Self Care',2024),
    (1002,'FACILITY B MEDICAL CENTER','Kings','New York City','112','Elective',326,1,'Minor','1','Hospice - Home',2024),
    -- Facility C -- volume 2, below the floor
    (1003,'FACILITY C SMALL','Queens','New York City','113','Elective',326,1,'Minor','3','Home or Self Care',2022),
    (1003,'FACILITY C SMALL','Queens','New York City','113','Elective',326,1,'Minor','3','Home or Self Care',2024),
    -- Facility D -- upstate, benchmark only
    (1004,'FACILITY D UPSTATE','Albany','Capital/Adirond','122','Elective',324,2,'Moderate','10','Home or Self Care',2022),
    (1004,'FACILITY D UPSTATE','Albany','Capital/Adirond','122','Elective',324,2,'Moderate','10','Home or Self Care',2022),
    (1004,'FACILITY D UPSTATE','Albany','Capital/Adirond','122','Elective',324,2,'Moderate','10','Home or Self Care',2024),
    (1004,'FACILITY D UPSTATE','Albany','Capital/Adirond','122','Elective',324,2,'Moderate','10','Home or Self Care',2024),
    -- Censored LOS: must parse to 120 and be KEPT (dropping it would bias the
    -- cohort against facilities with long-stay outliers)
    (1004,'FACILITY D UPSTATE','Albany','Capital/Adirond','122','Elective',324,4,'Major','120+','Skilled Nursing Home',2022),
    -- Hostile rows, all of which must be excluded from the cohort
    (1001,'Facility A Hospital','Manhattan','New York City','100','Elective',326,1,'Minor','','Home or Self Care',2022),
    (1001,'Facility A Hospital','Manhattan','New York City','100','Emergency',326,1,'Minor','5','Home or Self Care',2022),
    (1001,'Facility A Hospital','Manhattan','New York City','100','Elective',326,4,'Extreme','30','Expired',2022),
    (1001,'Facility A Hospital','Manhattan','New York City','OOS','Elective',999,1,'Minor','3','Home or Self Care',2022),
    -- Non-elective/complex knee (325) must NOT enter the elective cohort
    (1001,'Facility A Hospital','Manhattan','New York City','100','Elective',325,1,'Minor','9','Home or Self Care',2022),
    -- No PFI: cannot be grouped, must be dropped
    (NULL,'FACILITY E NOPFI','Bronx','New York City','104','Elective',326,1,'Minor','2','Home or Self Care',2022);

\echo '--- seeding synthetic CMS MRF ---------------------------------------'

-- billing_code holds the NORMALISED 3-digit number and billing_code_type the
-- DRG system, both resolved by the Python parser before insert. Raw forms like
-- 'MS-DRG 470', '0470' or '4700' never reach this table -- those are rejected
-- upstream, and etl/mrf.py has unit coverage proving it. What the SQL must get
-- right is the system and setting filtering, and the MS-DRG-over-APR preference.
--
-- Facility A: three MS-DRG 470 payer lines -> median negotiated 30000.
--             Also publishes APR-DRG 324, which MS-DRG must outrank.
-- Facility B: APR-DRG only (NYU Langone's real situation) -> median 60000.
-- Facility A publishes a more verbose MRF than B, proving the market median is
-- taken over facility medians rather than raw price lines.

INSERT INTO stg_cms_mrf (facility_name, billing_code, billing_code_type, setting,
    payer_name, plan_name, discounted_cash_price, payer_specific_negotiated_charge)
VALUES
    ('Facility A Hospital','470','MS-DRG','inpatient','Aetna','PPO',25000,28000),
    ('Facility A Hospital','470','MS-DRG','inpatient','Cigna','HMO',25000,30000),
    ('Facility A Hospital','470','MS-DRG','inpatient','United','EPO',25000,32000),
    -- Same facility, APR-DRG 324. A target code, but MS-DRG outranks it, so
    -- these must not dilute Facility A's median.
    ('Facility A Hospital','324','APR-DRG','inpatient','Aetna','PPO',99999,99999),
    -- Decoys the system and setting filters must reject
    ('Facility A Hospital','470','CPT','outpatient','Aetna','PPO',888888,888888),
    ('Facility A Hospital','470','APR-DRG','inpatient','Aetna','PPO',777777,777777),
    ('Facility A Hospital','470','MS-DRG','outpatient','Aetna','PPO',666666,666666),
    -- Facility B publishes APR-DRG only: the fallback path must price it.
    ('Facility B Medical Center','324','APR-DRG','inpatient','Aetna','PPO',55000,55000),
    ('Facility B Medical Center','326','APR-DRG','inpatient','Cigna','HMO',55000,65000);

\echo '--- seeding crosswalk -----------------------------------------------'
INSERT INTO facility_crosswalk (pfi_number, sparcs_facility_name, cms_facility_name,
    match_method, match_score, reviewed)
VALUES
    (1001,'FACILITY A HOSPITAL','Facility A Hospital','manual',100,TRUE),
    (1002,'FACILITY B MEDICAL CENTER','Facility B Medical Center','manual',100,TRUE);

-- Benchmarks, computed by hand from the fixtures above and stratified by
-- (severity, discharge_year):
--   2022 sev1: A(2,4) B(1,1) C(3)      -> 11/5  = 2.2
--   2022 sev2: D(10,10)                -> 20/2  = 10.0
--   2022 sev4: D(120)                  -> 120.0
--   2024 sev1: B(1,1) C(3)             ->  5/3  = 1.6667
--   2024 sev2: A(6,8) D(10,10)         -> 34/4  = 8.5

\echo '--- running transformations -----------------------------------------'
\i sql/02_transformations.sql
\i sql/03_pricing_and_value_index.sql
\i sql/04_export_views.sql

\echo '--- asserting -------------------------------------------------------'
DO $$
DECLARE
    v_num   NUMERIC;
    v_int   BIGINT;
    v_txt   TEXT;
BEGIN
    -- 1. HIPAA-redacted LOS must parse rather than abort the query.
    --    Original bug: a bare CAST(length_of_stay AS NUMERIC) throws
    --    "invalid input syntax for type numeric: 120+" on the first such row.
    ASSERT fn_parse_los('120+') = 120, 'los_parse: 120+ should parse to 120';
    ASSERT fn_parse_los('')     IS NULL, 'los_parse: blank should be NULL';
    ASSERT fn_parse_los('3')    = 3,     'los_parse: 3 should parse to 3';

    -- Cohort = A(4) + B(4) + C(2) + D(5, incl. the censored row) = 15.
    -- Excluded: blank LOS, Emergency, Extreme mortality, DRG 999, DRG 325
    -- (non-elective knee), and the row with no PFI.
    SELECT count(*) INTO v_int FROM ortho_cohort;
    ASSERT v_int = 15, format('cohort_filter: expected 15 cohort rows, got %s', v_int);

    SELECT count(*) INTO v_int FROM ortho_cohort WHERE los_days = 120;
    ASSERT v_int = 1, format('los_parse: censored row should be kept, got %s', v_int);

    -- 2a. The non-elective/complex counterpart DRG must not leak in.
    SELECT count(*) INTO v_int FROM ortho_cohort WHERE apr_drg_code = 325;
    ASSERT v_int = 0, format('drg_scope: DRG 325 must be excluded, got %s rows', v_int);

    -- 2b. A discharge with no PFI cannot be grouped and must be dropped.
    SELECT count(*) INTO v_int FROM ortho_cohort WHERE permanent_facility_id IS NULL;
    ASSERT v_int = 0, format('pfi_required: expected 0 null-PFI rows, got %s', v_int);

    -- 2c. Facility A keeps exactly 4 rows, matched on PFI not name.
    SELECT count(*) INTO v_int FROM ortho_cohort WHERE permanent_facility_id = 1001;
    ASSERT v_int = 4, format('cohort_filter: Facility A should keep 4 rows, got %s', v_int);

    -- 2d. Both borough spellings count as NYC. 'Manhattan' (2022) and
    --     'New York' (2024) are the same borough; a single-spelling county
    --     list silently drops one year's worth of Manhattan facilities.
    SELECT count(*) INTO v_int FROM ortho_cohort
        WHERE permanent_facility_id = 1001 AND is_nyc;
    ASSERT v_int = 4, format('geography: both Manhattan and New York must be NYC, got %s', v_int);

    -- 3. Benchmarks are stratified by (severity, discharge_year).
    --    2022 sev1: A(2,4) B(1,1) C(3) = 11/5 = 2.2
    --    2024 sev1: B(1,1) C(3)        =  5/3 = 1.6667
    --    2022 sev2: D(10,10)           = 10.0
    --    2024 sev2: A(6,8) D(10,10)    = 34/4 = 8.5
    SELECT expected_los INTO v_num FROM severity_benchmarks
        WHERE severity = 1 AND discharge_year = 2022;
    ASSERT v_num = 2.2, format('benchmark: 2022 sev-1 LOS should be 2.2, got %s', v_num);

    SELECT expected_los INTO v_num FROM severity_benchmarks
        WHERE severity = 2 AND discharge_year = 2024;
    ASSERT v_num = 8.5, format('benchmark: 2024 sev-2 LOS should be 8.5, got %s', v_num);

    -- Statewide scope: upstate Facility D must be inside the benchmark.
    SELECT benchmark_discharges INTO v_int FROM severity_benchmarks
        WHERE severity = 2 AND discharge_year = 2024;
    ASSERT v_int = 4, format('benchmark_scope: 2024 sev-2 should pool 4 discharges, got %s', v_int);

    -- 4. Indirect standardization for Facility A:
    --    observed = (2+4+6+8)/4 = 5.0
    --    expected = (2.2 + 2.2 + 8.5 + 8.5)/4 = 5.35   <- case-mix AND year weighted
    --    O/E      = 5.0/5.35 = 0.935
    SELECT observed_avg_los INTO v_num FROM facility_clinical_metrics
        WHERE pfi_number = 1001;
    ASSERT v_num = 5.00, format('oe: Facility A observed LOS should be 5.00, got %s', v_num);

    SELECT expected_avg_los INTO v_num FROM facility_clinical_metrics
        WHERE pfi_number = 1001;
    ASSERT v_num = 5.35, format('oe: Facility A expected LOS should be 5.35, got %s', v_num);

    SELECT oe_ratio_los INTO v_num FROM facility_clinical_metrics
        WHERE pfi_number = 1001;
    ASSERT v_num = 0.935, format('oe: Facility A O/E should be 0.935, got %s', v_num);

    -- 4b. PFI grouping: Facility A is spelled two ways across the two years but
    --     must remain ONE facility with the full volume of 4. Grouping by name
    --     would produce two rows of 2, halving the experience modifier and
    --     computing both O/E ratios against the wrong denominators.
    SELECT count(*) INTO v_int FROM facility_clinical_metrics WHERE pfi_number = 1001;
    ASSERT v_int = 1, format('pfi_grouping: Facility A should be 1 row, got %s', v_int);

    SELECT patient_volume INTO v_int FROM facility_clinical_metrics WHERE pfi_number = 1001;
    ASSERT v_int = 4, format('pfi_grouping: Facility A volume should be 4, got %s', v_int);

    -- 4c. Display name comes from the most recent release year.
    SELECT facility_name INTO v_txt FROM facility_clinical_metrics WHERE pfi_number = 1001;
    ASSERT v_txt = 'FACILITY A HOSPITAL',
        format('display_name: expected the 2024 spelling, got %s', v_txt);

    -- 4d. Hip/knee split reads the corrected DRG codes (324 hip, 326 knee).
    SELECT hip_volume INTO v_int FROM facility_clinical_metrics WHERE pfi_number = 1001;
    ASSERT v_int = 2, format('drg_split: Facility A hip volume should be 2, got %s', v_int);

    -- 5. Adverse-disposition classification. Original bug: ILIKE '%Home%'
    --    scored 'Skilled Nursing Home' and 'Hospice - Home' as good outcomes.
    --    Facility A: Home, Home, SkilledNursingHome(adverse), HomeHealth -> 25%
    SELECT observed_adverse_pct INTO v_num FROM facility_clinical_metrics
        WHERE pfi_number = 1001;
    ASSERT v_num = 25.00, format('disposition: Facility A adverse pct should be 25.00, got %s', v_num);

    --    Facility B: 3 x Home, 1 x 'Hospice - Home' (adverse) -> 25%
    SELECT observed_adverse_pct INTO v_num FROM facility_clinical_metrics
        WHERE pfi_number = 1002;
    ASSERT v_num = 25.00, format('disposition: Hospice - Home must count as adverse, got %s', v_num);

    -- 6. min_facility_volume drops Facility C; non-NYC drops Facility D.
    SELECT count(*) INTO v_int FROM facility_clinical_metrics;
    ASSERT v_int = 2, format('volume_floor/geography: expected 2 scored facilities, got %s', v_int);

    -- 7. System and setting filtering, plus MS-DRG outranking APR-DRG.
    --    Facility A must keep exactly the 3 inpatient MS-DRG 470 lines: the
    --    outpatient row, the CPT row, the APR-DRG 470 row (a different
    --    procedure) and even the legitimate APR-DRG 324 row are all excluded.
    SELECT price_line_count INTO v_int FROM facility_pricing
        WHERE facility_name = 'Facility A Hospital';
    ASSERT v_int = 3, format('drg_match: Facility A should have 3 price lines, got %s', v_int);

    SELECT pricing_code_system INTO v_txt FROM facility_pricing
        WHERE facility_name = 'Facility A Hospital';
    ASSERT v_txt = 'MS-DRG', format('system_rank: Facility A should price on MS-DRG, got %s', v_txt);

    -- 7b. Facility B publishes APR-DRG only, so the fallback must price it.
    SELECT pricing_code_system INTO v_txt FROM facility_pricing
        WHERE facility_name = 'Facility B Medical Center';
    ASSERT v_txt = 'APR-DRG', format('apr_fallback: Facility B should price on APR-DRG, got %s', v_txt);

    SELECT median_negotiated_charge INTO v_num FROM facility_pricing
        WHERE facility_name = 'Facility A Hospital';
    ASSERT v_num = 30000.00, format('drg_match: Facility A median negotiated should be 30000, got %s', v_num);

    -- 8. Market median over FACILITY medians: median(30000, 60000) = 45000.
    --    Over raw price lines it would be 32000 -- asserts the de-weighting.
    SELECT market_median_cost INTO v_num FROM market_benchmark;
    ASSERT v_num = 45000.00, format('market_median: expected 45000, got %s', v_num);

    -- 9. No fan-out. Original bug: pricing keyed on (facility, billing_code)
    --    joined on facility alone, multiplying rows.
    SELECT count(*) INTO v_int FROM master_orthopedic_market;
    ASSERT v_int = 2, format('fan_out: master table should have 2 rows, got %s', v_int);

    -- 10. Value Index for Facility A:
    --     clinical  = 1/0.935      = 1.0695
    --     financial = 45000/30000  = 1.5
    --     experience= ln(1+4)      = 1.6094
    --     index     = 1.0695 * 1.5 * 1.6094 = 2.582
    SELECT value_index INTO v_num FROM master_orthopedic_market
        WHERE pfi_number = 1001;
    ASSERT abs(v_num - 2.582) < 0.01, format('value_index: Facility A expected ~2.582, got %s', v_num);

    -- 11. Cheaper-but-slower Facility B must still be comparable, and the round()
    --     must not error. Original bug: percentile_cont returns double precision,
    --     and round(double precision, integer) does not exist in PostgreSQL --
    --     this line is what made the original script fail outright.
    SELECT value_index INTO v_num FROM master_orthopedic_market
        WHERE pfi_number = 1002;
    ASSERT v_num IS NOT NULL, 'round_type: Facility B value_index came back NULL';

    -- 12. Geography survives to the export so Tableau has a ZCTA join key.
    SELECT primary_zip3 INTO v_txt FROM vw_facility_scores WHERE pfi_number = 1001;
    ASSERT v_txt = '100', format('geography: expected zip3 100, got %s', v_txt);

    -- 13. Star ratings assigned.
    SELECT count(*) INTO v_int FROM vw_facility_scores WHERE star_rating IS NOT NULL;
    ASSERT v_int = 2, format('star_rating: expected 2 rated facilities, got %s', v_int);

    -- 14. Composite clinical O/E for Facility A.
    --     LOS O/E = 5.0 / 5.35 = 0.93458
    --     expected adverse, 2022 sev1 = 0 of 5      = 0.0
    --     expected adverse, 2024 sev2 = 1 of 4      = 0.25
    --     Facility A expected adverse = mean(0,0,.25,.25) = 0.125
    --     adverse O/E = 0.25 / 0.125 = 2.0
    --     composite   = sqrt(0.93458 * 2.0) = 1.3672
    SELECT oe_ratio_composite INTO v_num FROM facility_clinical_metrics
        WHERE pfi_number = 1001;
    ASSERT abs(v_num - 1.367) < 0.01,
        format('composite_oe: Facility A expected ~1.367, got %s', v_num);

    -- 15. Benchmarks stratified by (severity, discharge_year): 2022 has tiers
    --     1, 2 and 4; 2024 has tiers 1 and 2. Five strata in total.
    SELECT count(*) INTO v_int FROM severity_benchmarks;
    ASSERT v_int = 5, format('benchmark_strata: expected 5 (severity, year) rows, got %s', v_int);

    SELECT count(*) INTO v_int FROM severity_benchmarks WHERE discharge_year = 2022;
    ASSERT v_int = 3, format('benchmark_strata: expected 3 strata for 2022, got %s', v_int);

    RAISE NOTICE 'ALL SMOKE TESTS PASSED';
END $$;

\echo '--- results ---------------------------------------------------------'
SELECT pfi_number, facility_name, patient_volume, observed_avg_los,
       expected_avg_los, oe_ratio_los, observed_adverse_pct,
       facility_procedure_cost, market_median_cost, value_index, star_rating
FROM vw_facility_scores
ORDER BY value_index DESC NULLS LAST;

\echo '--- cleanup ---------------------------------------------------------'
DROP SCHEMA smoke_test CASCADE;
RESET search_path;
\echo 'Smoke test complete.'
