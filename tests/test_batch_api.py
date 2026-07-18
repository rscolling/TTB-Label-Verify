"""L2: POST /api/verify-batch with the extractor dependency faked.

Covers both application-data modes (shared fields, CSV manifest), per-label
error isolation, the friendly batch-size cap, manifest validation errors,
and proof that concurrency is actually bounded by BATCH_CONCURRENCY.
No test here touches the network or needs an API key.
"""

from __future__ import annotations

import threading
import time

from app.extraction import ExtractionError
from app.models import ExtractedLabel
from tests.conftest import FakeExtractor

FIELD_ORDER = [
    "brand",
    "class_type",
    "abv",
    "net_contents",
    "producer",
    "origin_country",
    "government_warning",
]

MANIFEST_HEADER = "filename,brand,class_type,abv,net_contents,producer,origin_country,is_import"


def post_batch(client, files, form=None, manifest: str | None = None):
    upload = [("files", (name, data, "image/png")) for name, data in files]
    if manifest is not None:
        upload.append(("manifest", ("manifest.csv", manifest.encode("utf-8"), "text/csv")))
    return client.post("/api/verify-batch", files=upload, data=form or {})


class TestSharedFieldsMode:
    def test_happy_path_returns_aligned_results_and_summary(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        client = make_client(FakeExtractor(good_extraction))
        names = ["a.png", "b.png", "c.png"]
        response = post_batch(client, [(n, png_bytes) for n in names], form=good_application)
        assert response.status_code == 200
        body = response.json()

        assert body["summary"]["total"] == 3
        assert body["summary"]["match"] == 3
        assert body["summary"]["review"] == 0
        assert body["summary"]["mismatch"] == 0
        assert body["summary"]["error"] == 0
        assert isinstance(body["summary"]["total_time_ms"], int)

        assert [entry["filename"] for entry in body["results"]] == names
        for entry in body["results"]:
            assert entry["overall_status"] == "match"
            assert isinstance(entry["processing_time_ms"], int)
            assert list(entry["fields"]) == FIELD_ORDER

    def test_missing_shared_brand_and_no_manifest_is_friendly_400(self, make_client, png_bytes):
        client = make_client(FakeExtractor())
        response = post_batch(client, [("a.png", png_bytes)], form={"brand": "  "})
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "missing_application"
        assert "brand name" in error["message"]

    def test_batch_reuses_single_label_pipeline_shapes(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        """A batch entry's fields are shaped exactly like /api/verify's fields."""
        client = make_client(FakeExtractor(good_extraction))
        single = client.post(
            "/api/verify",
            files={"file": ("a.png", png_bytes, "image/png")},
            data=good_application,
        ).json()
        batch = post_batch(client, [("a.png", png_bytes)], form=good_application).json()
        entry = batch["results"][0]
        assert entry["overall_status"] == single["overall_status"]
        # Timing differs run to run; everything else is byte-identical.
        assert entry["fields"] == single["fields"]


class TestPartialFailure:
    def test_one_corrupt_file_yields_error_entry_and_batch_continues(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        client = make_client(FakeExtractor(good_extraction))
        files = [
            ("good1.png", png_bytes),
            ("corrupt.png", b"this is not an image at all"),
            ("good2.png", png_bytes),
        ]
        body = post_batch(client, files, form=good_application).json()

        assert body["summary"] == {
            "total": 3,
            "match": 2,
            "review": 0,
            "mismatch": 0,
            "error": 1,
            "total_time_ms": body["summary"]["total_time_ms"],
        }
        by_name = {entry["filename"]: entry for entry in body["results"]}
        assert by_name["good1.png"]["overall_status"] == "match"
        assert by_name["good2.png"]["overall_status"] == "match"
        error = by_name["corrupt.png"]["error"]
        assert error["code"] == "bad_file"
        assert "doesn't look like an image" in error["message"]
        assert "fields" not in by_name["corrupt.png"]

    def test_extraction_failure_isolated_per_label(self, make_client, png_bytes, good_application):
        """The whole batch shares one extractor; if it errors, every label gets
        its own error entry rather than the request failing wholesale."""
        client = make_client(FakeExtractor(error=ExtractionError("The label reading service is unavailable right now.")))
        body = post_batch(client, [("a.png", png_bytes), ("b.png", png_bytes)], form=good_application).json()
        assert body["summary"]["error"] == 2
        for entry in body["results"]:
            assert entry["error"]["code"] == "extraction_failed"

    def test_no_label_detected_isolated_per_label(self, make_client, png_bytes, good_application):
        client = make_client(FakeExtractor(ExtractedLabel(label_detected=False)))
        body = post_batch(client, [("a.png", png_bytes)], form=good_application).json()
        entry = body["results"][0]
        assert entry["error"]["code"] == "no_label"
        assert "couldn't read a label" in entry["error"]["message"]


class TestBatchSizeCap:
    def test_300_files_accepted(self, make_client, png_bytes, good_extraction, good_application):
        client = make_client(FakeExtractor(good_extraction))
        files = [(f"label-{i}.png", png_bytes) for i in range(300)]
        response = post_batch(client, files, form=good_application)
        assert response.status_code == 200
        assert response.json()["summary"]["total"] == 300

    def test_301_files_rejected_with_friendly_413(self, make_client, png_bytes, good_application):
        extractor = FakeExtractor()
        client = make_client(extractor)
        files = [(f"label-{i}.png", png_bytes) for i in range(301)]
        response = post_batch(client, files, form=good_application)
        assert response.status_code == 413
        error = response.json()["error"]
        assert error["code"] == "batch_too_large"
        assert "301" in error["message"]
        assert "300" in error["message"]
        assert extractor.calls == 0  # rejected before any processing


class TestManifestMode:
    def manifest_for(self, rows: list[str]) -> str:
        return "\n".join([MANIFEST_HEADER, *rows])

    def test_each_file_verified_against_its_own_row(self, make_client, png_bytes, good_extraction):
        """Same extraction for both files; per-row application data flips the verdict."""
        client = make_client(FakeExtractor(good_extraction))
        manifest = self.manifest_for(
            [
                'match.png,"Stone\'s Throw",Kentucky Straight Bourbon Whiskey,45%,750 mL,,,false',
                "mismatch.png,Totally Different Brand,Vodka,40%,500 mL,,,false",
            ]
        )
        body = post_batch(client, [("match.png", png_bytes), ("mismatch.png", png_bytes)], manifest=manifest).json()
        by_name = {entry["filename"]: entry for entry in body["results"]}
        assert by_name["match.png"]["overall_status"] == "match"
        assert by_name["mismatch.png"]["overall_status"] == "mismatch"
        assert by_name["mismatch.png"]["fields"]["brand"]["verdict"] == "mismatch"
        assert by_name["mismatch.png"]["fields"]["abv"]["verdict"] == "mismatch"

    def test_filename_match_is_case_insensitive_basename(self, make_client, png_bytes, good_extraction):
        client = make_client(FakeExtractor(good_extraction))
        manifest = self.manifest_for(['photos/LABEL-One.PNG,"Stone\'s Throw",,,,,,'])
        body = post_batch(client, [("label-one.png", png_bytes)], manifest=manifest).json()
        assert body["results"][0]["overall_status"] == "match"

    def test_file_without_manifest_row_gets_error_entry_batch_continues(
        self, make_client, png_bytes, good_extraction
    ):
        client = make_client(FakeExtractor(good_extraction))
        manifest = self.manifest_for(['known.png,"Stone\'s Throw",,,,,,'])
        body = post_batch(client, [("known.png", png_bytes), ("unknown.png", png_bytes)], manifest=manifest).json()
        by_name = {entry["filename"]: entry for entry in body["results"]}
        assert by_name["known.png"]["overall_status"] == "match"
        error = by_name["unknown.png"]["error"]
        assert error["code"] == "no_application"
        assert "unknown.png" in error["message"]

    def test_extra_manifest_rows_are_ignored(self, make_client, png_bytes, good_extraction):
        """The UI re-sends the full manifest with every sub-batch; rows for
        files not in this request must not error."""
        client = make_client(FakeExtractor(good_extraction))
        manifest = self.manifest_for(
            [
                'here.png,"Stone\'s Throw",,,,,,',
                "later-chunk.png,Some Other Brand,,,,,,",
            ]
        )
        body = post_batch(client, [("here.png", png_bytes)], manifest=manifest).json()
        assert body["summary"] == {
            "total": 1,
            "match": 1,
            "review": 0,
            "mismatch": 0,
            "error": 0,
            "total_time_ms": body["summary"]["total_time_ms"],
        }

    def test_manifest_missing_brand_column_is_friendly_400(self, make_client, png_bytes):
        client = make_client(FakeExtractor())
        manifest = "filename,abv\na.png,45%"
        response = post_batch(client, [("a.png", png_bytes)], manifest=manifest)
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "bad_manifest"
        assert "'brand' column" in error["message"]

    def test_manifest_row_without_brand_is_friendly_400(self, make_client, png_bytes):
        client = make_client(FakeExtractor())
        manifest = self.manifest_for(["a.png,,,,,,,"])
        response = post_batch(client, [("a.png", png_bytes)], manifest=manifest)
        assert response.status_code == 400
        assert "no brand" in response.json()["error"]["message"]

    def test_manifest_duplicate_filename_is_friendly_400(self, make_client, png_bytes):
        client = make_client(FakeExtractor())
        manifest = self.manifest_for(["a.png,Brand One,,,,,,", "a.png,Brand Two,,,,,,"])
        response = post_batch(client, [("a.png", png_bytes)], manifest=manifest)
        assert response.status_code == 400
        assert "more than once" in response.json()["error"]["message"]

    def test_manifest_bad_is_import_value_is_friendly_400(self, make_client, png_bytes):
        client = make_client(FakeExtractor())
        manifest = self.manifest_for(["a.png,Brand,,,,,,maybe"])
        response = post_batch(client, [("a.png", png_bytes)], manifest=manifest)
        assert response.status_code == 400
        assert "is_import" in response.json()["error"]["message"]

    def test_empty_manifest_is_friendly_400(self, make_client, png_bytes):
        client = make_client(FakeExtractor())
        response = post_batch(client, [("a.png", png_bytes)], manifest="")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_manifest"

    def test_non_utf8_manifest_is_friendly_400(self, make_client, png_bytes):
        client = make_client(FakeExtractor())
        upload = [
            ("files", ("a.png", png_bytes, "image/png")),
            ("manifest", ("manifest.csv", b"\xff\xfe\x00\x01\x02invalid", "text/csv")),
        ]
        response = client.post("/api/verify-batch", files=upload)
        assert response.status_code == 400
        assert "CSV" in response.json()["error"]["message"]


class CountingExtractor:
    """FakeExtractor variant that records how many extracts run at once."""

    def __init__(self, result: ExtractedLabel, delay: float = 0.03) -> None:
        self.result = result
        self.delay = delay
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            time.sleep(self.delay)  # hold the slot long enough to force overlap
        finally:
            with self._lock:
                self._in_flight -= 1
        return self.result


class TestConcurrencyBounding:
    def test_parallelism_is_bounded_by_batch_concurrency(
        self, make_client, png_bytes, good_extraction, good_application, monkeypatch
    ):
        monkeypatch.setenv("BATCH_CONCURRENCY", "3")
        extractor = CountingExtractor(good_extraction)
        client = make_client(extractor)
        files = [(f"label-{i}.png", png_bytes) for i in range(12)]
        body = post_batch(client, files, form=good_application).json()

        assert body["summary"]["match"] == 12
        assert extractor.max_in_flight <= 3, "semaphore must cap concurrent extractions"
        assert extractor.max_in_flight >= 2, "labels should actually run in parallel"

    def test_concurrency_of_one_serializes_the_batch(
        self, make_client, png_bytes, good_extraction, good_application, monkeypatch
    ):
        monkeypatch.setenv("BATCH_CONCURRENCY", "1")
        extractor = CountingExtractor(good_extraction, delay=0.01)
        client = make_client(extractor)
        files = [(f"label-{i}.png", png_bytes) for i in range(6)]
        body = post_batch(client, files, form=good_application).json()
        assert body["summary"]["match"] == 6
        assert extractor.max_in_flight == 1
