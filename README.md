# TTB Label Verify

A proof-of-concept web tool for TTB label compliance review. An agent drops in
label photos (one or hundreds) plus the submittal-form CSV; one Claude vision
call per label transcribes it, and a deterministic rules engine compares the
seven required fields (brand, class/type, alcohol content, net contents,
producer, country of origin, government health warning). Every label becomes a
row in a worksheet — serial number, timestamp, per-field verdict marks, a
score, and a PASS / FAIL / REVIEW result — in a few seconds per label. Design
rationale and requirements traceability are in [APPROACH.md](APPROACH.md).

## Screenshots

The worksheet after a mixed scan — serials, timestamps, per-label time, the
seven field columns with verdict marks, scores, and PASS / FAIL / REVIEW
badges; flagged rows are tinted:

![Scan worksheet with pass, fail, and review rows](docs/screenshots/worksheet-mixed-scan.png)

The review drill-down on a failed row — the label photo, the submitted-vs-found
comparison for every field, and the per-clause diff of the health warning:

![Review drill-down showing the label photo, field comparison, and warning clause diff](docs/screenshots/review-drilldown.png)

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then set ANTHROPIC_API_KEY in .env
uvicorn app.main:app --port 8000
```

Open http://localhost:8000.

### Docker

```bash
docker build -t ttb-label-verify .
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=your-key ttb-label-verify
```

## How to use

There is one flow, whether you have one label or three hundred:

1. **Add the label photos.** Drag them into the drop zone or use the file
   picker — 1 to 300 photos per scan.
2. **Add the submittal form (CSV).** One row per photo, matched by file name —
   the `filename` column must match the photo's name exactly. Click
   "Download a blank submittal form (CSV)" to get a correct starting point
   with the header row and an example row.
3. **Click "Scan Labels".** Progress ticks as each sub-batch finishes and rows
   appear in the worksheet as they land.

Each worksheet row shows a serial number, the scan timestamp, the per-label
processing time, a thumbnail, the seven extracted field values each with a
✓ / ⚠ / ✗ / — mark, a score ("6/6 fields match"), and a PASS / FAIL / REVIEW
result. Flagged rows (FAIL and REVIEW) open a drill-down — click the row or
its Review button — showing the label photo large, the submitted-vs-found
comparison per field with a one-sentence explanation, and a per-clause diff
when the health warning text differs. Download the whole worksheet as CSV
when done.

**No submittal form?** The photos are still scanned and the extracted columns
filled in, but every row is flagged "No submittal data — needs review" —
there is nothing to check the labels against. The statutory health-warning
check still runs, so a wrong warning still fails.

Submittal-form CSV format (`filename` and `brand` required, the rest
optional):

```csv
filename,brand,class_type,abv,net_contents,producer,origin_country,is_import
bourbon-750.jpg,Copper Hollow,Kentucky Straight Bourbon Whiskey,45%,750 mL,"Copper Hollow Distilling Co., Bardstown, KY",,false
gin-import.jpg,Juniper Gate,London Dry Gin,47%,700 mL,"Juniper Gate Distillery, London",England,true
wine-750.jpg,Silverbrook Cellars,Red Wine,13.5%,750 mL,,,false
```

## Running tests

```bash
pip install -r requirements.txt
playwright install chromium      # once, for the browser E2E tests
pytest
```

The offline suite (335 tests, about 30 seconds) covers the rules engine, the
API with a mocked extractor, real-browser E2E of the worksheet flow against a
fake backend, and four adversarial QA gates. It never touches the network and
needs no API key.

Two paths need a real `ANTHROPIC_API_KEY`:

```bash
pytest -m live               # one live smoke call through the real pipeline
python eval/run_eval.py      # full 16-label eval against ground truth
```

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | (none) | Server-side only; required for real label extraction. Never sent to the browser. |
| `BATCH_CONCURRENCY` | `4` | How many labels are processed in parallel during a batch. |
| `EXTRACTION_MODEL` | `claude-sonnet-5` | Vision model used for extraction. `claude-haiku-4-5-20251001` measured faster on the eval set, with trade-offs documented in [APPROACH.md](APPROACH.md). |

## Repo layout

| Path | Contents |
|------|----------|
| `app/` | FastAPI app, extraction seam, rules engine, static UI |
| `eval/` | Synthetic 16-label test set, generator, manifest, eval harness |
| `tests/` | Offline test suite; `tests/qa/` is the independent adversarial QA suite |
| `docs/` | Build spec, test plan, screenshots |
