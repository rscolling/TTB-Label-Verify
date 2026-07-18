# Test Plan — Tests at Every Level

Every work package ends with a QA gate. The build agent must leave ALL levels
that apply to its package green before handing off; the QA agent independently
re-runs everything, adds adversarial cases, and files findings. A work package
is DONE only when the QA agent signs off.

## Levels

| Level | What | Tooling | Needs API key? |
|-------|------|---------|----------------|
| L1 Unit | Rules engine: every field matcher, every parser (ABV/proof variants, unit conversions, warning normalization + caps check, fuzzy thresholds, N/A logic) | pytest | No |
| L2 Component/API | FastAPI endpoints via httpx TestClient with a mocked extractor: happy path, every error path (bad file, no label, API failure, timeout), batch partial-failure semantics | pytest + mock | No |
| L3 Integration | Live Claude vision extraction against real sample label images; schema conformance; latency measured and reported against the 5s budget (harness gates on verdicts) | pytest, gated: skip cleanly when ANTHROPIC_API_KEY absent | Yes |
| L4 E2E/UI | Browser-driven: drag-drop single label → per-field verdicts render; batch upload → progress + table + CSV downloads; error states render friendly messages | Playwright (or browser-pane manual protocol, scripted steps) | Yes (live) / No (mocked backend mode) |
| L5 Eval harness | Synthetic label set with expected per-field verdicts; harness script prints pass/fail matrix; ALL expected verdicts must hold | scripts/run_eval.py | Yes |
| L6 Deploy smoke | Deployed URL from outside the network: health check, one real label end-to-end < 5s, batch of 5, error case | scripted curl/Playwright checklist | Yes |

## Canonical trap cases (must exist as named tests from L1 up)

1. `STONE'S THROW` vs `Stone's Throw` → brand ✅ match
2. Warning with `Government Warning:` (title case) → F7 ❌ fail (caps rule)
3. Warning with one word changed ("might cause health problems") → F7 ❌ with clause diff
4. Warning with smart quotes / line breaks / double spaces → F7 ✅ (normalization)
5. `90 Proof` label vs `45%` application → F3 ✅ (conversion)
6. `40% ABV` label vs `45%` application → F3 ❌
7. `750 mL` vs `75 cL` → F4 ✅; `750 mL` vs `700 mL` → F4 ❌
8. Domestic product, no country of origin on label → F6 N/A (not fail)
9. Import per application, no country on label → F6 ❌
10. Low-confidence extraction → field renders ⚠️ review, never silent ✅/❌

## Rules of the game

- L1/L2 run in CI-style on every change (`pytest` full suite, < 30s runtime).
- No test may hit the live API unless explicitly marked `@pytest.mark.live`
  and key-gated.
- The QA agent writes its own tests in `tests/qa/` — the build agent never
  edits or deletes files there. QA tests failing = gate failed.
- A QA finding is either FIXED, or documented in APPROACH.md as a known
  limitation with rationale. No silent dismissals.

## Work packages and gates

- **WP1 (Day 1):** scaffold + rules engine + extraction module (mockable interface). Gate: L1 + L2 green, QA adversarial pass on rules engine.
- **WP2 (Day 2):** single-label UI + error handling. Gate: L2 + L4 (mocked) green.
- **WP3 (Day 3):** batch + CSV + synthetic label set + eval harness. Gate: L1–L5 green (L3/L5 need key).
- **WP4 (Day 4):** docs + deploy. Gate: L6 green from external network, < 5s verified.
