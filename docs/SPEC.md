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
  bounded parallelism, progress indicator, results table, CSV export.
- **R5 UX:** dead-simple. Large controls, drag-and-drop, plain language, no
  jargon, obvious buttons. Audience includes non-technical users age 50+.
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
