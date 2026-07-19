#!/usr/bin/env python3
"""Form-ingestion eval harness scaffold (QA P1-7).

Runs deterministic parsers against golden spreadsheet fixtures under
eval/forms/. PDF/photo fixtures can be added later; when ANTHROPIC_API_KEY is
set, files with source_kind pdf-llm / image-llm would call the live extractor
(not enabled until golden PDFs ship).

Usage:
  python eval/run_form_eval.py
  python eval/run_form_eval.py --markdown
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.form_ingest import ingest_form  # noqa: E402

FORMS_DIR = Path(__file__).resolve().parent / "forms"
MANIFEST_PATH = FORMS_DIR / "manifest.json"


class _NoLlm:
    """Fail if a fixture unexpectedly routes to the LLM path."""

    def extract_rows(self, raw: bytes, kind: str):
        raise RuntimeError(f"LLM path not expected for this fixture (kind={kind})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Eval form ingestion against golden fixtures")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()

    if not MANIFEST_PATH.is_file():
        print(f"No form eval manifest at {MANIFEST_PATH}", file=sys.stderr)
        return 2

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    cases = manifest.get("cases", [])
    passed = 0
    failed = 0
    rows_out = []

    for case in cases:
        path = FORMS_DIR / case["file"]
        raw = path.read_bytes()
        try:
            result = ingest_form(case["file"], raw, _NoLlm())
        except Exception as exc:  # noqa: BLE001 — report fixture failures
            failed += 1
            rows_out.append((case["id"], "ERROR", str(exc)))
            continue

        ok = True
        reasons = []
        if result.source_kind != case["expected_source_kind"]:
            ok = False
            reasons.append(f"source_kind {result.source_kind!r} != {case['expected_source_kind']!r}")
        if len(result.rows) != case["expected_row_count"]:
            ok = False
            reasons.append(f"rows {len(result.rows)} != {case['expected_row_count']}")
        for check in case.get("row_checks", []):
            idx = check["index"]
            if idx >= len(result.rows):
                ok = False
                reasons.append(f"missing row {idx}")
                continue
            row = result.rows[idx]
            for field, expected in check.get("fields", {}).items():
                got = getattr(row, field, None)
                if got != expected:
                    ok = False
                    reasons.append(f"row{idx}.{field}={got!r} != {expected!r}")

        if ok:
            passed += 1
            rows_out.append((case["id"], "PASS", ""))
        else:
            failed += 1
            rows_out.append((case["id"], "FAIL", "; ".join(reasons)))

    if args.markdown:
        print("| case | result | detail |")
        print("|---|---|---|")
        for cid, status, detail in rows_out:
            print(f"| {cid} | {status} | {detail} |")
    else:
        for cid, status, detail in rows_out:
            print(f"{status:5}  {cid}" + (f"  {detail}" if detail else ""))
        print(f"\n{passed} passed, {failed} failed, {passed + failed} total")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
