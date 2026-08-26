# Disclaimer

**This is a portfolio project. It is not medical advice, not financial advice,
and not a validated quality measure. Do not use it to choose a hospital.**

## What this project is

A demonstration of healthcare data engineering: it takes two public datasets —
New York's SPARCS de-identified inpatient discharges and hospitals' federally
mandated CMS price transparency files — and combines them into a risk-adjusted
"Value Index" for elective hip and knee replacement.

It exists to show that the data pipeline, the entity resolution, and the
risk-adjustment arithmetic can be built correctly. It does not exist to tell
anyone where to have surgery.

## What the Value Index is not

The index is a **reasoned heuristic, not a validated model**. Specifically:

- **It has never been calibrated against patient outcomes.** No one has checked
  whether a facility scoring 8.0 produces better results than one scoring 4.0.
  The three terms are multiplied together unweighted because that is a defensible
  starting point, not because any evidence says those are the right weights.
- **Length of stay is a proxy, not an outcome.** The SPARCS public-use file
  contains no readmission, revision, infection, or complication data. Length of
  stay and discharge disposition are the best available stand-ins, and both
  reflect a hospital's discharge-planning culture and post-acute network at
  least as much as its surgical quality.
- **Risk adjustment is coarse.** Expected values come from APR severity tier and
  discharge year alone. Real risk models use dozens of clinical covariates.
  A hospital serving a sicker or more socially complex population than its
  severity coding captures will score worse than it deserves.
- **Long stays are censored.** Any stay of 120 days or more is recorded
  identically in the source data, so observed length of stay is a floor.

## What the prices are not

**Published rates are not what you will pay.** They ignore your deductible,
coinsurance, out-of-pocket maximum, and benefit design, and they cover the
facility charge only — not the surgeon, anaesthesiology, implants billed
separately, or post-acute care.

Price transparency compliance is also incomplete and uneven. Facilities that
publish nothing are absent from the market median; facilities that publish under
a different coding system may be absent too. The market median is therefore
computed over the hospitals that chose to comply, which is not a random sample.

**Always confirm pricing directly with the hospital and your insurer.**

## Data sources and their licences

- **NY SPARCS De-Identified Inpatient Discharges** — New York State Department
  of Health, via Health Data NY. Published as a HIPAA-compliant public use file.
  Contains no protected health information.
- **CMS Hospital Price Transparency machine-readable files** — published by each
  hospital under 45 CFR Part 180. Retrieved through the `cms-hpt.txt` discovery
  protocol, which exists specifically to allow automated collection.

No private, licensed, or patient-identifiable data is used anywhere in this
project.

## On naming hospitals

This project names real facilities, because the entire premise of price
transparency is that naming them is the point, and because every input is
already public. That is not a claim that any named hospital is good or bad. Any
apparent ranking is the output of the heuristic described above, with all the
limitations listed above, computed from historical administrative data that was
never designed to measure surgical quality.

If you represent a facility and believe something here is wrong, please open an
issue — corrections are welcome.

## No warranty

This software is provided under the MIT License, without warranty of any kind.
See [LICENSE](LICENSE).
