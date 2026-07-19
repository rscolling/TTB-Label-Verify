# QA Report — TTB Label Verify

**Role:** Independent QA agent  
**Scope:** Full repo review (`app/`, `tests/`, `eval/`, `docs/`, deploy config)  
**Evidence:** Docs + source review; offline suite re-run on this machine  
**Date:** 2026-07-19  

---

## 1. Summary

**What it is:** A PoC web tool for TTB alcohol-label compliance screening. Label photos go through one Claude vision call; submittal forms (CSV/Excel/PDF/photo) are normalized; a **deterministic Python rules engine** decides pass/fail/review on seven fields. No model-generated verdicts.

**Architecture quality:** Strong for a PoC. Clear seams (`Extractor`, `FormExtractor`), no persistence (R8), friendly error shapes, bounded batch concurrency, and a real multi-level test pyramid including an independent `tests/qa/` gate that build agents must not edit.

**Verification this run:**

| Check | Result |
|--------|--------|
| Offline `pytest` | **Green** (~42s; one expected `xfail` for QA5-F1) |
| Live Anthropic eval / deploy smoke | **Not re-run** (API-key / network gated) |
| Documented last live eval (APPROACH) | 16/16 labels, 112/112 field verdicts; mean **4.9s**, max **7.1s** |

**Overall QA posture:** **Ship-ready as a PoC demo**, with documented limitations and a few production-hardening gaps. Compliance judgment path is well tested; residual risk is mostly **perception drift**, **performance under real hosting**, **resource abuse**, and **regulatory completeness**.

---

## 2. What works well

| Area | Assessment |
|------|------------|
| Core design | AI = perception only; code = judgment. Auditable and unit-testable. |
| Rules engine (F1–F7) | Fuzzy brand/class, ABV/proof, net contents ±1%, origin import rules, statutory warning + clause diff, confidence downgrade (R6) |
| Form ingest (WP7) | Magic-byte routing, header aliases, preview-before-scan, no silent bad pairing when counts mismatch |
| Batch isolation | Cap 300 before spend; one bad file → error row; batch continues |
| UX / a11y intent | Single flow, plain language, marks not color-only, drill-down + focus/Escape, mobile cards |
| Security (XSS / CSV injection) | Dynamic text via `textContent`; export formula guards covered in QA |
| Traceability | Stakeholder → requirement → design → test matrix in APPROACH.md |
| Regression culture | Past bugs (ZeroDivision, leaky 500s, confidence on absent fields, worksheet staleness) fixed and pinned |

---

## 3. Failure points

Ranked by risk to **correctness, usability, or production safety**.

### 3.1 Known / accepted (documented)

| ID | Severity | Issue | Impact |
|----|----------|--------|--------|
| **K1** | **High (SLA)** | **R2 latency budget missed on free deploy** (APPROACH L6: ~5.2–6.7s/label; local eval max 7.1s, 5/16 over 5s) | Stakeholders said ~5s or agents won’t use it. Demo URL can fail the “feels fast” bar. |
| **K2** | **Medium** | **Bold warning is vision self-report only** | “Not bold” → REVIEW only; false confidence on typography compliance. |
| **K3** | **Medium** | **Net contents: metric + fl oz only** — no US malt standards (pint/quart / “1 PT. 6 FL. OZ.”) | Beer/malt rows may go REVIEW or wrong parse on common US labels. |
| **K4** | **Medium** | **Single photo ≠ full COLA label set** | Front-only scan can miss warning/net contents; mitigated by REVIEW for missing required elements, but still incomplete vs real submissions. |
| **K5** | **Low** | **QA5-F1:** `"` in photo name breaks order-matching (`xfail`) | Rare (`"` illegal on Windows); order-match notice can lie. |
| **K6** | **Low** | Full-width / exotic Unicode filenames don’t match ASCII manifest | Unmatched → “no application” error, not wrong pairing. |
| **K7** | **Low** | Bad manifest error **per UI chunk** (×10 sub-batches) | Noisy UX vs one API 400. |
| **K8** | **Low** | Direct empty-file batch → raw FastAPI 422 | UI blocks it; API consumers see different shape. |

### 3.2 Open residual risks (not fully closed)

| ID | Severity | Issue | Why it fails / can fail |
|----|----------|--------|-------------------------|
| **F1** | **High** | **No upload size / total payload cap** | QA notes 11MB PDF still accepted; 300 large images held in memory. DoS, OOM, and cost risk on public demo. |
| **F2** | **High** | **Unauthenticated public API** (by PoC design) | Anyone can burn Anthropic quota via `/api/verify*`. No rate limit beyond concurrency=4. |
| **F3** | **High** | **Vision / form-LLM is the accuracy bottleneck** | Synthetic 16-label set is strong but not real COLA photography; Haiku path already showed a **confident wrong** glare case (reason Sonnet is default). Real phone photos still under-sampled. |
| **F4** | **Medium** | **Required-elements check is client-only** (`app.js`) | API-only consumers skip TTB completeness REVIEW flags. Server verdicts only cover submittal consistency + F7. |
| **F5** | **Medium** | **Incomplete regulatory matrix** | Deferred: appellation, sub-7% wine/FDA, sulfites, FD&C Yellow No. 5, form-compliance triangle. Agents may over-trust PASS. |
| **F6** | **Medium** | **Class-family inference is keyword regex** | Mis-family → wrong ABV requiredness (e.g. edge class names). Mitigated by tests, still brittle. |
| **F7** | **Medium** | **Health check is shallow** | `/api/health` → `{"status":"ok"}` only; does not prove API key, model, or Anthropic reachability. |
| **F8** | **Medium** | **PDF/photo form transcription un-eval’d at scale** | Label eval is solid; form-LLM path has unit/API tests with fakes, little live ground-truth matrix. |
| **F9** | **Low** | **ABV ±0.05 is policy-hard** | “45%” vs “45.1%” fails; correct if policy is strict, noisy if agents expect rounding. |
| **F10** | **Low** | **Zero storage = no audit trail** | CSV export only; no `review_events` yet. Fine for PoC, weak for real agency use. |
| **F11** | **Low** | **CI runs offline suite only** | No scheduled live eval; perception regressions can land green in CI. |

### 3.3 Failure modes by layer (quick map)

```
Upload ──► bad image / huge file / rate abuse     [F1, F2]
   │
Vision ──► OCR noise, glare, multi-label miss     [F3, K2, K4]
   │
Form   ──► PDF/photo parse errors, alias gaps     [F8]
   │
Match  ──► order vs filename pairing edge cases   [K5, K6]
   │
Rules  ──► US units, fuzzy gray zone, family KW   [K3, F6, F9]
   │
UI     ──► client-only required elements          [F4]
   │
Host   ──► free tier latency / shared CPU         [K1]
```

---

## 4. Test evidence snapshot

| Level | Coverage | Notes |
|-------|----------|--------|
| L1 unit matchers | Strong | Traps 1–10 called out in plan; dedicated files per field |
| L2 API | Strong | Friendly errors, batch semantics, ingest abuse |
| L4 E2E | Strong | Playwright worksheet, drill-down, batch, form ingest |
| Adversarial QA | Strong (~40% of suite) | Isolated under `tests/qa/` |
| L5 eval | Documented pass | Not re-run here; depends on key + cost |
| L6 deploy | Documented partial fail on latency | Demo correct but often >5s |

**This machine:** offline suite **passed** with **1 expected xfail** (QA5-F1).  
**CI:** `.github/workflows/ci.yml` installs Chromium and runs offline `pytest` — appropriate for a PoC.

---

## 5. Recommendations

### P0 — Before treating as more than a demo

1. **Enforce hard upload limits** (per file and per request, e.g. 10–15 MB image, 20 MB form; reject with friendly 413 before buffering whole payload).  
2. **Protect the public demo:** auth (basic API key / IP allowlist), or Render/env-level rate limits + spend caps on Anthropic.  
3. **Fix or re-host for R2:** paid tier / closer region / measure p50–p95; surface “over budget” in UI when `processing_time_ms > 5000`.  

### P1 — Correctness & trust for agent use

4. **Move required-elements check server-side** (same rules as `app.js`) so API and UI cannot disagree; keep client only for presentation.  
5. **Expand live eval:** real phone photos (angle/glare/crop), multi-label products, beer US net contents; keep synthetic set as regression floor.  
6. **Add American standard net contents** parser (pint/quart/compound) in `net_contents.py`.  
7. **Form-LLM eval harness** (golden PDF/photo forms → expected rows) parallel to label eval.  

### P2 — Product / compliance growth

8. **Label-set model** (front/back/neck → one submission, best-of-field + cross-image contradiction).  
9. **Broader CFR matrix** (sulfites, Yellow 5, appellation, form completeness) with REVIEW-not-FAIL where photos legitimately omit fields.  
10. **Optional persistence path** sketched in APPROACH (scans + review_events) when leaving pure PoC.  
11. **Health deep-check** (optional authenticated) that validates key present + one lightweight model ping.  
12. **Ship QA5-F1 fix** when convenient (percent-encode `"` in client-written manifest names).  

### P3 — Engineering hygiene

13. **Scheduled live smoke** (weekly `pytest -m live` + sample of `run_eval.py`) outside PR CI.  
14. Pin dependency ranges or lockfile for deploy reproducibility.  
15. Document clearly in UI: *PASS = consistency screen, not final COLA approval.*

---

## 6. QA verdict

| Question | Answer |
|----------|--------|
| Does the PoC meet its stated architecture goals? | **Yes** |
| Is offline quality high enough to trust the rules engine? | **Yes** |
| Is it safe/robust as an open internet demo? | **Only with spend/size controls** |
| Does it fully replace human TTB review? | **No** — and it correctly routes hard cases to REVIEW |
| Gate for “demo + evaluation assignment”? | **PASS** with latency and scope limitations disclosed |
| Gate for production agency tool? | **FAIL until** P0–P1 items (limits, auth, server-side completeness, broader eval) |

---

## 7. Suggested next QA actions

1. Run `pytest -m live` + `python eval/run_eval.py --markdown` and attach latency histogram.  
2. Manual L6 checklist against the live Render URL (health, 1 label, batch of 5, bad file).  
3. Abuse pass: max concurrent clients, huge JPEG, 301 files, hostile form cells.  

---

## 8. Implementation follow-up (2026-07-19)

Actionable recommendations from §5 were implemented in-repo. Offline suite green after changes.

| Rec | Status | Where |
|-----|--------|--------|
| P0-1 Upload size caps | Done | `app/limits.py`, `_read_capped` in `app/main.py` |
| P0-2 API key + rate limit | Done (opt-in via env) | `app/security.py`, `ProtectMiddleware` |
| P0-3 Over-5s budget UI/export | Done | Time column + CSV `over_5s_budget` |
| P1-4 Server-side required elements | Done | `app/rules/required_elements.py`, API `required_elements` |
| P1-6 US net contents (pint/quart/compound) | Done | `app/rules/net_contents.py` |
| P1-7 Form eval harness | Done (scaffold) | `eval/run_form_eval.py`, `eval/forms/` |
| P2-8 Label-set merge foundation | Done (library only) | `app/rules/label_set.py` |
| P2-9 Broader CFR | Partial | Disclosure helpers + docs; not auto-applied |
| P2-10 Audit trail | Partial | CSV `reviewer_note` (still no server persistence / R8) |
| P2-11 Health deep-check | Done | `/api/health?deep=1` |
| P2-12 QA5-F1 quote filename | Fixed | `wireSafeFilename` in `app/static/app.js` |
| P3-13 Scheduled live smoke | Done | `.github/workflows/live-smoke.yml` |
| P3-14 Dependency pins | Done | `requirements.txt` upper bounds |
| P3-15 COLA disclaimer | Done | Footer in `index.html` |

**Still ops/human:** set `VERIFY_API_KEY` / rate limits on Render; paid tier for R2 latency; real-photo eval expansion; wire label-set into the HTTP UI.
