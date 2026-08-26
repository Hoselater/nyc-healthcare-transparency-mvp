"""NYC Orthopedic Value Index -- public web app.

Runs on Streamlit Community Cloud (free). Reads from a cloud PostgreSQL
database when one is configured, and falls back to a committed CSV snapshot
otherwise, so the public demo never breaks because a free-tier database went
to sleep.

Local:  streamlit run app.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
SNAPSHOT_CSV = PROJECT_ROOT / "data" / "nyc_ortho_scores_public.csv"

st.set_page_config(
    page_title="NYC Orthopedic Value Index",
    page_icon="🦴",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def _database_url() -> str | None:
    """Streamlit secrets first, then environment. Absent is fine."""
    try:
        if "DATABASE_URL" in st.secrets:
            return str(st.secrets["DATABASE_URL"])
    except Exception:  # noqa: BLE001 - no secrets.toml at all is normal
        pass
    return os.getenv("DATABASE_URL")


@st.cache_data(ttl=3600, show_spinner="Loading scored facilities...")
def load_scores() -> tuple[pd.DataFrame, str]:
    """Return (dataframe, source_label)."""
    url = _database_url()
    if url:
        try:
            from sqlalchemy import create_engine

            engine = create_engine(url, pool_pre_ping=True)
            frame = pd.read_sql("SELECT * FROM vw_tableau_export", engine)
            if not frame.empty:
                return frame, "live database"
        except Exception as exc:  # noqa: BLE001 - fall back rather than 500
            st.warning(
                f"Could not reach the database, showing the committed snapshot "
                f"instead. ({type(exc).__name__})"
            )

    if SNAPSHOT_CSV.exists():
        return pd.read_csv(SNAPSHOT_CSV), "CSV snapshot"

    return pd.DataFrame(), "no data"


def stars(rating) -> str:
    if pd.isna(rating):
        return "—"
    filled = int(rating)
    return "★" * filled + "☆" * (5 - filled)


def money(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"${value:,.0f}"


def num(value, spec: str = ".2f", suffix: str = "") -> str:
    """Format a possibly-missing number without raising.

    Worth the guard: a NULL reaching an f-string format spec raises, and on the
    deployed app that turns one missing value into a blank error page.
    """
    if value is None or pd.isna(value):
        return "—"
    return f"{value:{spec}}{suffix}"


def count(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{int(value):,}"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
scores, source = load_scores()

st.title("🦴 NYC Orthopedic Value Index")
st.caption(
    "Risk-adjusted value scoring for elective hip and knee replacement across "
    "the five boroughs — combining NY SPARCS clinical outcomes with CMS "
    "Hospital Price Transparency data."
)

if scores.empty:
    st.error(
        "No scored data available. Run the pipeline and publish a snapshot:\n\n"
        "```\npython run_pipeline.py all\npython run_pipeline.py publish\n```"
    )
    st.stop()


def col(name: str, default=None):
    """Tolerate schema drift rather than crashing the public demo."""
    return scores[name] if name in scores.columns else default


# --- Sidebar filters -------------------------------------------------------
st.sidebar.header("Filters")

boroughs = sorted(scores["hospital_county"].dropna().unique()) if "hospital_county" in scores else []
chosen_boroughs = st.sidebar.multiselect("Borough", boroughs, default=boroughs)

cost_series = col("facility_procedure_cost")
if cost_series is not None and cost_series.notna().any():
    cost_min = int(cost_series.min())
    cost_max = int(cost_series.max())
    max_cost = st.sidebar.slider(
        "Maximum procedure cost",
        min_value=cost_min,
        max_value=cost_max,
        value=cost_max,
        step=1000,
        format="$%d",
        help="Median negotiated rate for MS-DRG 470, or the discounted cash "
             "price where no negotiated rate is published.",
    )
else:
    max_cost = None

volume_series = col("patient_volume")
min_volume = st.sidebar.slider(
    "Minimum annual volume",
    min_value=0,
    max_value=int(volume_series.max()) if volume_series is not None else 100,
    value=0,
    step=10,
    help="Higher surgical volume correlates with better arthroplasty outcomes.",
)

min_stars = st.sidebar.select_slider(
    "Minimum quality rating", options=[1, 2, 3, 4, 5], value=1,
    format_func=lambda v: "★" * v,
)

st.sidebar.divider()
st.sidebar.caption(f"**Data source:** {source}")
if "last_discharge_year" in scores.columns:
    years = scores[["first_discharge_year", "last_discharge_year"]].dropna()
    if not years.empty:
        st.sidebar.caption(
            f"**Clinical years:** {int(years['first_discharge_year'].min())}–"
            f"{int(years['last_discharge_year'].max())}"
        )

# --- Apply filters ---------------------------------------------------------
view = scores.copy()
if chosen_boroughs and "hospital_county" in view:
    view = view[view["hospital_county"].isin(chosen_boroughs)]
if max_cost is not None and "facility_procedure_cost" in view:
    view = view[view["facility_procedure_cost"] <= max_cost]
if "patient_volume" in view:
    view = view[view["patient_volume"] >= min_volume]
if "star_rating" in view:
    view = view[view["star_rating"] >= min_stars]

if view.empty:
    st.warning("No facilities match these filters. Widen them in the sidebar.")
    st.stop()

# --- Headline numbers ------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Facilities", len(view))
c2.metric("Median cost", money(view["facility_procedure_cost"].median())
          if "facility_procedure_cost" in view else "—")
if "facility_procedure_cost" in view and view["facility_procedure_cost"].notna().sum() > 1:
    spread = view["facility_procedure_cost"].max() / view["facility_procedure_cost"].min()
    c3.metric("Price spread", f"{spread:.1f}×",
              help="Highest published price divided by the lowest, for the same procedure.")
else:
    c3.metric("Price spread", "—")
c4.metric("Total procedures", count(view["patient_volume"].sum())
          if "patient_volume" in view else "—")

st.divider()

# --- Cost vs quality -------------------------------------------------------
st.subheader("Cost against risk-adjusted quality")
st.caption(
    "Each bubble is a hospital; size is surgical volume. Lower and further left "
    "is better — cheaper, and discharging patients faster than their case mix "
    "predicts. The dashed line is the market median price."
)

plot_columns = {"facility_procedure_cost", "clinical_oe", "facility_name"}
if plot_columns.issubset(view.columns):
    plot_data = view.dropna(subset=["facility_procedure_cost", "clinical_oe"])
    if not plot_data.empty:
        figure = px.scatter(
            plot_data,
            x="clinical_oe",
            y="facility_procedure_cost",
            size="patient_volume",
            color="star_rating" if "star_rating" in plot_data else None,
            hover_name="facility_name",
            hover_data={
                "hospital_county": True,
                "patient_volume": ":,",
                "observed_avg_los": ":.2f",
                "expected_avg_los": ":.2f",
                "facility_procedure_cost": ":$,.0f",
                "value_index": ":.3f",
                "clinical_oe": False,
            },
            color_continuous_scale="RdYlGn",
            labels={
                "clinical_oe": "Observed / Expected  (lower is better)",
                "facility_procedure_cost": "Procedure cost",
                "star_rating": "Rating",
            },
            size_max=45,
        )
        if "market_median_cost" in plot_data:
            median_cost = plot_data["market_median_cost"].dropna()
            if not median_cost.empty:
                figure.add_hline(
                    y=float(median_cost.iloc[0]),
                    line_dash="dash",
                    line_color="gray",
                    annotation_text="market median",
                )
        figure.add_vline(x=1.0, line_dash="dash", line_color="gray",
                         annotation_text="as expected")
        figure.update_layout(height=520, margin=dict(t=20, b=20))
        st.plotly_chart(figure, use_container_width=True)
    else:
        st.info("No facilities have both a price and a clinical score under these filters.")

st.divider()

# --- Ranked table ----------------------------------------------------------
st.subheader("Ranked facilities")

table = view.sort_values("value_index", ascending=False).copy()
table["Rating"] = table["star_rating"].apply(stars) if "star_rating" in table else "—"

display_columns = {
    "facility_name": "Facility",
    "hospital_county": "Borough",
    "Rating": "Rating",
    "value_index": "Value Index",
    "facility_procedure_cost": "Cost",
    "pct_vs_market_median": "vs. market",
    "oe_ratio_los": "LOS O/E",
    "observed_adverse_pct": "Adverse disch. %",
    "patient_volume": "Volume",
}
present = {k: v for k, v in display_columns.items() if k in table.columns}

st.dataframe(
    table[list(present)].rename(columns=present),
    use_container_width=True,
    hide_index=True,
    column_config={
        "Cost": st.column_config.NumberColumn(format="$%d"),
        "vs. market": st.column_config.NumberColumn(format="%.1f%%"),
        "Value Index": st.column_config.NumberColumn(format="%.3f"),
        "LOS O/E": st.column_config.NumberColumn(format="%.3f"),
        "Adverse disch. %": st.column_config.NumberColumn(format="%.1f%%"),
        "Volume": st.column_config.NumberColumn(format="%d"),
    },
)

st.download_button(
    "Download this view as CSV",
    data=view.to_csv(index=False).encode("utf-8"),
    file_name="nyc_ortho_value_index.csv",
    mime="text/csv",
)

# --- Facility detail -------------------------------------------------------
st.divider()
st.subheader("Facility detail")

pick = st.selectbox("Choose a facility", table["facility_name"].tolist())
row = table[table["facility_name"] == pick].iloc[0]

d1, d2, d3 = st.columns(3)
with d1:
    st.markdown("**Quality**")
    st.metric("Rating", stars(row.get("star_rating")))
    st.metric("Observed LOS", num(row.get("observed_avg_los"), ".2f", " days"))
    st.metric("Expected LOS", num(row.get("expected_avg_los"), ".2f", " days"),
              help="Case-mix adjusted, by APR severity tier and discharge year.")
with d2:
    st.markdown("**Cost**")
    st.metric("Procedure cost", money(row.get("facility_procedure_cost")))
    st.metric("Market median", money(row.get("market_median_cost")))
    st.metric("Versus market", num(row.get("pct_vs_market_median"), "+.1f", "%"))
with d3:
    st.markdown("**Experience**")
    st.metric("Procedures", count(row.get("patient_volume")))
    st.metric("Hip / knee",
              f"{count(row.get('hip_volume'))} / {count(row.get('knee_volume'))}")
    st.metric("Value Index", num(row.get("value_index"), ".3f"))

if row.get("cost_basis") == "median_cash_price":
    st.info(
        "This facility publishes no payer-specific negotiated rate for MS-DRG 470, "
        "so its discounted cash price is used instead."
    )

# --- Methodology -----------------------------------------------------------
with st.expander("Methodology and limitations"):
    st.markdown(
        """
**Value Index**

```
              1              market median cost
Value =  ───────────  ×  ────────────────────────  ×  ln(1 + volume)
         clinical O/E     facility procedure cost
```

The clinical term uses **indirect standardization**: every discharge is compared
to the average for its own APR severity tier (1 Minor → 4 Extreme) and discharge
year, so a hospital treating sicker patients is not penalised for it. An O/E
below 1.0 means better than expected.

**What this is not**

* Length of stay and discharge disposition are *proxies*. The SPARCS public-use
  file carries no readmission, revision, or complication data.
* Published rates are not out-of-pocket cost. They ignore deductibles,
  coinsurance, and benefit design.
* Stays of 120+ days are censored to 120 in the source data, so observed LOS is
  a floor for facilities with long-stay outliers.
* MRF compliance is partial across hospitals, so market coverage is incomplete.
* The index weights are a reasoned judgement, not a validated model.

**Sources** — NY SPARCS De-Identified Inpatient Discharges (Health Data NY);
CMS Hospital Price Transparency machine-readable files (45 CFR Part 180).
        """
    )

st.caption(
    "Built as a healthcare informatics portfolio project. Not medical or "
    "financial advice — verify pricing directly with the hospital and your insurer."
)
