"""L5 eval harness: run the synthetic label set through the REAL pipeline.

For every label in eval/manifest.json this script runs ClaudeExtractor (live
Claude vision call) + the deterministic rules engine — in-process, exactly
the code path the API serves — and compares each per-field verdict against
the manifest's expected verdict list. It prints a per-label x per-field
matrix, totals, and per-label latency (mean/max, R2 budget is 5s), and
exits non-zero if any verdict falls outside its expected set.

Usage (requires ANTHROPIC_API_KEY):
    python eval/run_eval.py                 # full set (16 labels, 16 API calls)
    python eval/run_eval.py --limit 3       # first 3 labels only
    python eval/run_eval.py --label 09-proof-only   # one label by (partial) name
    python eval/run_eval.py --markdown      # matrix as a markdown table (for APPROACH.md)

The comparison/reporting logic is pure and unit-tested offline with a stub
extractor (tests/test_run_eval.py); only a real run needs the key.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow `python eval/run_eval.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from app.models import ApplicationData  # noqa: E402
from app.rules import verify  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = EVAL_DIR / "manifest.json"
LATENCY_BUDGET_S = 5.0  # R2

# manifest.json expected-verdict keys -> rules-engine field keys
MANIFEST_TO_FIELD = {
    "brand": "brand",
    "class_type": "class_type",
    "abv": "abv",
    "net_contents": "net_contents",
    "producer": "producer",
    "origin_country": "origin_country",
    "warning": "government_warning",
}


def load_manifest(path: Path = MANIFEST_PATH) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["labels"]


def build_application(entry: dict[str, Any]) -> ApplicationData:
    """Manifest application block -> ApplicationData (numbers become strings)."""
    application = entry["application"]

    def text(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    return ApplicationData(
        brand=text(application["brand"]) or "",
        class_type=text(application.get("class_type")),
        abv=text(application.get("abv")),
        net_contents=text(application.get("net_contents")),
        producer=text(application.get("producer")),
        origin_country=text(application.get("origin_country")),
        is_import=bool(application.get("is_import", False)),
    )


def compare_verdicts(
    actual_by_field: dict[str, str], expected: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """Compare actual verdicts to the manifest's allowed lists, in manifest order."""
    checks = []
    for manifest_key, allowed in expected.items():
        field = MANIFEST_TO_FIELD[manifest_key]
        actual = actual_by_field.get(field, "<missing>")
        checks.append(
            {
                "manifest_key": manifest_key,
                "field": field,
                "actual": actual,
                "allowed": list(allowed),
                "ok": actual in allowed,
            }
        )
    return checks


def evaluate_label(entry: dict[str, Any], extractor: Any, labels_dir: Path = EVAL_DIR) -> dict[str, Any]:
    """Run one label through extract + verify; never raises (errors are recorded)."""
    name = Path(entry["file"]).stem
    record: dict[str, Any] = {"name": name, "checks": [], "latency_s": None, "error": None}
    try:
        image_bytes = (labels_dir / entry["file"]).read_bytes()
        started = time.perf_counter()
        extracted = extractor.extract(image_bytes)
        results = verify(extracted, build_application(entry))
        record["latency_s"] = time.perf_counter() - started
        actual_by_field = {result.field: result.verdict.value for result in results}
        record["checks"] = compare_verdicts(actual_by_field, entry["expected"])
    except Exception as exc:  # an eval run reports failures; it does not crash
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def record_passed(record: dict[str, Any]) -> bool:
    return record["error"] is None and all(check["ok"] for check in record["checks"])


def _cell(check: dict[str, Any], markdown: bool) -> str:
    if check["ok"]:
        return check["actual"]
    want = "|".join(check["allowed"])
    return f"**✗ {check['actual']} (want {want})**" if markdown else f"✗ {check['actual']} (want {want})"


def render_report(records: list[dict[str, Any]], markdown: bool = False) -> str:
    """Per-label x per-field matrix + totals + latency, plain or markdown."""
    columns = ["label", *MANIFEST_TO_FIELD.keys(), "time"]
    rows: list[list[str]] = []
    for record in records:
        if record["error"] is not None:
            rows.append([record["name"], f"ERROR: {record['error']}", *[""] * (len(columns) - 3), ""])
            continue
        cells = {check["manifest_key"]: _cell(check, markdown) for check in record["checks"]}
        latency = f"{record['latency_s']:.1f}s" if record["latency_s"] is not None else "-"
        rows.append([record["name"], *[cells.get(key, "-") for key in MANIFEST_TO_FIELD], latency])

    lines: list[str] = []
    if markdown:
        lines.append("| " + " | ".join(columns) + " |")
        lines.append("|" + "|".join("---" for _ in columns) + "|")
        for row in rows:
            lines.append("| " + " | ".join(row) + " |")
    else:
        widths = [max(len(column), *(len(row[i]) for row in rows)) if rows else len(column)
                  for i, column in enumerate(columns)]
        lines.append("  ".join(column.ljust(widths[i]) for i, column in enumerate(columns)))
        lines.append("  ".join("-" * widths[i] for i in range(len(columns))))
        for row in rows:
            lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(columns))))

    labels_passed = sum(1 for record in records if record_passed(record))
    all_checks = [check for record in records for check in record["checks"]]
    checks_passed = sum(1 for check in all_checks if check["ok"])
    errors = sum(1 for record in records if record["error"] is not None)
    latencies = [record["latency_s"] for record in records if record["latency_s"] is not None]

    lines.append("")
    lines.append(f"Labels passing: {labels_passed}/{len(records)}")
    lines.append(f"Field verdicts as expected: {checks_passed}/{len(all_checks)}")
    if errors:
        lines.append(f"Labels with errors: {errors}")
    if latencies:
        mean_s = statistics.mean(latencies)
        max_s = max(latencies)
        over = sum(1 for value in latencies if value > LATENCY_BUDGET_S)
        lines.append(
            f"Latency per label (extract+verify): mean {mean_s:.1f}s, max {max_s:.1f}s "
            f"(budget {LATENCY_BUDGET_S:g}s, over budget: {over})"
        )
    return "\n".join(lines)


def select_entries(entries: list[dict[str, Any]], label: str | None, limit: int | None) -> list[dict[str, Any]]:
    if label:
        entries = [entry for entry in entries if label.lower() in Path(entry["file"]).stem.lower()]
    if limit is not None:
        entries = entries[:limit]
    return entries


def main(argv: list[str] | None = None, extractor: Any = None) -> int:
    # Windows consoles default to a legacy codepage that can't print ✓/✗.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run the synthetic-label eval through the real pipeline.")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="only run the first N labels")
    parser.add_argument("--label", default=None, metavar="NAME", help="only run labels whose name contains NAME")
    parser.add_argument("--markdown", action="store_true", help="print the matrix as a markdown table")
    args = parser.parse_args(argv)

    if extractor is None:
        import os

        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(
                "ERROR: ANTHROPIC_API_KEY is not set.\n"
                "The eval harness makes live Claude vision calls (one per label).\n"
                "Set the key (e.g. in .env or the environment) and re-run:\n"
                "    python eval/run_eval.py [--limit N] [--label NAME] [--markdown]",
                file=sys.stderr,
            )
            return 2
        from app.extraction import ClaudeExtractor

        extractor = ClaudeExtractor()

    entries = select_entries(load_manifest(), args.label, args.limit)
    if not entries:
        print("No labels matched the given --label/--limit filters.", file=sys.stderr)
        return 2

    print(f"Running {len(entries)} label(s) through ClaudeExtractor + rules engine...\n", file=sys.stderr)
    records = [evaluate_label(entry, extractor) for entry in entries]
    print(render_report(records, markdown=args.markdown))
    return 0 if all(record_passed(record) for record in records) else 1


if __name__ == "__main__":
    sys.exit(main())
