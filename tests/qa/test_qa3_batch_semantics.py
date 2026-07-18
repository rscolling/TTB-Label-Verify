"""QA gate 3 — batch semantics, limits, concurrency config, timing, and R8 (L2).

Independent verification of the WP3 claims:
  * the 300-file cap admits exactly 300 and rejects 301 BEFORE any extraction;
  * per-label error isolation holds even for unexpected (non-domain) exceptions;
  * BATCH_CONCURRENCY env abuse (0, negative, garbage) degrades sanely;
  * per-label processing_time_ms excludes semaphore queue wait;
  * R8 — no upload bytes persist to disk (including the >1MB multipart spool
    rollover path) and no image data is echoed in the response.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time

import pytest
from PIL import Image

from app.models import ExtractedLabel
from tests.conftest import FakeExtractor


def post_batch(client, files, form=None, manifest: str | None = None):
    upload = [("files", (name, data, "image/png")) for name, data in files]
    if manifest is not None:
        upload.append(("manifest", ("manifest.csv", manifest.encode("utf-8"), "text/csv")))
    return client.post("/api/verify-batch", files=upload, data=form or {})


class SlowExtractor(FakeExtractor):
    def __init__(self, result: ExtractedLabel, delay: float) -> None:
        super().__init__(result)
        self.delay = delay

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        time.sleep(self.delay)
        return super().extract(image_bytes)


class EveryThirdCallExplodes(FakeExtractor):
    """Raises an UNEXPECTED exception type on every 3rd call — exercises the
    last-resort isolation branch, not the friendly domain errors."""

    def __init__(self, result: ExtractedLabel) -> None:
        super().__init__(result)
        self._lock = threading.Lock()

    def extract(self, image_bytes: bytes) -> ExtractedLabel:
        with self._lock:
            self.calls += 1
            n = self.calls
        if n % 3 == 0:
            raise RuntimeError("simulated unexpected crash inside the pipeline")
        return self.result


class TestQa3BatchCap:
    def test_qa3_exactly_300_files_all_processed(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        extractor = FakeExtractor(good_extraction)
        client = make_client(extractor)
        files = [(f"l{i}.png", png_bytes) for i in range(300)]
        started = time.perf_counter()
        response = post_batch(client, files, form=good_application)
        wall = time.perf_counter() - started
        assert response.status_code == 200
        body = response.json()
        assert body["summary"]["total"] == 300
        assert body["summary"]["match"] == 300
        assert extractor.calls == 300
        assert len(body["results"]) == 300
        # Pipeline overhead (excl. real extraction) must be far under R2 budget.
        assert wall < 30, f"300-file batch overhead took {wall:.1f}s"

    def test_qa3_301_files_rejected_before_any_extraction_spend(
        self, make_client, png_bytes, good_application
    ):
        extractor = FakeExtractor()
        client = make_client(extractor)
        files = [(f"l{i}.png", png_bytes) for i in range(301)]
        response = post_batch(client, files, form=good_application)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "batch_too_large"
        assert extractor.calls == 0, "413 must fire BEFORE extraction spend"

    def test_qa3_301_files_with_manifest_rejected_before_manifest_parse_errors(
        self, make_client, png_bytes
    ):
        """Cap check must win even when the manifest is ALSO broken — the user
        fixes one thing at a time, biggest first."""
        extractor = FakeExtractor()
        client = make_client(extractor)
        files = [(f"l{i}.png", png_bytes) for i in range(301)]
        response = post_batch(client, files, manifest="not,a,valid\nmanifest")
        assert response.status_code == 413
        assert extractor.calls == 0


class TestQa3Isolation:
    def test_qa3_all_corrupt_batch_returns_all_error_entries_zero_spend(
        self, make_client, good_application
    ):
        extractor = FakeExtractor()
        client = make_client(extractor)
        files = [(f"junk{i}.bin", b"not an image %d" % i) for i in range(5)]
        response = post_batch(client, files, form=good_application)
        assert response.status_code == 200  # batch itself succeeds; entries carry errors
        body = response.json()
        assert body["summary"]["error"] == 5
        assert body["summary"]["match"] == 0
        assert all(entry["error"]["code"] == "bad_file" for entry in body["results"])
        assert extractor.calls == 0  # corrupt files never reach the extractor

    def test_qa3_unexpected_exception_every_third_label_is_isolated(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        extractor = EveryThirdCallExplodes(good_extraction)
        client = make_client(extractor)
        files = [(f"l{i}.png", png_bytes) for i in range(12)]
        response = post_batch(client, files, form=good_application)
        assert response.status_code == 200
        body = response.json()
        assert body["summary"]["error"] == 4  # calls 3, 6, 9, 12
        assert body["summary"]["match"] == 8
        assert body["summary"]["total"] == 12
        errored = [e for e in body["results"] if "error" in e]
        assert all(e["error"]["code"] == "internal_error" for e in errored)
        # The raw exception text must NOT leak into the user-facing message.
        assert all("simulated" not in e["error"]["message"] for e in errored)

    def test_qa3_duplicate_uploaded_filenames_both_processed(
        self, make_client, png_bytes, good_extraction, good_application
    ):
        client = make_client(FakeExtractor(good_extraction))
        files = [("dup.png", png_bytes), ("dup.png", png_bytes)]
        body = post_batch(client, files, form=good_application).json()
        assert body["summary"]["total"] == 2
        assert [e["filename"] for e in body["results"]] == ["dup.png", "dup.png"]

    def test_qa3_duplicate_uploaded_filenames_share_one_manifest_row(
        self, make_client, png_bytes, good_extraction
    ):
        client = make_client(FakeExtractor(good_extraction))
        manifest = "filename,brand\ndup.png,Stone's Throw\n"
        files = [("dup.png", png_bytes), ("DUP.PNG", png_bytes)]
        body = post_batch(client, files, manifest=manifest).json()
        assert body["summary"]["match"] == 2

    def test_qa3_zero_files_is_rejected_not_processed(self, make_client):
        """No files at all → FastAPI rejects the request (422 validation).
        NOTE (documented observation): this is the framework's {"detail": ...}
        shape, not the app's friendly {"error": {...}} envelope. Unreachable
        through the UI (client-side guard); logged as a LOW finding for the
        report, not a gate failure."""
        extractor = FakeExtractor()
        client = make_client(extractor)
        response = client.post("/api/verify-batch", data={"brand": "X"})
        assert response.status_code == 422
        assert extractor.calls == 0


class TestQa3ConcurrencyEnvAbuse:
    @pytest.mark.parametrize("value", ["0", "-3", "abc", "", "4.5", "  "])
    def test_qa3_bad_batch_concurrency_env_never_crashes_a_batch(
        self, make_client, png_bytes, good_extraction, good_application, monkeypatch, value
    ):
        monkeypatch.setenv("BATCH_CONCURRENCY", value)
        client = make_client(FakeExtractor(good_extraction))
        files = [(f"l{i}.png", png_bytes) for i in range(4)]
        response = post_batch(client, files, form=good_application)
        assert response.status_code == 200, f"BATCH_CONCURRENCY={value!r} broke the batch"
        assert response.json()["summary"]["match"] == 4


class TestQa3PerLabelTiming:
    def test_qa3_processing_time_excludes_semaphore_queue_wait(
        self, make_client, png_bytes, good_application, good_extraction, monkeypatch
    ):
        """With concurrency 1 and a 0.15s extractor, label N waits ~N*0.15s in
        the queue. If processing_time_ms included queue wait, the last label
        would report ~750ms. It must report ~150ms like the first."""
        monkeypatch.setenv("BATCH_CONCURRENCY", "1")
        client = make_client(SlowExtractor(good_extraction, delay=0.15))
        files = [(f"l{i}.png", png_bytes) for i in range(5)]
        body = post_batch(client, files, form=good_application).json()
        times = [entry["processing_time_ms"] for entry in body["results"]]
        assert body["summary"]["total_time_ms"] >= 600, "batch was serialized; sanity"
        assert max(times) < 450, (
            f"per-label processing_time_ms includes queue wait: {times}"
        )


class TestQa3NoPersistenceR8:
    def _rgb_png_over_spool_limit(self) -> bytes:
        """A PNG comfortably over starlette's 1MB spool limit, forcing the
        multipart parser onto its disk-rollover path."""
        buffer = io.BytesIO()
        Image.frombytes("RGB", (700, 700), os.urandom(700 * 700 * 3)).save(buffer, format="PNG")
        data = buffer.getvalue()
        assert len(data) > 1_100_000, "test image must exceed the 1MB spool limit"
        return data

    def test_qa3_no_upload_bytes_persist_after_batch(
        self, make_client, good_extraction, good_application, tmp_path, monkeypatch
    ):
        """Route BOTH the temp spool and the cwd into fresh directories; after
        the batch completes neither may contain a single file."""
        spool_dir = tmp_path / "spool"
        work_dir = tmp_path / "cwd"
        spool_dir.mkdir()
        work_dir.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(spool_dir))
        monkeypatch.chdir(work_dir)

        client = make_client(FakeExtractor(good_extraction))
        big = self._rgb_png_over_spool_limit()
        response = post_batch(
            client, [("big1.png", big), ("big2.png", big)], form=good_application
        )
        assert response.status_code == 200
        assert response.json()["summary"]["match"] == 2
        assert list(spool_dir.iterdir()) == [], "multipart spool file survived the request"
        assert list(work_dir.iterdir()) == [], "something wrote into the working directory"

    def test_qa3_response_contains_no_image_data(
        self, make_client, good_extraction, good_application
    ):
        import base64

        client = make_client(FakeExtractor(good_extraction))
        big = self._rgb_png_over_spool_limit()
        response = post_batch(client, [("big.png", big)], form=good_application)
        assert response.status_code == 200
        text = response.text
        assert len(text) < 50_000, "response for one label should be a few KB of verdicts"
        assert "data:image" not in text
        assert base64.b64encode(big[:512]).decode("ascii")[:24] not in text
