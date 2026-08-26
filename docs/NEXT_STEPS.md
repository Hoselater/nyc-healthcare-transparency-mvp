# Next steps: tokens, deployment, and stronger data

Everything here is either a short task for you, or a decision worth making
before spending money.

---

## 1. Socrata app token (2 minutes, free)

The dataset lives on **health.data.ny.gov** — that's the *New York State*
Department of Health, running on Socrata. An NYC Department of Health account
will not work; it's a different organisation on a different platform.

1. Go to <https://health.data.ny.gov>
2. **Sign In** (top right) → create an account if you don't have one there
3. Once signed in, go straight to
   <https://health.data.ny.gov/profile/edit/developer_settings>
   (or: click your avatar → *Developer Settings*)
4. Click **Create New App Token**
5. Fill in any name and description — e.g. "nyc-ortho-value-index", and your
   GitHub repo URL for the website field
6. Copy the **App Token**. That's the public identifier, and it's the one you
   want. Ignore the *Secret Token*.
7. Put it in `.env`:

```
SOCRATA_APP_TOKEN=your_token_here
```

Without it the API still works, but throttles aggressively and can start
returning 403 mid-pull.

---

## 2. Running the app in your own browser

The preview I used runs under the Claude harness; when that stops, the port
closes, which is why `localhost:8501` refused the connection. Run it yourself:

```powershell
cd D:\Claude\nyc-healthcare-transparency-mvp
venv\Scripts\activate
streamlit run app.py
```

It opens automatically at <http://localhost:8501>. Leave that terminal open —
closing it stops the server. `Ctrl+C` to stop.

The crosswalk review console is a second app:

```powershell
streamlit run review_crosswalk.py
```

---

## 3. Streamlit Community Cloud (free, ~10 minutes)

1. <https://share.streamlit.io> → sign in with GitHub
2. **Create app → Deploy a public app from GitHub**
3. Repository `Hoselater/nyc-healthcare-transparency-mvp`, branch **master**
   (not `main` — this repo uses master), main file `app.py`
4. **Deploy**

First build takes 2–3 minutes. You get
`https://<something>.streamlit.app`, which you can rename in app settings.

The app reads `data/nyc_ortho_scores_public.csv` from the repo, so it works with
no database attached. To refresh the published numbers:

```powershell
python run_pipeline.py publish
git add data/nyc_ortho_scores_public.csv
git commit -m "Update scored snapshot"
git push
```

Streamlit redeploys automatically on push.

**Do not** deploy `review_crosswalk.py` publicly — it writes to your database.

---

## 4. Tableau Public (free, ~1 hour)

Tableau does the geography; Streamlit does the ranking. Tableau joins shapefiles
natively and Streamlit does not, which is the whole reason for two surfaces.

1. **Export the data** — `python run_pipeline.py export` writes
   `exports/nyc_ortho_scores_<date>.csv`
2. **Open Tableau Public Desktop** → Connect → **Text File** → that CSV
3. **Get the shapefile** — <https://opendata.cityofnewyork.us>, search
   *"Modified Zip Code Tabulation Areas"* or *"ZIP Code Tabulation Areas"*.
   Download as **Shapefile**, and unzip it — Tableau needs the `.shp` and its
   sibling files together in one folder.
4. **Add it** — in the Data Source tab, click **Add** next to Connections →
   **Spatial file** → select the `.shp`
5. **Relate them** — drag the spatial file next to your CSV. Tableau will ask
   for a relationship. Your CSV has `primary_zip3` (3 digits); the shapefile has
   5-digit ZCTAs. Create a calculated field on the shapefile side:
   `LEFT([ZCTA], 3)` and relate that to `primary_zip3`.
6. **Build the map** — new worksheet, double-click **Geometry** to draw the
   boroughs. Then drag `value_index` to **Colour** and `patient_volume` to
   **Size**. Add `facility_name`, `facility_procedure_cost`, `oe_ratio_los` and
   `star_rating` to **Tooltip**.
7. **Publish** — File → **Save to Tableau Public As…**. This makes it public and
   gives you the URL for the README.

**Expect this to look approximate.** Three-digit ZIP is a patient-catchment
indicator, not a facility address, so the map shows where patients come from
rather than where hospitals sit. If you want precise hospital pins, the honest
route is geocoding facility addresses — say the word and I'll add a geocoding
step to the pipeline using a free service.

---

## 5. Figma (free, ~2 hours)

Figma is the one piece I cannot do for you — it's a GUI design tool with no
scriptable free tier. But the design decisions are already made; you're
translating, not inventing.

Three screens, in order of importance:

**Screen 1 — Search.** Procedure picker (knee / hip), insurance carrier
dropdown, ZIP code, and a "Find hospitals" button. The insurance dropdown should
be populated from real payer names, which the pipeline already has in
`stg_cms_mrf.payer_name`.

**Screen 2 — Results.** A ranked card list. Each card: hospital name, a
**5-star rating** (that's `star_rating` — do not show the raw Value Index),
an estimated cost, distance, and annual procedure volume as a trust signal.
Sort control: Best value / Lowest cost / Highest quality / Closest.

**Screen 3 — Facility detail.** The one screen where you can afford to explain
the method: observed vs expected length of stay, how the price compares to the
market median, volume, and a plain-English note that this is not medical advice.

The design principle worth writing in your portfolio: **the consumer never sees
an O/E ratio.** They see stars and a dollar amount. The sophistication is in the
pipeline, and hiding it is the product decision.

Start from a free community template — search Figma Community for "healthcare
dashboard" or "provider directory" — rather than an empty canvas.

---

## 6. Paid data sources worth considering

Only one of these is likely worth actual money for a portfolio project. I've
ordered them by value-per-dollar for *this* project.

| Source | Rough cost | What it fixes | Verdict |
|---|---|---|---|
| **Turquoise Health** | Free tier exists; paid tiers negotiable | Pre-parsed, normalised MRF data across all US hospitals. Solves Mount Sinai's 403, Montefiore's dead certificate, and every format quirk at once | **Best option.** They offer free access for research/academic use — worth an email before paying |
| **Serif Health** | Paid, enterprise-ish | Same category, strong on payer-rate normalisation | Good, but priced for companies |
| **CMS Limited Data Set (LDS)** | ~$3–5k + DUA | Real Medicare claims: readmissions, revisions, complications — the outcomes SPARCS lacks | Fixes the deepest weakness, but the Data Use Agreement and cost rule it out for a portfolio piece |
| **Definitive Healthcare / IQVIA** | $10k+ | Facility firmographics, volumes, affiliations | Overkill |
| **SPARCS full identified file** | Application + fee | Patient-level detail beyond the public file | Requires IRB-style approval |

**Free things that would strengthen the data more than any purchase:**

- **CMS Hospital Compare / Care Compare** — free download, and it has the
  complication and readmission rates for hip/knee replacement that SPARCS
  doesn't. This is the single highest-value addition available, and it's free.
  <https://data.cms.gov/provider-data/>
- **CMS Provider of Services file** — free, gives you the CCN ↔ facility
  mapping that would make the crosswalk mostly unnecessary.
- **NPPES NPI registry** — free, for facility identity resolution.
- **CMS Inpatient Prospective Payment System files** — free, national average
  reimbursement per DRG, a useful benchmark alongside negotiated rates.

**My recommendation:** before spending anything, add CMS Care Compare. It turns
"length of stay is a proxy for quality" into "here are actual complication and
readmission rates", which is the criticism most likely to be levelled at this
project. It's free and it's a well-documented flat file.

---

## 7. Known blockers needing you

| Blocker | Status | What would fix it |
|---|---|---|
| Mount Sinai (403) | Their CDN rejects every automated agent tested | Download the JSON manually from their price-transparency page, host it or point `manual_mrf_url` at a local copy |
| NYC Health + Hospitals (403) | IP-level bot blocking on the whole domain | Same — manual retrieval |
| Montefiore | Expired TLS certificate on their web server | Deferred at your request. Fixable by allowing unverified TLS for that one host, which I'd rather ask about first |
| Wyckoff Heights | No `cms-hpt.txt` published at all | Find the file via their price-transparency footer link |

For each, the pipeline already supports a `manual_mrf_url` column in
`data/target_hospitals.csv` — paste a URL there and the crawler uses it instead
of trying discovery.
