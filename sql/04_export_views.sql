-- =============================================================================
-- 04_export_views.sql
-- Consumer-facing ranking + the flat view Tableau reads.
--
-- Run AFTER 03_. Idempotent.
--   psql -d nyc_healthcare_mvp -f sql/04_export_views.sql
-- =============================================================================

-- -----------------------------------------------------------------------------
-- vw_facility_scores -- ranked, star-rated, Tableau-ready.
--
-- Adds the pieces the roadmap's visualisation and Figma phases need but that the
-- raw master table has no place for:
--   * primary_zip3, so the ZCTA shapefile join in Tableau has a key to bind to
--     (the original master table carried no geography at all, which made the
--     documented map step impossible).
--   * star_rating, the "abstract the math into 5 stars" consumer heuristic.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_facility_scores AS
WITH ranked AS (
    -- Rank only fully-scored facilities; unpriced ones must not dilute the
    -- quintile boundaries that produce the star rating.
    SELECT
        m.*,
        rank()         OVER (ORDER BY m.value_index DESC) AS value_rank,
        percent_rank() OVER (ORDER BY m.value_index ASC)  AS value_percentile,
        ntile(5)       OVER (ORDER BY m.value_index ASC)  AS value_quintile
    FROM master_orthopedic_market m
    WHERE m.value_index IS NOT NULL
),
unranked AS (
    -- Carried through so the QA view and the "no published price" story stay
    -- visible; excluded from vw_tableau_export below.
    SELECT
        m.*,
        NULL::bigint           AS value_rank,
        NULL::double precision AS value_percentile,
        NULL::integer          AS value_quintile
    FROM master_orthopedic_market m
    WHERE m.value_index IS NULL
),
combined AS (
    SELECT * FROM ranked
    UNION ALL
    SELECT * FROM unranked
)
SELECT
    c.sparcs_facility_name          AS facility_name,
    c.cms_facility_name,
    c.pfi_number,
    c.hospital_county,
    c.primary_zip3,

    -- The year span matters for comparability: with multiple SPARCS releases
    -- stacked, a facility present in only one of them has a volume drawn from a
    -- shorter window than its peers, which understates its experience modifier.
    c.first_discharge_year,
    c.last_discharge_year,
    (c.last_discharge_year - c.first_discharge_year + 1) AS years_observed,

    c.patient_volume,
    c.hip_volume,
    c.knee_volume,
    c.avg_severity,

    c.observed_avg_los,
    c.expected_avg_los,
    c.oe_ratio_los,
    c.observed_adverse_pct,
    c.expected_adverse_pct,
    c.oe_ratio_adverse,
    c.clinical_oe,

    c.median_cash_price,
    c.median_negotiated_charge,
    c.facility_procedure_cost,
    c.cost_basis,
    c.pricing_code_system,
    c.payer_count,
    c.market_median_cost,
    round(
        100.0 * (c.facility_procedure_cost - c.market_median_cost)
        / nullif(c.market_median_cost, 0)
    , 1)                            AS pct_vs_market_median,

    c.clinical_multiplier,
    c.financial_multiplier,
    c.experience_modifier,
    c.value_index,
    c.clinical_only_index,
    c.value_rank,
    round((c.value_percentile * 100)::numeric, 1) AS value_percentile,
    c.value_quintile                AS star_rating,
    c.data_status,
    c.match_method,
    c.crosswalk_reviewed
FROM combined c;


-- -----------------------------------------------------------------------------
-- vw_tableau_export -- the exact column set exported to CSV.
--
-- Only complete, review-safe rows, ordered for legibility.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_tableau_export AS
SELECT *
FROM vw_facility_scores
WHERE value_index IS NOT NULL
ORDER BY value_index DESC;
