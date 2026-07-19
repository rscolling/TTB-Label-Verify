# TTB Label Verification — Build Specification

Prototype for the Alcohol and Tobacco Tax and Trade Bureau (TTB): verify alcohol
beverage label images against application data using AI extraction + deterministic
rules verification.

Source of requirements: https://github.com/treasurytakehome-rgb/instructions
(stakeholder-interview format; each requirement below traces to a stakeholder statement).

## Core architecture decision

**AI for perception, code for judgment.** A single Claude vision call
(claude-sonnet-5, structured output) extracts fields from the label image.
A deterministic Python rules engine renders every verification verdict.
No verdict is ever produced by the LLM — a compliance agency needs auditable,
testable, explainable matching rules.

## Required fields (all 7 — extraction + verification)

| # | Field | Matching rule |
|---|-------|---------------|
| F1 | Brand name | Case-insensitive, whitespace-normalized, fuzzy (rapidfuzz token_sort_ratio ≥ 90 = match, 75–89 = warn, <75 = fail). "STONE'S THROW" ≡ "Stone's Throw" MUST pass. |
| F2 | Class/type designation | Case-insensitive + fuzzy, same thresholds as brand |
| F3 | Alcohol content | Parse `45% Alc./Vol.`, `45% ABV`, `90 Proof`, `45%` variants. Proof = 2 × ABV. Numeric equality with ±0.05 tolerance after conversion. |
| F4 | Net contents | Unit normalization (mL, cL, L, fl oz) → compare in mL, ±1% tolerance |
| F5 | Producer name + address | Case-insensitive fuzzy on name; address token-overlap (warn-level only — addresses vary in formatting) |
| F6 | Country of origin | Only when application marks import; case-insensitive exact after normalization; absent on domestic = N/A, not fail |
| F7 | Government Health Warning | EXACT text match (27 CFR 16.21) after whitespace/quote normalization; "GOVERNMENT WARNING:" prefix must be ALL CAPS on the label; bold = best-effort via vision model self-report (documented limitation). Report per-clause diff on mismatch. Title-case prefix MUST fail. |

Statutory warning text (verify against ttb.gov during build):
"GOVERNMENT WARNING: (1) According to the Surgeon General, women should not drink
alcoholic beverages during pregnancy because of the risk of birth defects.
(2) Consumption of alcoholic beverages impairs your ability to drive a car or
operate machinery, and may cause health problems."

## Hard requirements

- **R2 Performance:** < 5 seconds per label, end-to-end, with the elapsed time
  displayed in the UI on every result (single vision call, downscale images
  before upload, no chained calls).
- **R4 Batch:** multi-file upload (target 200–300), concurrent processing with
  bounded parallelism, progress indicator ("Scanned X of N…", rows appear as
  chunks land), worksheet results table, CSV export (serial, filename,
  scan_timestamp, processing_seconds, pass/fail, score + per-field
  verdict/reason columns; UTF-8 BOM, all cells quoted, formula-injection
  guard).
- **R5 UX (WP5 worksheet model):** dead-simple, ONE unified flow — no tabs.
  Large controls, drag-and-drop, plain language, no jargon, obvious buttons.
  Audience includes non-technical users age 50+.
  - Upload: a drag-and-drop zone accepting 1..N label photos (click-to-browse
    kept) plus a clearly-labeled second slot for the submittal form — in
    whatever format the applicant sent (WP7): CSV/TSV/TXT, Excel (.xlsx),
    PDF, or a photo of the form. Structured formats parse deterministically
    (case-insensitive header aliases); PDF/photo forms are transcribed by an
    LLM document-extraction call into the same batch-manifest rows (filename,
    brand, class_type, abv, net_contents, producer, origin_country,
    is_import) and previewed ("Show what was read") before any scan. Rows
    naming a photo file match by file name; a form without file names matches
    rows to photos by order when the counts are equal (persistent notice) and
    blocks the scan with an explanation when they differ. The big "Run"
    button sits inside the form step. There is NO typed application-details
    form: application data arrives only via the submittal form.
  - Worksheet: one row per scanned label with a zero-padded serial number
    (001, 002, … in scan order), a per-row scan date-time stamp (local, 24h,
    client-derived when that label's result lands), a per-row Time column
    (that label's processing time, "4.9s" — R2), a thumbnail + filename,
    the 7 extracted field values each with a compact status mark
    (✓ / ⚠ / ✗ / — — icon plus text/title, never color alone), a score
    ("6/7 fields match" — applicable fields that match), and a result:
    PASS (green, every applicable field matches), FAIL (red, any mismatch),
    REVIEW (amber, worst issue is review-level). FAIL and REVIEW rows are
    flagged for human review — row tint + flag icon.
  - No submittal form provided: photos are still scanned and extracted
    columns filled, but the submittal-checked columns and score read "No
    submittal data — needs review" and the row is flagged — never a silent
    pass. A single photo with no form must work (replaces the old
    single-label flow). The statutory health-warning check (F7) still renders
    a real verdict.
  - Required-elements check (WP7, deterministic, zero extra model calls,
    client-derived like score/pass-fail): per class family (malt / wine /
    spirits, inferred from the declared-else-extracted class/type), the
    label-mandatory elements — brand, class/type, net contents, producer,
    health warning; ABV for wine and spirits — that were NOT found in the
    photo flag the row at REVIEW level with a "may appear on another label or
    be embossed on the container" reason (TTB citations in app/static/app.js
    and APPROACH.md). Never downgrades a FAIL.
  - Review drill-down: clicking a flagged row or its Review button (a real
    <button>, keyboard-accessible) opens a detail panel: the label image
    large (client-side object URL — no server storage, preserves R8), the
    scan timestamp and per-label time ("Scanned 2026-07-18 19:42:07 · 4.9s"),
    and the field-by-field comparison (submittal value vs
    extracted value, per-field verdict + reason, warning clause diff prose).
    Focus moves to the panel on open; Escape closes it and returns focus to
    the row's button. Completion focuses the summary banner (aria-live).
  - 390px responsiveness: the worksheet collapses to stacked cards with
    data-label column announcements; no horizontal page scrolling.
- **R6 Robustness:** imperfect images (angle, glare, lighting) degrade
  gracefully — low-confidence extraction surfaces as ⚠️ needs-human-review,
  never a silent wrong verdict.
- **R8 Scope:** standalone PoC. No COLA integration. No persistent storage of
  uploaded data (in-memory only; results exist for the session). API key
  server-side only (env var), never in client code.
- Per-field output: ✅ match / ⚠️ review / ❌ mismatch + extracted value vs
  application value + confidence + processing time.

## Error handling (explicit evaluation criterion)

- Non-image / corrupt file → clear friendly message
- Image with no detectable label → "couldn't read this label" + guidance
- API failure/timeout → retry once, then per-label error state (batch continues)
- Oversized images → downscale server-side, never reject silently

## Stack

Python 3.11+, FastAPI, uvicorn, anthropic SDK (claude-sonnet-5 vision,
structured output via tool-use schema), rapidfuzz, Pillow (downscale), pytest,
httpx (API tests), vanilla HTML/CSS/JS single page (no framework). No database.

## Deliverables

1. Public GitHub repo: source, README (setup/run), APPROACH.md (requirements
   traceability: stakeholder quote → requirement → design decision; trade-offs;
   on-prem/Azure-tenant migration path for the firewall constraint; limitations)
2. Deployed URL (always-on host — cold-start hosting is disqualified by R2)
3. Synthetic test-label set (~12–15) with expected results as an eval harness,
   including trap cases: fuzzy-brand-case, title-case warning, wrong ABV,
   proof/ABV mismatch, angled/glare shots.

## Out of scope

COLA integration, auth/user accounts, persistent storage, mobile app,
fine-tuned models.
