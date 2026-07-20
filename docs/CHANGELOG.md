# Changelog

Notable project changes, newest first.

---

## 2026-07-19 — Human review workflow + second-agent QA (Grok Build)

**Git:** `a94e60b`, `8f482ef`, `3ec0bb5` on `main`

**QA process:** a second, independent QA pass was run with **Grok Build
(xAI)** alongside the original QA agent ([QA-REPORT.md](QA-REPORT.md)).
Grok Build exercised the running UI as a reviewer would; its findings were
triaged by the maintainer and fixed in the commits above.

### Features

- **Worksheet filters**: All / Passed / Failed buttons above the results.
- **Human review workflow** in the flagged-row drill-down:
  - Approve / Deny the label (with undo); decision overrides the displayed
    status and recounts the summary banner, original scan verdict preserved.
  - Per-field **Confirm reading / Update reading** checkboxes with a
    corrected-reading input (audit evidence; never auto-changes the score).
  - **Feedback comment** for the applicant company on why the label failed.
- **CSV export**: `pass_fail` reflects the reviewer decision;
  `reviewer_note` records it; new `reviewer_comment` and `field_checks`
  columns. Review state is browser-only (R8) — the CSV is the record.

### Fixes (from the second-agent QA pass)

- Filter buttons wrapped onto two lines; now one row with spacing before
  the download button.
- Drill-down comparison table clipped its right-hand columns; Explanation
  folded into the Result cell so all columns fit without sideways scroll.
- Label photo now click-to-enlarges in a native `<dialog>` lightbox with a
  visible close button; photo stays sticky while the comparison scrolls.

### Docs

- README: review-workflow section; all screenshots recaptured for the
  current UI (main shots now use real eval label images).
- Error-UI capture script scenario 2 updated: count mismatches no longer
  block the scan (they become MISSING rows), so the blocked-scan example is
  now a form row with no brand name.

## 2026-07-19 — QA hardening pass

**Git:** `7b7106f` on `main`  
**Commit:** `feat: QA hardening — limits, auth, required elements, US net contents`  

**Commit scope:** implement actionable recommendations from [QA-REPORT.md](QA-REPORT.md) (summary, failure points, recommendations). Offline suite green after the change.

### Features

- **Upload size caps** (`app/limits.py`): per-image, per-form, and batch-total byte limits with friendly HTTP 413. Env: `MAX_IMAGE_BYTES`, `MAX_FORM_BYTES`, `MAX_BATCH_TOTAL_BYTES`.
- **Optional demo protection** (`app/security.py`): `VERIFY_API_KEY` (X-API-Key / Bearer) and `RATE_LIMIT_PER_MINUTE` on verify/ingest routes; health + UI stay open.
- **Server-side required-elements** (`app/rules/required_elements.py`): TTB-cited core completeness; API field `required_elements`; overall status upgrades match→review when elements are missing from the photo (never downgrades FAIL).
- **American standard net contents**: pint, quart, and compound statements (e.g. `1 PT. 6 FL. OZ.`) in `app/rules/net_contents.py`.
- **Over-5s budget signaling**: worksheet Time column + CSV `over_5s_budget`.
- **Deeper health**: `/api/health` reports `api_key_configured` / `auth_required`; `?deep=1` adds config checks.
- **Form eval scaffold**: `eval/run_form_eval.py` + `eval/forms/` golden CSV fixtures.
- **Label-set merge foundation**: `app/rules/label_set.py` (library only; HTTP still one row per photo).
- **CSV audit columns**: `required_missing`, blank `reviewer_note` (local audit trail under R8 no-server-storage).
- **Scheduled live smoke workflow**: `.github/workflows/live-smoke.yml` (needs `ANTHROPIC_API_KEY` secret).

### Fixes

- **QA5-F1**: order-matched photos with `"` in the filename write a wire-safe (`%22`) name into the serialized manifest so it matches multipart Content-Disposition encoding.

### Docs / UX

- Footer disclaimer: **PASS is a consistency screen, not a final COLA approval**.
- [QA-REPORT.md](QA-REPORT.md) §8 implementation status table.
- README config table and APPROACH.md limitations updated.

### Tests

- New: `tests/test_required_elements.py`, `tests/test_label_set.py`, `tests/test_limits_and_security.py`, net-contents US units, health deep, limits/auth.
- Updated contracts/E2E for additive API fields and CSV columns; QA5-F1 xfail removed.

### Not in this commit (ops / later)

- Render secrets and rate limits in production.
- Paid hosting for hard &lt;5s R2 budget.
- Wire label-set merge into the HTTP UI.
- Expanded real-photo / form-LLM live eval sets.
