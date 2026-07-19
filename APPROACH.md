# Approach

## Architecture

**AI for perception, code for judgment.** The LLM now perceives BOTH documents
in the comparison — a Claude vision call transcribes the seven required fields
off each label photo, and, when the submittal form arrives as a PDF or a photo
rather than a spreadsheet, a second document-extraction call transcribes the
form's application rows (`claude-sonnet-5` by default; the `EXTRACTION_MODEL`
knob and its measured trade-offs are under
[Technical choices](#technical-choices-for-this-scope) — structured output via
forced tool-use schemas in both cases). A deterministic, unit-tested Python
rules engine then renders every verdict. No verdict is ever produced by a
model: a compliance agency needs matching rules that are auditable, testable,
and explainable, and that behave identically on the same input every time.
Each LLM call is treated as an OCR-with-context sensor whose output is checked
by code — and the form transcription is previewed to the agent ("Show what was
read") before a single label is scanned.

```
browser (single page, vanilla JS)
   |  multipart POST /api/ingest-form          (WP7: the submittal form, any format)
   v
form ingestion (app/form_ingest.py)            dispatch by extension + magic bytes
   |-- .csv/.tsv/.txt/.xlsx --> deterministic parsers (header aliases)
   |-- .pdf/.png/.jpg       --> ClaudeFormExtractor: ONE document call,
   |                            forced tool-use structured output
   v
normalized rows + source_kind + warnings  -->  previewed, then serialized back
                                               to the canonical CSV manifest
   |  multipart POST /api/verify | /api/verify-batch
   v
FastAPI (app/main.py)
   |-- prepare_image()          Pillow: validate, downscale to <=1568 px
   |-- Extractor (protocol) --> ClaudeExtractor: ONE vision call,
   |   app/extraction.py        forced tool-use structured output
   v
rules engine (app/rules/)      deterministic matchers, F1-F7
   |   FieldResult: verdict + extracted + expected + reason + confidence
   v
JSON: per-field verdicts, overall status, elapsed time
```

### Format-agnostic submittal form (WP7)

The submittal form's input format is unknown — agents receive whatever the
applicant sent. `POST /api/ingest-form` (additive; the batch endpoint's
contract is untouched) detects the format by extension AND magic bytes (a PDF
renamed `.csv` still routes to the PDF path), parses spreadsheet formats
deterministically with case-insensitive header aliases ("Class Type",
"Alcohol Content", "Import"), and sends PDFs/photos through
`ClaudeFormExtractor` — the same engineering as the label extractor (lazy
client, forced tool use, one retry for transient failures, immediate failure
on permanent 4xx, `EXTRACTION_MODEL` knob) with a transcribe-only system
prompt: what the form says, never invented, null for absent. The client
serializes the previewed rows back to the canonical CSV `manifest` at scan
time, so `/api/verify-batch` — and every QA contract locked around it — is
byte-identical; plain CSVs skip the round-trip entirely and ship raw, keeping
the server's manifest parser the authority.

Matching is deterministic and never guesses: rows naming a photo file match
by normalized file name (existing server behavior); a form with no file names
matches rows to photos in selection order only when the counts are equal
(with a persistent "check the pairings" notice) and blocks the scan with an
explanation otherwise; in mixed forms the leftover rows pair by order only
when unambiguous, else the unmatched photos scan as flagged
no-submittal-data rows.

### Required-elements check (per class family, TTB-cited)

A second deterministic slice — zero extra model calls, client-derived like
score and pass/fail — checks each row for the elements TTB requires on every
label of its class family, sourced from TTB's mandatory-label-information
guidance: malt beverages 27 CFR part 7 subpart E (TTB G 2023-12), distilled
spirits 27 CFR 5.61/5.64/5.65/5.141 (TTB G 2021-2), wine 27 CFR 4.32–4.36
(TTB G 2019-8). The family is inferred from the declared (else extracted)
class/type by keyword; unknown classes get the generic core. All families
require brand, class/type, net contents, producer, and the health warning
(27 CFR part 16); wine and spirits additionally require alcohol content —
for malt beverages ABV is conditional (27 CFR 7.63(a)(3)) and stays
informational. Origin for imports (27 CFR 7.69, CBP rules) is already
enforced deterministically by the rules engine off the submittal's
`is_import` flag.

A required element not found in the photo flags the row at **REVIEW**, not
FAIL: net contents and the producer statement may be blown, embossed, or
molded into the container (7.70, 7.66–7.68) and the warning may sit on a
front, back, or side label, so a single photo legitimately may not show them
— the drill-down's "Required on every label" section says exactly that, with
one specially worded case (distilled spirits must show alcohol content in the
same field of vision as the brand and class, 27 CFR 5.61). A real mismatch
still FAILs; the flag composes with the worst-issue logic and never
downgrades. This prototype encodes the malt-beverage-compatible core plus the
three-family ABV variance; the finer per-class variances (appellation of
origin 4.23/4.27, sub-7%-ABV wines under FDA rules, ingredient disclosures
such as sulfites and FD&C Yellow No. 5 per 7.63(b)) remain the documented
growth path below.

## Requirements traceability

The assignment's requirements were embedded in stakeholder interviews. This
matrix traces each stakeholder statement to the derived requirement, the design
decision that answers it, and where that decision is tested.

| Stakeholder statement | Derived requirement | Design decision | Test evidence |
|---|---|---|---|
| Sarah Chen: "If we can't get results back in about 5 seconds, nobody's going to use it" (previous vendor pilot failed at 30-40 s) | < 5 s per label, end to end | One vision call per label, never chained; images downscaled to 1568 px before upload (`prepare_image`); the per-label elapsed time is surfaced three ways — the worksheet's Time column on every row, the drill-down header ("Scanned 2026-07-18 19:42:07 · 4.9s"), and the `processing_seconds` column of the CSV export | `tests/test_e2e_ui.py` (`test_row_shows_per_label_elapsed_time`; drill-down header stamp asserted), `tests/test_e2e_batch_ui.py` (Time cell on every row; `processing_seconds` numeric in the export), `tests/qa/test_qa3_e2e_batch.py` (independent QA re-check of both), `tests/test_extraction.py` (single call, downscale asserted), `eval/run_eval.py` (per-label latency reported against the 5 s budget) |
| Sarah Chen: accessibility — "something my mother could figure out"; team tech comfort ranges from recent grads to 50+ | Dead-simple UI, no jargon | One flow for one label or three hundred: drop the photos, add the submittal form (CSV, Excel, PDF, or a photo — WP7) in its own matching drop zone, press Run. Plain-language column names ("Kind of drink", "Amount in bottle"), icon + text verdict marks (✓ / ⚠ / ✗ / — with accessible text, never color alone), flagged rows carry a visible flag and open a review drill-down with the photo and a plain-English field-by-field comparison; focus moves to the summary banner on completion and into the panel on open (Escape closes and returns focus); the worksheet collapses to stacked cards on narrow screens | `tests/test_e2e_ui.py`, `tests/test_ui.py`, `tests/qa/test_qa2_e2e.py` (focus and a11y assertions), `tests/qa/test_qa3_e2e_batch.py`, `tests/qa/test_qa4_worksheet_probes.py` |
| Sarah Chen: ~150,000 label applications/year, 47 agents, 5-10 min manual review per label; batch upload needed for peak seasons | Batch verification, 200-300 labels | One endpoint; application data arrives as the submittal-form CSV matched by file name (mirrors the spreadsheet-per-queue workflow agents already use; shared form fields remain an API-level option); 300-file cap enforced before any API spend; bounded concurrency (`BATCH_CONCURRENCY`, default 4); per-label isolation — one bad file becomes an error row, the batch continues; UI submits in sub-batches of 10 so the progress bar reflects real completion; CSV export of results | `tests/test_batch_api.py`, `tests/qa/test_qa3_batch_semantics.py` (cap-before-spend, isolation, exactly-300 admitted), `tests/test_e2e_batch_ui.py`, `tests/qa/test_qa3_e2e_batch.py` |
| Marcus Williams: infrastructure on Azure (post-FedRAMP 2019 migration); "network restricts outbound traffic to many domains" | Must be deployable inside TTB's network | The Anthropic API is the app's only outbound dependency, isolated behind the `Extractor` protocol; containerized (see [On-prem path](#on-prem--firewall-path)); swapping the backend is a one-class change | `tests/conftest.py` — the entire offline suite runs the real app against a fake extractor with zero outbound traffic, which is the swap demonstrated |
| Marcus Williams: avoid sensitive data storage; this is a proof-of-concept | No persistence (R8) | Uploads processed in memory and discarded; no database, no files written, no image data echoed back in responses; API key lives server-side in an env var only | `tests/qa/test_qa3_batch_semantics.py` (`test_qa3_no_upload_bytes_persist_after_batch`, `test_qa3_response_contains_no_image_data`) |
| Marcus Williams: existing COLA system is .NET; integration NOT required | Standalone tool | No COLA coupling; the JSON API (`/api/verify`, `/api/verify-batch`) is the seam a future .NET integration would call | `tests/test_api.py` (stable response contract), `tests/qa/test_qa2_contract.py` |
| Dave Morrison: "STONE'S THROW" vs "Stone's Throw" are functionally identical; label review requires judgment beyond pattern matching | Tolerant text matching with a human-review lane | Brand and class/type use case-insensitive, whitespace-normalized fuzzy matching (rapidfuzz `token_sort_ratio`: >= 90 match, 75-89 review, < 75 mismatch). The middle band routes genuine judgment calls to the agent instead of forcing a binary verdict | `tests/test_text_match.py` (trap 1 named test), `tests/test_e2e_ui.py` (happy path uses exactly this brand pair) |
| Dave Morrison: the tool must accelerate the workflow without adding friction | Fewer clicks, explanations not codes | Single page, one Run button, per-field one-sentence explanations in the drill-down; the worksheet flags only the rows that need a human look (FAIL and REVIEW) and the score column says how many fields matched, so a clean batch is a column of green badges an agent can skim | `tests/test_ui.py`, `tests/test_e2e_batch_ui.py` |
| Jenny Park: government warning requires exact match — word-for-word, all caps, bold | F7 strictness (27 CFR 16.21) | Whitespace/smart-quote normalization, then exact text comparison against the statutory text; "GOVERNMENT WARNING:" prefix checked for all caps on the case-preserved transcription (title case fails); on mismatch, a per-clause diff with word-level differences ("expected 'may' -> found 'might'"); bold is a best-effort vision self-report — "not bold" downgrades to review, never a silent pass or a hard fail (documented limitation) | `tests/test_warning.py` (traps 2-4), `tests/qa/test_qa_warning.py` (unicode whitespace, lowercase prefix), `tests/test_e2e_ui.py` (clause diff renders as prose) |
| Jenny Park: handle imperfectly photographed labels (angles, lighting, glare) | Degrade to review, never a silent wrong verdict | The extractor flags uncertain fields; an uncertain reading of text that is present on the label has its match/mismatch verdict downgraded to ⚠️ needs review, while a confidently absent field keeps its decisive verdict (a missing-origin import still fails); the eval set includes angled and glare-degraded variants | `tests/test_engine.py` (trap 10), `eval/labels/15-bourbon-angled.png`, `eval/labels/16-bourbon-glare.png` with expected verdicts in `eval/manifest.json` |

## Technical choices for this scope

**FastAPI + vanilla JS, no frontend framework.** The UI is one static page with
two scripts. For a PoC judged on clarity and error handling, a build chain
(React, bundler, TypeScript) adds setup cost and review surface without adding
capability. Vanilla JS also keeps the XSS posture simple to verify: all dynamic
text is inserted with `textContent`, never `innerHTML`, so hostile filenames or
extracted label text are inert by construction.

**No orchestration framework.** The pipeline is one model call followed by pure
functions. LangChain-style abstractions would wrap a single `messages.create`
in indirection. The `anthropic` SDK is called directly; the retry policy — one
retry for transient failures (connection errors, 5xx, 429) and malformed tool
payloads, no retry for permanent 4xx — is a small hand-written loop.

**Deterministic rules, not LLM-as-judge.** Verdicts must be reproducible
(same label, same answer, every time), auditable (an agent can read
`app/rules/warning.py` and see exactly why title case fails), cheap (no second
model call per field), and testable (the matchers carry 442 offline tests). A
rule change is a reviewable code diff, not prompt drift. The rules engine and
its callers are pinned by the 442-test offline suite. The model does the one
thing code cannot: read a photograph.

**Extraction model choice.** `EXTRACTION_MODEL` (default `claude-sonnet-5`)
selects the vision model. Both candidates were measured on the 16-label eval
set:

| | `claude-sonnet-5` (default) | `claude-haiku-4-5-20251001` |
|---|---|---|
| Latency (mean per label) | 5.3 s as first measured; 4.9 s after slimming the output schema | 4.1 s |
| Structural reliability | all tool payloads well-formed | 3/16 labels returned intermittently malformed payloads (now caught and retried) |
| Degraded-image verdict integrity | correct on the glare-degraded label | one confidently wrong verdict on the glare-degraded label |

Sonnet stays the default because a confidently wrong verdict on a degraded
photo is exactly the failure mode R6 forbids; the env knob is the documented
speed option, with these trade-offs. The call sets no sampling parameters —
`claude-sonnet-5` rejects `temperature` as deprecated — so run-to-run
stability comes from the schema-forced output and the deterministic rules
engine, not from sampling settings.

**Production seams.** If this went to production the seams are already in
place: the `Extractor` protocol takes an Azure-tenant or local backend; the
batch endpoint's chunked processing would move behind a queue and worker pool;
the JSON API would take auth and become the COLA integration surface. The
rules engine would not change. The designed feature growth is in
[What this grows into](#what-this-grows-into).

**Persistence and the record of scans.** The tool deliberately stores nothing
server-side — Marcus's constraint. Uploads are processed in memory and
discarded, and the CSV export is the record of a scan: serial, filename,
timestamp, processing seconds, pass/fail, score, and per-field verdicts with
reasons. That is the right trade for a PoC, and it is also the model this
grows into — the export columns are already the flattened form of a small
relational schema, on Azure SQL inside TTB's FedRAMP tenant:

```
submissions(submission_id, applicant, class_family, received_at, declared fields)
images(image_id, submission_id, blob_ref, sha256)
scans(scan_id, submission_id, serial, model_id, scanned_at, latency_ms)
field_verdicts(scan_id, field, extracted, declared, verdict, score, reason)
review_events(scan_id, reviewer, action, decided_at, note)
```

`review_events` is the audit trail: every human decision on a flagged row
becomes a recorded event with a reviewer, an action, and a timestamp — which
is what turns a screening tool's output into a defensible record. The rules
engine and API do not change; the client-side CSV builder becomes a set of
inserts.

## On-prem / firewall path

Marcus's constraint — Azure infrastructure, outbound traffic restricted to many
domains — is answered by a seam, not a rewrite:

- The app is containerized (see `Dockerfile`) and self-contained: no CDN
  assets, no external calls except the extraction backend.
- `ClaudeExtractor` is the only code that leaves the network, and it sits
  behind the `Extractor` protocol (`app/extraction.py`). Deploying inside
  TTB's tenant means implementing that protocol against an Azure OpenAI
  vision deployment in the FedRAMP boundary, or a locally hosted vision
  model — one class, zero changes to the rules engine, API, or UI.
- The offline test suite already proves the swap: 442 tests run the full app
  against a substitute extractor with no outbound traffic at all.

## What this grows into

The assignment's own guidance was to prefer a working core over ambitious but
incomplete features, so the following is designed but deliberately deferred —
each is a bounded extension of seams that already exist, not a rewrite.

**Three-layer verification.** Today's engine checks *consistency* (printed vs
declared) and — since WP7 — a first cut of *completeness*: the per-family
required-elements table above (three families, TTB-cited, REVIEW severity).
The full check is still a triangle with a scored delta on each edge: federal
requirements for the product's class ↔ the intake-form declarations ↔ the
vision extraction. What remains deferred is the finer per-class requirements
table (wine appellation-of-origin rules 4.23/4.27, sub-7%-ABV wines under FDA
jurisdiction, standards of fill, ingredient disclosures such as sulfites and
FD&C Yellow No. 5 per 7.63(b)) and the third finding, *form compliance* (did
the application itself declare everything its class requires) — a table and
one more comparison pass over data the pipeline already carries.

**Many images per submission.** A real COLA application is a label *set* —
front, back, and neck labels for one product. The next step groups uploaded
images by submission, takes the best reading of each field across the set
(highest-confidence extraction wherever the field appears), and flags
cross-image contradictions — a front label saying 45% and a back label saying
40% is a finding in itself, distinct from either image disagreeing with the
application. The worksheet row becomes one row per submission with per-image
drill-down, and the `images`/`scans` split in the schema above is already
shaped for it.

**American-standard net contents.** The net-contents matcher normalizes
metric units (mL, cL, L, fl oz). Malt beverages are labeled in American
standard measure — pints, quarts, and compound statements like
"1 PT. 6 FL. OZ." — which the parser does not yet read. This is a bounded
parser extension in `app/rules/net_contents.py`: a unit table and compound
parsing, feeding the same ±1% comparison in mL that every other statement
already goes through.

## Testing and verification

442 tests pass offline in about 40 seconds (`pytest`), plus one key-gated live
test (`pytest -m live`). Levels:

| Level | What | Where |
|-------|------|-------|
| Unit | Every field matcher and parser: ABV/proof variants, unit conversions, warning normalization and caps check, fuzzy thresholds, N/A logic | `tests/test_alcohol.py`, `test_net_contents.py`, `test_warning.py`, `test_text_match.py`, `test_producer.py`, `test_origin.py`, `test_engine.py` |
| API | FastAPI endpoints via TestClient with a mocked extractor: happy paths, every error path, batch partial-failure semantics | `tests/test_api.py`, `tests/test_batch_api.py`, `tests/test_ui.py` |
| Browser E2E | Real headless Chromium (Playwright) against the real app with a fake backend: the worksheet flow with and without a submittal CSV, serials/timestamps/Time column, review drill-down and clause diff as prose, error recovery, progress and CSV export | `tests/test_e2e_ui.py`, `tests/test_e2e_batch_ui.py` |
| Adversarial QA | Independent suites in `tests/qa/` (never edited by the build side) | 5 QA gates, ~40% of the suite |
| Eval harness | 16 synthetic labels with ground-truth verdicts through the real pipeline | `eval/run_eval.py` (key-gated); its comparison logic is itself unit-tested offline in `tests/test_run_eval.py` |
| Live smoke | One real vision call through the full pipeline | `pytest -m live` |

The build was developed with independent adversarial QA passes after each work
package. Those passes found real bugs, which are fixed and pinned by regression
tests: a user-controlled 500 (application net contents of "0 mL" caused a
ZeroDivisionError), unvalidated model output crashing the engine instead of
surfacing as a friendly 502, FastAPI's default 500 body leaking instead of the
documented error shape, and stale batch results persisting when new photos were
selected. The worksheet redesign got the same treatment: an audit caught that
WP5 had dropped the per-label elapsed time (restored as the Time column, the
drill-down stamp, and `processing_seconds` in the export), and the QA4 pass
found two result-integrity gaps, both fixed and pinned — the submittal CSV is
now snapshotted at submit so swapping it mid-scan cannot affect a running
scan, and results scored against a previous spreadsheet clear when a new CSV
is chosen, the same rule as choosing new photos.
The QA suites also verify properties under attack rather than by
inspection: script-bearing filenames and label text render inert
(XSS-by-construction), CSV export guards against spreadsheet formula injection,
the 301-file cap fires before any extraction spend, and reported per-label
processing time excludes semaphore queue wait.

## Eval harness

`eval/` contains 16 synthetic labels covering four clean baselines and the
canonical trap cases: fuzzy brand casing, title-case warning prefix, a
one-word warning swap, cosmetic warning differences (smart quotes, line
breaks), proof-to-ABV conversion, wrong ABV, cL/mL unit equivalence, wrong net
contents, missing import origin, wrong class/type, and two photo-degraded
variants (angle, glare, derived with a fixed seed so regeneration is
verdict-stable). Labels are rendered programmatically (HTML/CSS through
headless Chromium) at 1600 px — an earlier 1000 px render put the statutory
warning's small print at ~11 px, low enough to cause random transcription
noise (finding 4 below) — so the trap text on each image is exact by
construction;
the generator is committed (`eval/generate_labels.py`) and
`eval/manifest.json` is the ground truth, declaring the application data and
the allowed per-field verdicts for every label.

Run it (requires `ANTHROPIC_API_KEY`; one vision call per label):

```bash
python eval/run_eval.py             # full set, pass/fail matrix + latency
python eval/run_eval.py --markdown  # same matrix as a markdown table
python eval/run_eval.py --label 09-proof-only   # one label
```

The harness runs the exact code path the API serves (ClaudeExtractor + rules
engine, in process), prints a per-label x per-field matrix, and exits non-zero
if any verdict falls outside its allowed set.

### Live eval results

Final keyed run, `python eval/run_eval.py --markdown`, `claude-sonnet-5`,
2026-07-18:

| label | brand | class_type | abv | net_contents | producer | origin_country | warning | time |
|---|---|---|---|---|---|---|---|---|
| 01-bourbon-clean | match | match | match | match | match | na | match | 5.9s |
| 02-wine-clean | match | match | match | match | match | na | match | 4.6s |
| 03-beer-clean | match | match | match | match | match | na | match | 4.4s |
| 04-gin-import-clean | match | match | match | match | match | match | match | 5.1s |
| 05-brand-case-fuzzy | match | match | match | match | match | na | match | 4.5s |
| 06-warning-titlecase | match | match | match | match | match | na | mismatch | 4.9s |
| 07-warning-word-swap | match | match | match | match | match | na | mismatch | 5.1s |
| 08-warning-cosmetic | match | match | match | match | match | na | match | 4.7s |
| 09-proof-only | match | match | match | match | match | na | match | 4.6s |
| 10-abv-wrong | match | match | mismatch | match | match | na | match | 4.4s |
| 11-netcontents-cl | match | match | match | match | match | match | match | 4.4s |
| 12-netcontents-wrong | match | match | match | mismatch | match | na | match | 5.6s |
| 13-import-missing-origin | match | match | match | match | match | mismatch | match | 4.4s |
| 14-classtype-wrong | match | mismatch | match | match | match | na | match | 7.1s |
| 15-bourbon-angled | match | match | match | match | match | na | match | 4.7s |
| 16-bourbon-glare | match | match | match | match | match | na | match | 4.5s |

Labels passing: 16/16. Field verdicts as expected: 112/112. Latency
(extract + verify) mean 4.9 s, max 7.1 s against the 5 s budget — 5 of the 16
labels individually exceeded 5 s. The harness exits non-zero if any verdict
falls outside its allowed set; the two most recent consecutive full-set runs
were both clean (16/16 labels, 112/112 verdicts, exit 0).

One note on R6: the photo-degraded variants are synthetic 1600 px renders,
while real phone photos arrive at 3-4K resolution, so the synthetic set is the
conservative case for small print.

### Eval-driven iteration

The live eval was not a one-shot scorecard; it surfaced five real defects,
each fixed and pinned by a regression test:

1. **Producer boilerplate.** A compliant beer label failed the producer match
   because the label prints "BREWED AND CANNED BY X" where the application says
   "X"; the matcher now strips bottler-statement boilerplate (bottled /
   distilled / produced / brewed / canned / packed / packaged by) before
   comparing.
2. **Brand tagline fold-in.** A label rendering the application's brand inside
   a longer tagline hard-failed the brand match; exact containment with extra
   words is a judgment call, so containment now routes to ⚠️ review, never a
   hard mismatch.
3. **Absent-field confidence bug.** The import-missing-origin label flipped
   between review and mismatch across runs because the low-confidence
   downgrade also fired on confidently absent fields; the downgrade now
   applies only to uncertain readings of text that is present, so confident
   absence keeps its decisive verdict.
4. **OCR noise at 1000 px.** Compliant labels randomly failed the warning
   check — a different label each run — because the 1000 px render put the
   statutory warning at ~11 px; the set is re-rendered at 1600 px, after which
   two consecutive full runs were clean.
5. **Retry masking a permanent 400.** A deprecated `temperature` parameter
   caused a 400 that the retry loop swallowed into a generic failure message;
   a permanent 4xx can never succeed on retry, so it now fails immediately
   with the API's own message, and only connection errors, 5xx, 429, and
   malformed tool payloads get the single retry.

## Assumptions and limitations

- **Bold detection is best-effort.** Whether the warning prefix is printed in
  bold comes from the vision model's self-report; a "not bold" report yields
  ⚠️ review, never a hard fail. Reliable bold detection needs typographic
  analysis out of scope for this PoC.
- **ABV tolerance is strict (±0.05).** "45%" vs "45.1%" is a mismatch. The
  tolerance is one constant (`app/rules/alcohol.py`) if policy differs.
- **Zero-file batch via direct API** returns FastAPI's standard 422 validation
  shape rather than the friendly error format; the UI prevents the case and
  shows its own message.
- **Manifest matching is by normalized file name** (case-insensitive
  basename). Visually identical unicode variants (for example full-width
  digits in a filename) do not match a plain-ASCII manifest row; the file gets
  a per-label "no application" error entry rather than a silent wrong pairing.
- **A double-quote in an order-matched photo name breaks the pairing**
  (QA5-F1, LOW; `tests/qa/test_qa5_e2e_ingest.py`, `xfail`). When the submittal
  form names no files, the client pairs rows to photos by selection order and
  writes each photo's raw name into the serialized manifest. The browser
  percent-encodes `"` in the multipart filename (`a"b.png` -> `a%22b.png`), so
  the file arrives under a name the manifest key no longer matches and comes
  back as a "no row for this photo" error — contradicting the persistent
  "Matched N rows by order" notice. Comma, `%`, and space all round-trip
  cleanly; only `"` (the multipart filename-escaping char) triggers it, and `"`
  is illegal in filenames on Windows and most filesystems, so the case is rare.
  Fix when it matters: percent-encode `"` in the filename the client writes to
  the manifest at [app.js order-matching](app/static/app.js) so the manifest
  key matches the wire.
- **A structurally bad CSV surfaces per chunk in the UI.** The web UI submits
  in sub-batches of 10 and re-sends the manifest with each; a manifest-level
  error therefore appears as error rows for each sub-batch rather than one
  banner. The direct API rejects it once, before any spend.
- **Anthropic rate limits bound batch throughput.** 300 labels is 300 vision
  calls; `BATCH_CONCURRENCY` (default 4) is the throttle. A production system
  would add queueing and backoff beyond the built-in single retry for
  transient failures.
- **A direct 300-file API call is memory-bound** (all uploads are read into
  memory for the request). The UI's 10-file chunking keeps real usage small;
  a production system would stream to bounded temp storage — deliberately not
  done here to honor the no-storage constraint.

## Tools

The prototype was built AI-assisted — Claude Code driving separate build, QA,
and asset agents under human direction — which is the working method the role
itself calls for; all requirements analysis, architecture decisions, and the
adversarial QA gates above were part of that directed process.
