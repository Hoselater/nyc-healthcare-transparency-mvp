-- =============================================================================
-- 05_qa_checks.sql
-- Data-quality diagnostics. Read these before trusting a run.
--   psql -d nyc_healthcare_mvp -f sql/05_qa_checks.sql
-- =============================================================================

\echo '== 1. Row counts by stage =='
SELECT 'stg_sparcs'               AS stage, count(*) FROM stg_sparcs
UNION ALL SELECT 'ortho_cohort',            count(*) FROM ortho_cohort
UNION ALL SELECT 'ortho_cohort (NYC)',      count(*) FROM ortho_cohort WHERE is_nyc
UNION ALL SELECT 'facility_clinical_metrics', count(*) FROM facility_clinical_metrics
UNION ALL SELECT 'stg_cms_mrf',             count(*) FROM stg_cms_mrf
UNION ALL SELECT 'facility_pricing',        count(*) FROM facility_pricing
UNION ALL SELECT 'facility_crosswalk',      count(*) FROM facility_crosswalk
UNION ALL SELECT 'master_orthopedic_market', count(*) FROM master_orthopedic_market;

\echo ''
\echo '== 1b. Cohort by discharge year =='
\echo '      With SPARCS_YEARS stacked, each configured year should appear here.'
\echo '      A missing year means that dataset failed to load -- check the ETL log.'
SELECT discharge_year,
       count(*)                          AS discharges,
       count(*) FILTER (WHERE is_nyc)    AS nyc_discharges,
       count(DISTINCT permanent_facility_id) FILTER (WHERE is_nyc) AS nyc_facilities,
       round(avg(los_days), 2)           AS avg_los
FROM ortho_cohort
GROUP BY discharge_year
ORDER BY discharge_year;

\echo ''
\echo '== 2. SPARCS rows dropped by the cohort filter, and why =='
SELECT
    count(*) FILTER (WHERE apr_drg_code IS NULL
                        OR NOT (apr_drg_code = ANY (fn_cfg_int_list('apr_drg_codes'))))          AS wrong_drg,
    count(*) FILTER (WHERE fn_parse_los(length_of_stay_raw) IS NULL)                             AS unparseable_los,
    count(*) FILTER (WHERE length_of_stay_raw ~ '^[0-9]+ *[+]$')                                 AS censored_120_plus,
    count(*) FILTER (WHERE apr_severity_of_illness_code IS NULL
                        OR apr_severity_of_illness_code NOT BETWEEN 1 AND 4)                     AS bad_severity,
    count(*) FILTER (WHERE type_of_admission IS NOT NULL
                        AND type_of_admission NOT ILIKE 'Elective')                              AS non_elective,
    count(*) FILTER (WHERE apr_risk_of_mortality ILIKE 'Extreme')                                AS extreme_mortality_risk
FROM stg_sparcs;

\echo ''
\echo '== 3. Severity benchmarks (the Expected side of every O/E ratio) =='
SELECT * FROM severity_benchmarks ORDER BY severity, discharge_year;

\echo ''
\echo '== 4. Patient dispositions seen, and how each was classified =='
\echo '     Any unexpected value landing in adverse=1 is worth eyeballing.'
SELECT
    patient_disposition,
    fn_is_adverse_disposition(patient_disposition) AS adverse_flag,
    count(*) AS discharges
FROM ortho_cohort
GROUP BY 1, 2
ORDER BY discharges DESC;

\echo ''
\echo '== 5. Crosswalk coverage: NYC facilities with no CMS mapping =='
SELECT c.pfi_number, c.facility_name, c.patient_volume
FROM facility_clinical_metrics c
LEFT JOIN facility_crosswalk x
       ON x.pfi_number = c.pfi_number
WHERE x.pfi_number IS NULL
ORDER BY c.patient_volume DESC;

\echo ''
\echo '== 6. Fuzzy matches still awaiting human review =='
SELECT sparcs_facility_name, cms_facility_name, match_method, match_score
FROM facility_crosswalk
WHERE reviewed = FALSE AND match_method = 'fuzzy'
ORDER BY match_score ASC;

\echo ''
\echo '== 7. Pricing coverage: mapped facilities with no MRF price =='
SELECT sparcs_facility_name, cms_facility_name, patient_volume, data_status
FROM master_orthopedic_market
WHERE facility_procedure_cost IS NULL
ORDER BY patient_volume DESC;

\echo ''
\echo '== 8. Fan-out guard: any facility appearing more than once? =='
\echo '     Must return zero rows. A non-empty result means a duplicate crosswalk'
\echo '     or pricing key is multiplying the master join.'
SELECT pfi_number, sparcs_facility_name, count(*)
FROM master_orthopedic_market
GROUP BY 1, 2 HAVING count(*) > 1;

\echo ''
\echo '== 9. Market benchmark and price dispersion =='
SELECT * FROM market_benchmark;

\echo ''
\echo '== 10. Implausible outputs worth investigating =='
SELECT sparcs_facility_name, patient_volume, oe_ratio_los,
       facility_procedure_cost, value_index
FROM master_orthopedic_market
WHERE oe_ratio_los > 3 OR oe_ratio_los < 0.25
   OR facility_procedure_cost > 500000
   OR facility_procedure_cost < 5000
ORDER BY value_index DESC NULLS LAST;

\echo ''
\echo '== 11. Top 15 by Value Index =='
SELECT facility_name, patient_volume, oe_ratio_los,
       facility_procedure_cost, market_median_cost, value_index, star_rating
FROM vw_facility_scores
WHERE value_index IS NOT NULL
ORDER BY value_index DESC
LIMIT 15;
