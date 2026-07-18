# Approach

## Architecture

**AI for perception, code for judgment.** One Claude vision call
(`claude-sonnet-5`, structured output via a forced tool-use schema) transcribes
the seven required fields off the label image, with per-field confidence. A
deterministic, unit-tested Python rules engine then renders every verdict. No
verdict is ever produced by the model: a compliance agency needs matching rules
that are auditable, testable, and explainable, and that behave identically on
the same input every time. The LLM is treated as an OCR-with-context sensor
whose output is checked by code.

```
browser (single page, vanilla JS)
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

## Requirements traceability

The assignment's requirements were embedded in stakeholder interviews. This
matrix traces each stakeholder statement to the derived requirement, the design
decision that answers it, and where that decision is tested.

| Stakeholder statement | Derived requirement | Design decision | Test evidence |
|---|---|---|---|
| Sarah Chen: "If we can't get results back in about 5 seconds, nobody's going to use it" (previous vendor pilot failed at 30-40 s) | < 5 s per label, end to end | One vision call per label, never chained; images downscaled to 1568 px before upload (`prepare_image`); elapsed time displayed on every single and batch result | `tests/test_extraction.py` (single call, downscale asserted), `tests/test_e2e_ui.py` (timer rendered in browser), `eval/run_eval.py` (per-label latency reported against the 5 s budget) |
| Sarah Chen: accessibility — "something my mother could figure out"; team tech comfort ranges from recent grads to 50+ | Dead-simple UI, no jargon | Two numbered steps, drag-and-drop with a large fallback button, plain-language field hints with examples ("45% or 90 proof"), icon + text verdicts (✅ Matches / ⚠️ Needs review / ❌ Doesn't match — never icon alone), results tables collapse to stacked cards on narrow screens, focus moves to the result banner or error callout so screen readers announce outcomes | `tests/test_e2e_ui.py`, `tests/test_ui.py`, `tests/qa/test_qa2_e2e.py` (focus and a11y assertions), `tests/qa/test_qa3_e2e_batch.py` |
| Sarah Chen: ~150,000 label applications/year, 47 agents, 5-10 min manual review per label; batch upload needed for peak seasons | Batch verification, 200-300 labels | One endpoint, two application-data modes: CSV manifest matched by file name (mirrors the spreadsheet-per-queue workflow agents already use) or shared form fields; 300-file cap enforced before any API spend; bounded concurrency (`BATCH_CONCURRENCY`, default 4); per-label isolation — one bad file becomes an error row, the batch continues; UI submits in sub-batches of 10 so the progress bar reflects real completion; CSV export of results | `tests/test_batch_api.py`, `tests/qa/test_qa3_batch_semantics.py` (cap-before-spend, isolation, exactly-300 admitted), `tests/test_e2e_batch_ui.py`, `tests/qa/test_qa3_e2e_batch.py` |
| Marcus Williams: infrastructure on Azure (post-FedRAMP 2019 migration); "network restricts outbound traffic to many domains" | Must be deployable inside TTB's network | The Anthropic API is the app's only outbound dependency, isolated behind the `Extractor` protocol; containerized (see [On-prem path](#on-prem--firewall-path)); swapping the backend is a one-class change | `tests/conftest.py` — the entire offline suite runs the real app against a fake extractor with zero outbound traffic, which is the swap demonstrated |
| Marcus Williams: avoid sensitive data storage; this is a proof-of-concept | No persistence (R8) | Uploads processed in memory and discarded; no database, no files written, no image data echoed back in responses; API key lives server-side in an env var only | `tests/qa/test_qa3_batch_semantics.py` (`test_qa3_no_upload_bytes_persist_after_batch`, `test_qa3_response_contains_no_image_data`) |
| Marcus Williams: existing COLA system is .NET; integration NOT required | Standalone tool | No COLA coupling; the JSON API (`/api/verify`, `/api/verify-batch`) is the seam a future .NET integration would call | `tests/test_api.py` (stable response contract), `tests/qa/test_qa2_contract.py` |
| Dave Morrison: "STONE'S THROW" vs "Stone's Throw" are functionally identical; label review requires judgment beyond pattern matching | Tolerant text matching with a human-review lane | Brand and class/type use case-insensitive, whitespace-normalized fuzzy matching (rapidfuzz `token_sort_ratio`: >= 90 match, 75-89 review, < 75 mismatch). The middle band routes genuine judgment calls to the agent instead of forcing a binary verdict | `tests/test_text_match.py` (trap 1 named test), `tests/test_e2e_ui.py` (happy path uses exactly this brand pair) |
| Dave Morrison: the tool must accelerate the workflow without adding friction | Fewer clicks, explanations not codes | Single page, one submit button, per-field one-sentence explanations; batch "What to look at" column names only the fields that need attention | `tests/test_ui.py`, `tests/test_e2e_batch_ui.py` |
| Jenny Park: government warning requires exact match — word-for-word, all caps, bold | F7 strictness (27 CFR 16.21) | Whitespace/smart-quote normalization, then exact text comparison against the statutory text; "GOVERNMENT WARNING:" prefix checked for all caps on the case-preserved transcription (title case fails); on mismatch, a per-clause diff with word-level differences ("expected 'may' -> found 'might'"); bold is a best-effort vision self-report — "not bold" downgrades to review, never a silent pass or a hard fail (documented limitation) | `tests/test_warning.py` (traps 2-4), `tests/qa/test_qa_warning.py` (unicode whitespace, lowercase prefix), `tests/test_e2e_ui.py` (clause diff renders as prose) |
| Jenny Park: handle imperfectly photographed labels (angles, lighting, glare) | Degrade to review, never a silent wrong verdict | The extractor reports per-field confidence; any field below 0.6 has its match/mismatch verdict downgraded to ⚠️ needs review; the eval set includes angled and glare-degraded variants | `tests/test_engine.py` (trap 10), `eval/labels/15-bourbon-angled.png`, `eval/labels/16-bourbon-glare.png` with expected verdicts in `eval/manifest.json` |

## Technical choices for this scope

**FastAPI + vanilla JS, no frontend framework.** The UI is one static page with
two scripts. For a PoC judged on clarity and error handling, a build chain
(React, bundler, TypeScript) adds setup cost and review surface without adding
capability. Vanilla JS also keeps the XSS posture simple to verify: all dynamic
text is inserted with `textContent`, never `innerHTML`, so hostile filenames or
extracted label text are inert by construction.

**No orchestration framework.** The pipeline is one model call followed by pure
functions. LangChain-style abstractions would wrap a single `messages.create`
in indirection. The `anthropic` SDK is called directly; retry-once is four
lines of code.

**Deterministic rules, not LLM-as-judge.** Verdicts must be reproducible
(same label, same answer, every time), auditable (an agent can read
`app/rules/warning.py` and see exactly why title case fails), cheap (no second
model call per field), and testable (the matchers carry 296 offline tests). A
rule change is a reviewable code diff, not prompt drift. The rules engine and
its callers are pinned by the 296-test offline suite. The model does the one
thing code cannot: read a photograph.

**Growth path.** If this went to production the seams are already in place:
the `Extractor` protocol takes an Azure-tenant or local backend; the batch
endpoint's chunked processing would move behind a queue and worker pool; results
would gain a persistent, append-only audit log (a deliberate PoC exclusion per
the no-storage constraint); the JSON API would take auth and become the COLA
integration surface. The rules engine would not change.

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
- The offline test suite already proves the swap: 296 tests run the full app
  against a substitute extractor with no outbound traffic at all.

## Testing and verification

296 tests pass offline in about 20 seconds (`pytest`), plus one key-gated live
test. Levels:

| Level | What | Where |
|-------|------|-------|
| Unit | Every field matcher and parser: ABV/proof variants, unit conversions, warning normalization and caps check, fuzzy thresholds, N/A logic | `tests/test_alcohol.py`, `test_net_contents.py`, `test_warning.py`, `test_text_match.py`, `test_producer.py`, `test_origin.py`, `test_engine.py` |
| API | FastAPI endpoints via TestClient with a mocked extractor: happy paths, every error path, batch partial-failure semantics | `tests/test_api.py`, `tests/test_batch_api.py`, `tests/test_ui.py` |
| Browser E2E | Real headless Chromium (Playwright) against the real app with a fake backend: form flows, verdict rendering, clause diff as prose, error recovery, batch progress and CSV download | `tests/test_e2e_ui.py`, `tests/test_e2e_batch_ui.py` |
| Adversarial QA | Independent suites in `tests/qa/` (never edited by the build side) | 3 QA gates, ~40% of the suite |
| Eval harness | 16 synthetic labels with ground-truth verdicts through the real pipeline | `eval/run_eval.py` (key-gated); its comparison logic is itself unit-tested offline in `tests/test_run_eval.py` |
| Live smoke | One real vision call through the full pipeline | `pytest -m live` |

The build was developed with independent adversarial QA passes after each work
package. Those passes found real bugs, which are fixed and pinned by regression
tests: a user-controlled 500 (application net contents of "0 mL" caused a
ZeroDivisionError), unvalidated model output crashing the engine instead of
surfacing as a friendly 502, FastAPI's default 500 body leaking instead of the
documented error shape, and stale batch results persisting when new photos were
selected. The QA suites also verify properties under attack rather than by
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
headless Chromium) so the trap text on each image is exact by construction;
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

Pending: this section is populated from `python eval/run_eval.py --markdown`
after a keyed run. No API key was present in the build environment, so the
matrix here would have been fabricated — it is omitted instead.

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
- **A structurally bad CSV surfaces per chunk in the UI.** The web UI submits
  in sub-batches of 10 and re-sends the manifest with each; a manifest-level
  error therefore appears as error rows for each sub-batch rather than one
  banner. The direct API rejects it once, before any spend.
- **Anthropic rate limits bound batch throughput.** 300 labels is 300 vision
  calls; `BATCH_CONCURRENCY` (default 4) is the throttle. A production system
  would add queueing and backoff beyond the built-in single retry.
- **A direct 300-file API call is memory-bound** (all uploads are read into
  memory for the request). The UI's 10-file chunking keeps real usage small;
  a production system would stream to bounded temp storage — deliberately not
  done here to honor the no-storage constraint.

## Tools

The prototype was built AI-assisted — Claude Code driving separate build, QA,
and asset agents under human direction — which is the working method the role
itself calls for; all requirements analysis, architecture decisions, and the
adversarial QA gates above were part of that directed process.
