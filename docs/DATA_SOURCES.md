# Data sources: what each one measures, and what to tell a user

Four sources now feed the pipeline. They are not interchangeable, and the
differences between them are the most important thing to get right — most of
the ways this project could mislead someone come from quietly treating one kind
of number as if it were another.

---

## The four sources

| Source | What it measures | Coverage | Join key | Refresh |
|---|---|---|---|---|
| **SPARCS Inpatient Discharges** | Every discharge: length of stay, severity, disposition | 41/41 NYC facilities | PFI | Annual, ~1yr lag |
| **CMS Hospital Price Transparency (MRF)** | What a specific payer negotiated | ~18/41 | Facility name → crosswalk | Hospital-controlled, irregular |
| **SPARCS Cost Transparency** | Facility list charge and reported cost | 41/41 | PFI | Annual, ends 2021 |
| **CMS Care Compare** | Actual complication and readmission rates | 30/41 | CCN → crosswalk | Quarterly |

---

## Three different meanings of "cost"

This is the distinction most likely to mislead, so the pipeline keeps the three
strictly apart and labels every figure with its basis.

**Negotiated rate** (`mrf_negotiated`) — what a named insurer actually agreed to
pay. Closest to reality for an insured patient, and the basis the Value Index
prefers wherever it exists.

**List charge** (`sparcs_charge`) — the hospital's published price. Almost nobody
pays it: it is the starting point for negotiation and typically runs **two to
four times** the negotiated rate. Used only as a fallback, and never pooled with
negotiated rates in the same market median — doing so would make every
MRF-priced hospital look like a bargain purely because of which source its
number came from.

**Reported cost** — the hospital's own cost of delivering care, from the
Institutional Cost Report. Interesting for margin analysis; not what anyone pays.

### What to tell a user

> "$38,000" means *the median rate insurers negotiated with this hospital*, not
> your bill. Your share depends on your deductible, coinsurance and
> out-of-pocket maximum. This figure also covers the **facility** only — not the
> surgeon, anaesthesia, separately billed implants, or rehab afterwards.

Where the fallback is in use, say so plainly: *"This hospital doesn't publish
negotiated rates, so we're showing its list price — expect the real negotiated
figure to be considerably lower."*

---

## Two different meanings of "quality"

**Length-of-stay O/E** (SPARCS) — available for every facility, but a *proxy*.
It measures how quickly a hospital discharges patients relative to what their
case mix predicts. That reflects discharge-planning culture and post-acute
networks at least as much as surgical skill.

**Complication and readmission rates** (CMS Care Compare) — genuine outcomes,
risk-standardised by CMS, but missing for about a quarter of the market.

### The proxy has now been tested

Across the 27 NYC facilities with both, length-of-stay O/E correlates **0.44**
with the actual complication rate.

That number deserves to be published rather than buried. It says the proxy
carries real signal — hospitals that discharge faster than expected do tend to
have fewer complications — but that it explains only a modest share of the
variation. Staten Island University Hospital is the clearest counter-example: an
O/E of 1.89 looks alarming, yet its complication rate of 3.1% is *below* the NYC
average of 3.73%.

**This is why CMS outcomes are reported next to the Value Index rather than
folded into it.** Scoring 30 hospitals on real outcomes and 11 on a proxy would
produce one number that means different things for different rows — the exact
failure this project has been trying to avoid everywhere else.

### What to tell a user

> The star rating is built from how efficiently a hospital discharges patients,
> how much it charges, and how many of these operations it does. Where CMS
> publishes actual complication and readmission rates, we show those too — and
> you should weight them more heavily than our rating, because they measure the
> thing you actually care about.

---

## Hip and knee are not one procedure

The clinical side separates cleanly: APR-DRG 324 and 326 are distinct codes, so
volume, length of stay and disposition are all computed per procedure. The split
reveals genuine within-hospital variation — Mount Sinai West runs a 0.885 O/E on
knees against 0.985 on hips.

Pricing separates only partly, and for a reason worth explaining: **MS-DRG 470
is defined as "major hip OR knee joint replacement"** — one billing code for
both. A hospital that prices on MS-DRG has no hip/knee split to give. APR-coded
sources do separate them, so per-procedure pricing exists for some facilities
and not others.

---

## Presenting this to a consumer

The design principle is that **the consumer never sees an O/E ratio**. They see
stars and a dollar amount. Everything above is the machinery behind that, and it
belongs on a "how this works" page rather than the results screen.

What each screen should carry:

- **Results list** — stars, estimated cost, volume, distance. One line of
  provenance: *"Prices from published insurer rates"* or *"list prices"*.
- **Facility detail** — the CMS complication and readmission rates, prominently,
  with CMS's own "better/no different/worse than national" wording. Then the
  length-of-stay comparison, labelled as a proxy.
- **Methodology page** — the correlation figure, the three cost meanings, and
  the coverage table at the top of this document.

---

## Still missing, and what would fix it

| Gap | Impact | Fix |
|---|---|---|
| Ambulatory surgery / ASC data | Joint replacement is moving outpatient fast; the inpatient-only view will get less representative every year | SPARCS Ambulatory Surgery file — **requires a formal NYSDOH data request** |
| Outpatient pricing | HSS alone publishes 13 outpatient centres, all currently unparsed | Parse **CPT 27130 (hip) and 27447 (knee)** from the outpatient MRFs — implementable now, not yet built |
| Cost data ends 2021 | Fallback-priced facilities carry stale prices against 2022–2024 clinical data | Wider MRF coverage, or a newer SPARCS cost release |
| Mount Sinai, NYC H+H | Two large systems with no price data | Their CDNs block all automated agents; needs a manually downloaded file |
| Surgeon-level data | Volume is measured per hospital, but outcomes track the surgeon at least as strongly | No public NY source; would need Medicare claims |

**The single highest-value next addition is CPT-based outpatient pricing.** The
files are already being crawled and the codes are well defined; it is a parser
change, not a new data acquisition problem.
