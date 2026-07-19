# Error / problem UI states

Screenshots captured with a local fake backend (`capture_error_ui.py`).

| File | State |
|------|--------|
| `01-no-photos-error.png` | Pre-scan validation: Run with no photos — red **error callout** |
| `02-form-photo-count-mismatch.png` | Scan blocked: form rows ≠ photos, no filename column |
| `03-worksheet-mixed-pass-error-fail.png` | Worksheet: PASS + **ERROR** (extraction failed) + **FAIL** |
| `04-error-row-drilldown.png` | Drill-down on an ERROR row |
| `05-fail-row-drilldown.png` | Drill-down on a FAIL row (field comparison) |
| `06-no-submittal-review.png` | Photos only — every row **REVIEW** (no silent pass) |
| `07-all-rows-bad-file.png` | Bad image → per-row ERROR + summary banner |

Re-capture:

```powershell
cd C:\Users\colli\Local_Documents\ttb-label-verify
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe docs\screenshots\error-ui\capture_error_ui.py
```
