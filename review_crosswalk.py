"""Crosswalk review console -- LOCAL ONLY, not part of the public app.

Maps each SPARCS facility to its CMS price-file counterpart. Automated matching
gets most of the way; this is for the residue, where a human eye is faster and
more reliable than any similarity threshold.

    streamlit run review_crosswalk.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from rapidfuzz import fuzz, process

from etl import crosswalk, db

st.set_page_config(page_title="Crosswalk Review", page_icon="🔗", layout="wide")

NO_MATCH = "— no CMS match —"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def fetch(sql: str) -> list[tuple]:
    raw = db.get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        raw.close()


@st.cache_data(ttl=60)
def load():
    facilities = fetch(
        """
        SELECT c.pfi_number, c.facility_name, c.patient_volume, c.hospital_county,
               x.cms_facility_name, x.match_method, x.match_score, x.reviewed
        FROM facility_clinical_metrics c
        LEFT JOIN facility_crosswalk x ON x.pfi_number = c.pfi_number
        ORDER BY c.patient_volume DESC
        """
    )
    cms = [
        r[0]
        for r in fetch(
            """
            SELECT facility_name, count(*) FROM stg_cms_mrf
            GROUP BY 1 ORDER BY 1
            """
        )
    ]
    return facilities, cms


def save(pfi: int, sparcs_name: str, cms_name: str | None) -> None:
    raw = db.get_engine().raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(
                """
                INSERT INTO facility_crosswalk
                    (pfi_number, sparcs_facility_name, cms_facility_name,
                     match_method, match_score, reviewed, updated_at)
                VALUES (%s, %s, %s, 'manual', 100, TRUE, now())
                ON CONFLICT (pfi_number) DO UPDATE SET
                    sparcs_facility_name = EXCLUDED.sparcs_facility_name,
                    cms_facility_name    = EXCLUDED.cms_facility_name,
                    match_method         = 'manual',
                    match_score          = 100,
                    reviewed             = TRUE,
                    updated_at           = now()
                """,
                (pfi, sparcs_name, cms_name),
            )
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.title("🔗 Facility Crosswalk Review")
st.caption(
    "Each NYC facility in SPARCS needs to be matched to the hospital that "
    "published its price file. Approving a row marks it reviewed, and automated "
    "rebuilds will never overwrite it."
)

try:
    facilities, cms_names = load()
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not read the database: {exc}")
    st.stop()

if not facilities:
    st.warning("No scored facilities yet. Run `python run_pipeline.py transform` first.")
    st.stop()

frame = pd.DataFrame(
    facilities,
    columns=["pfi", "sparcs_name", "volume", "borough", "cms_name",
             "method", "score", "reviewed"],
)

total = len(frame)
matched = int(frame["cms_name"].notna().sum())
approved = int(frame["reviewed"].fillna(False).sum())
covered_vol = int(frame.loc[frame["cms_name"].notna(), "volume"].sum())
all_vol = int(frame["volume"].sum())

c1, c2, c3, c4 = st.columns(4)
c1.metric("Facilities", total)
c2.metric("Matched", f"{matched}/{total}")
c3.metric("Human-approved", f"{approved}/{total}")
c4.metric("Volume covered", f"{covered_vol/all_vol:.0%}" if all_vol else "—",
          help="Share of NYC surgical volume with a price-file match. This "
               "matters more than the facility count -- one large hospital is "
               "worth more than several small ones.")

st.divider()

show = st.radio(
    "Show",
    ["Needs attention", "All facilities", "Approved only"],
    horizontal=True,
    help="'Needs attention' is anything unmatched or not yet human-approved.",
)

if show == "Needs attention":
    view = frame[frame["cms_name"].isna() | ~frame["reviewed"].fillna(False)]
elif show == "Approved only":
    view = frame[frame["reviewed"].fillna(False)]
else:
    view = frame

if view.empty:
    st.success("Nothing left to review.")
    st.stop()

st.caption(f"{len(view)} facility(s). Highest volume first — those matter most.")

# Candidates are ranked per facility, best first, so the right answer is
# usually already selected and approving is a single click.
norm_cms = {name: crosswalk.normalize_name(name) for name in cms_names}

for _, row in view.iterrows():
    pfi = int(row["pfi"])
    header = f"{row['sparcs_name']}  ·  {int(row['volume']):,} procedures"
    if row["reviewed"]:
        header = "✅ " + header
    elif pd.notna(row["cms_name"]):
        header = f"🟡 {header}  ·  suggested: {row['cms_name']}"
    else:
        header = "🔴 " + header

    with st.expander(header, expanded=not row["reviewed"] and pd.isna(row["cms_name"])):
        norm = crosswalk.normalize_name(row["sparcs_name"])
        ranked = process.extract(
            norm, list(norm_cms.values()), scorer=fuzz.token_set_ratio, limit=8
        )
        inverse = {v: k for k, v in norm_cms.items()}
        options = [NO_MATCH] + [inverse[c] for c, _s, _i in ranked if c in inverse]
        labels = {NO_MATCH: NO_MATCH}
        for cand, score, _ in ranked:
            if cand in inverse:
                labels[inverse[cand]] = f"{inverse[cand]}   ({score:.0f})"

        current = row["cms_name"] if pd.notna(row["cms_name"]) else NO_MATCH
        if current not in options:
            options.insert(1, current)
            labels[current] = f"{current}   (current)"

        left, right = st.columns([4, 1])
        with left:
            picked = st.selectbox(
                "CMS facility",
                options,
                index=options.index(current),
                format_func=lambda o: labels.get(o, o),
                key=f"pick_{pfi}",
                label_visibility="collapsed",
            )
            other = st.selectbox(
                "…or pick from the full list",
                [NO_MATCH] + sorted(cms_names),
                index=0,
                key=f"all_{pfi}",
            )
        with right:
            st.write("")
            if st.button("Approve", key=f"ok_{pfi}", type="primary", width="stretch"):
                choice = other if other != NO_MATCH else picked
                save(pfi, row["sparcs_name"], None if choice == NO_MATCH else choice)
                st.cache_data.clear()
                st.rerun()

st.divider()
st.info(
    "After approving, re-run scoring to pick the changes up:\n\n"
    "```\npython run_pipeline.py score\npython run_pipeline.py publish\n```"
)
