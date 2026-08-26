# Going public — free hosting, start to finish

Everything below is free at the tier this project needs. No credit card is
required for any step.

| Piece | Service | Cost | What it gives you |
|---|---|---|---|
| Code | GitHub | Free | The public repository recruiters read |
| Web app | Streamlit Community Cloud | Free | A live URL: `yourname-nyc-ortho.streamlit.app` |
| Map dashboard | Tableau Public | Free | The ZCTA choropleth, embeddable |
| Cloud database | Neon *(optional)* | Free tier | A "live data" story instead of a CSV |

**The one paid trap to avoid:** a custom domain. `yourname.streamlit.app` is
free forever. A `.com` runs roughly $12/year — worth it only once everything
else works.

---

## Step 0 — Install Python (required, nothing runs without it)

This machine has only the Microsoft Store *stub* for Python, not Python itself.

```powershell
winget install --id Python.Python.3.12 -e
```

Then **close and reopen your terminal** and verify:

```powershell
python --version
```

If it still opens the Microsoft Store, the Store alias is shadowing the real
install. Go to **Settings → Apps → Advanced app settings → App execution
aliases** and switch **off** `python.exe` and `python3.exe`. Reopen the
terminal and check again.

Prefer a manual installer? <https://www.python.org/downloads/windows/> — take
the latest **Windows installer (64-bit)** for 3.12, and tick **"Add python.exe
to PATH"** on the first screen.

---

## Step 1 — Run the pipeline locally

```powershell
cd D:\Claude\nyc-healthcare-transparency-mvp
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` and set `DB_PASSWORD` to your PostgreSQL password. Then:

```powershell
createdb -U postgres nyc_healthcare_mvp
python run_pipeline.py all
```

`all` finishes by writing `data/nyc_ortho_scores_public.csv` — the snapshot the
web app reads.

Check it before publishing anything:

```powershell
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' -U postgres -d nyc_healthcare_mvp -f sql/05_qa_checks.sql
```

---

## Step 2 — Test the web app locally

```powershell
streamlit run app.py
```

Opens at <http://localhost:8501>. Get it looking right here — it is much faster
than debugging on the deployed instance.

---

## Step 3 — Push to GitHub

Set your git identity once (it is not configured on this machine):

```powershell
git config --global user.name "Owen Ferris"
git config --global user.email "owen.h.ferris@gmail.com"
```

Create an empty **public** repository at <https://github.com/new>. Do not let
GitHub add a README, `.gitignore`, or licence — the repo already has them.

The repository is already initialised, and a dry run has confirmed the ignore
rules keep `.env`, secrets, and raw data out. So just stage and check:

```powershell
git add .
git status
```

**Read that `git status` output before committing.** Confirm `.env` is *not*
listed and no multi-gigabyte CSV is staged. Then:

```powershell
git commit -m "NYC healthcare transparency pipeline: SPARCS + CMS value index"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

If `git push` asks for a password, GitHub no longer accepts account passwords
over HTTPS. Generate a personal access token at **Settings → Developer settings
→ Personal access tokens → Tokens (classic)** with the `repo` scope, and paste
that as the password.

### If you ever commit a secret by accident

Rotate the credential first — assume it is compromised the moment it is pushed.
Rewriting history does not un-publish it.

---

## Step 4 — Deploy the web app

1. Go to <https://share.streamlit.io> and sign in with GitHub.
2. **Create app → Deploy a public app from GitHub**.
3. Repository: your repo. Branch: `main`. Main file path: `app.py`.
4. **Deploy**.

First build takes two or three minutes while it installs the requirements. You
get a URL like `https://owen-nyc-ortho.streamlit.app`.

The app reads `data/nyc_ortho_scores_public.csv` from the repo, so it works
immediately with no database attached. To refresh the public numbers later:

```powershell
python run_pipeline.py publish
git add data/nyc_ortho_scores_public.csv
git commit -m "Update scored snapshot"
git push
```

Streamlit redeploys automatically on push.

---

## Step 5 — Tableau Public (the map)

Streamlit handles ranking and filtering; Tableau handles geography, because it
joins shapefiles natively and Streamlit does not.

1. Download **Tableau Public** (free): <https://public.tableau.com/app/discover>
2. Connect → Text File → `exports/nyc_ortho_scores_*.csv`
3. Add → Spatial file → the NYC ZCTA shapefile from
   <https://opendata.cityofnewyork.us> (search "ZIP Code Tabulation Areas")
4. Relate the shapefile's ZCTA field to `primary_zip3`
5. Double-click Geometry to draw the map. `value_index` → Colour,
   `patient_volume` → Size. Tooltip: facility name, cost, `oe_ratio_los`, stars.
6. **File → Save to Tableau Public As…** — this publishes it and gives you a URL.

Anything saved to Tableau Public is **publicly visible**. That is fine here —
every input is already public data — but never point it at a workbook holding
anything private.

Put the resulting URL in the README, and link to it from the Streamlit app.

---

## Step 6 (optional) — A live cloud database

Skip this unless you specifically want the "live data" talking point. The CSV
snapshot is genuinely the right engineering choice for a few dozen static rows,
and it cannot break.

If you do want it, **Neon** (<https://neon.tech>) has the most usable free tier:

1. Create a project. Copy the connection string.
2. Load only the *scored output*, not staging — the free tier is 0.5 GB and the
   raw SPARCS data is far larger:

   ```powershell
   & 'C:\Program Files\PostgreSQL\18\bin\pg_dump.exe' -U postgres -d nyc_healthcare_mvp `
       -t master_orthopedic_market -t market_benchmark --no-owner -f scored.sql
   & 'C:\Program Files\PostgreSQL\18\bin\psql.exe' "YOUR_NEON_CONNECTION_STRING" -f scored.sql
   ```

   You will also need to recreate `vw_facility_scores` and `vw_tableau_export`
   there — run `sql/04_export_views.sql` against Neon.
3. In Streamlit Cloud: **App settings → Secrets**, paste:

   ```toml
   DATABASE_URL = "postgresql+psycopg2://user:pass@host/db?sslmode=require"
   ```

The app prefers the database when reachable and falls back to the CSV when it
is not, so a sleeping free-tier instance degrades quietly instead of 500-ing.

---

## Step 7 — Make the repo readable

The README is what actually gets read. Add near the top, once you have the URLs:

```markdown
**[Live app](https://your-app.streamlit.app)** ·
**[Tableau dashboard](https://public.tableau.com/...)** ·
**[Methodology](docs/ERRATA.md)**
```

Add a screenshot of the app to the README — `![](docs/screenshot.png)`. A repo
with a picture gets read; one without mostly does not.

Add a licence: on GitHub, **Add file → Create new file**, name it `LICENSE`,
and GitHub offers a template picker. MIT is the conventional choice for
portfolio work.

---

## What "done" looks like

- [ ] Python installed, `python --version` works
- [ ] `sql/99_smoke_test.sql` prints `ALL SMOKE TESTS PASSED`
- [ ] `python run_pipeline.py all` completes; QA checks reviewed
- [ ] Crosswalk reviewed — check 6 in the QA script is empty or understood
- [ ] `streamlit run app.py` looks right locally
- [ ] Public GitHub repo, `.env` confirmed absent from it
- [ ] Streamlit app deployed and loading
- [ ] Tableau Public dashboard published
- [ ] README links both, with a screenshot
