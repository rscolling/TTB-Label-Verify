# Changelog

Notable project changes, newest first.

---

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
