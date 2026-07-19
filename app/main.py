"""FastAPI app: POST /api/verify (single label) and GET /api/health.

No persistence — uploads are processed in memory and discarded (R8).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.batch import MAX_BATCH_SIZE, ManifestError, batch_concurrency, normalize_filename, parse_manifest
from app.extraction import BadImageError, ClaudeExtractor, ExtractionError, Extractor, prepare_image
from app.form_ingest import ClaudeFormExtractor, FormExtractor, FormIngestError, ingest_form
from app.limits import (
    human_mb,
    max_batch_total_bytes,
    max_form_bytes,
    max_image_bytes,
)
from app.models import ApplicationData
from app.rules import build_result_payload, verify
from app.security import ProtectMiddleware, configured_api_key

load_dotenv()

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="TTB Label Verification", version="0.1.0")
app.add_middleware(ProtectMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@lru_cache(maxsize=1)
def get_extractor() -> Extractor:
    """Default production extractor; tests override this dependency."""
    return ClaudeExtractor()


@lru_cache(maxsize=1)
def get_form_extractor() -> FormExtractor:
    """Default production form extractor (PDF/photo forms); tests override this."""
    return ClaudeFormExtractor()


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


class NoLabelError(Exception):
    """The image decoded fine but no readable label was detected in it."""


BAD_FILE_MESSAGE = (
    "That file doesn't look like an image. Please upload a photo of the "
    "label as a JPG or PNG and try again."
)
NO_LABEL_MESSAGE = (
    "We couldn't read a label in this image. Try a straight-on, well-lit "
    "photo where the label text fills most of the frame."
)


def _too_large_message(kind: str, size: int, limit: int) -> str:
    return (
        f"That {kind} is {human_mb(size)} — the limit is {human_mb(limit)}. "
        "Please use a smaller file (or a lower-resolution photo) and try again."
    )


async def _read_capped(upload: UploadFile, limit: int, kind: str) -> bytes | JSONResponse:
    """Read an upload, rejecting when it exceeds `limit` bytes."""
    chunks: list[bytes] = []
    total = 0
    while True:
        piece = await upload.read(1024 * 1024)
        if not piece:
            break
        total += len(piece)
        if total > limit:
            return _error(413, "payload_too_large", _too_large_message(kind, total, limit))
        chunks.append(piece)
    return b"".join(chunks)


def run_label_check(image_bytes: bytes, application: ApplicationData, extractor: Extractor) -> dict[str, Any]:
    """The single-label pipeline, shared verbatim by /api/verify and /api/verify-batch.

    Raises BadImageError, ExtractionError, or NoLabelError; the callers map
    those to their own error shapes (HTTP error vs per-label batch entry).
    """
    start = time.perf_counter()
    prepare_image(image_bytes)  # validate early (and cheaply) -> friendly error
    extracted = extractor.extract(image_bytes)
    if not extracted.label_detected:
        raise NoLabelError()
    results = verify(extracted, application)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return build_result_payload(results, extracted, elapsed_ms)


@app.exception_handler(Exception)
def unexpected_error(request: Any, exc: Exception) -> JSONResponse:
    """Last-resort net: even an unforeseen bug renders the friendly error shape."""
    return _error(500, "internal_error", "Something went wrong on our end. Please try again.")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the single-page UI."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health(deep: bool = False) -> dict[str, Any]:
    """Liveness probe. With ?deep=1, also report config readiness (no live model call)."""
    body: dict[str, Any] = {"status": "ok"}
    key_set = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    body["api_key_configured"] = key_set
    body["auth_required"] = configured_api_key() is not None
    if deep:
        body["checks"] = {
            "anthropic_api_key": "configured" if key_set else "missing",
            "verify_api_key": "required" if configured_api_key() else "open",
            "extraction_model": os.environ.get("EXTRACTION_MODEL", "claude-sonnet-5"),
            "max_image_bytes": max_image_bytes(),
            "max_form_bytes": max_form_bytes(),
            "max_batch_size": MAX_BATCH_SIZE,
        }
    return body


@app.post("/api/verify", response_model=None)  # returns dict or error JSONResponse
async def verify_label(
    file: UploadFile = File(...),
    brand: str = Form(...),
    class_type: str | None = Form(None),
    abv: str | None = Form(None),
    net_contents: str | None = Form(None),
    producer: str | None = Form(None),
    origin_country: str | None = Form(None),
    is_import: bool = Form(False),
    extractor: Extractor = Depends(get_extractor),
) -> JSONResponse | dict[str, Any]:
    """Verify one label image against the application data."""
    image_bytes = await _read_capped(file, max_image_bytes(), "photo")
    if isinstance(image_bytes, JSONResponse):
        return image_bytes
    application = ApplicationData(
        brand=brand,
        class_type=class_type,
        abv=abv,
        net_contents=net_contents,
        producer=producer,
        origin_country=origin_country,
        is_import=is_import,
    )

    try:
        return run_label_check(image_bytes, application, extractor)
    except BadImageError:
        return _error(400, "bad_file", BAD_FILE_MESSAGE)
    except ExtractionError as exc:
        return _error(502, "extraction_failed", f"{exc} Please try again in a moment.")
    except NoLabelError:
        return _error(422, "no_label", NO_LABEL_MESSAGE)


@app.post("/api/ingest-form", response_model=None)  # returns dict or error JSONResponse
async def ingest_form_endpoint(
    file: UploadFile = File(...),
    form_extractor: FormExtractor = Depends(get_form_extractor),
) -> JSONResponse | dict[str, Any]:
    """Read the submittal form (any supported format) into normalized rows.

    Additive endpoint (WP7): the frozen /api/verify-batch contract is untouched
    — the client previews these rows, then serializes them back to the
    canonical CSV `manifest` field at scan time. Deterministic parsers handle
    CSV/TSV/XLSX; PDF and photo forms go through the LLM document extractor
    (perception only — verdicts stay deterministic). Nothing is persisted (R8).
    """
    raw = await _read_capped(file, max_form_bytes(), "submittal form")
    if isinstance(raw, JSONResponse):
        return raw
    try:
        result = ingest_form(file.filename or "form", raw, form_extractor)
    except FormIngestError as exc:
        return _error(400, "bad_form", str(exc))
    except ExtractionError as exc:
        return _error(502, "form_extraction_failed", f"{exc} Please try again in a moment.")
    return {
        "rows": [asdict(row) for row in result.rows],
        "source_kind": result.source_kind,
        "warnings": result.warnings,
    }


def _batch_error_entry(filename: str, code: str, message: str) -> dict[str, Any]:
    return {"filename": filename, "error": {"code": code, "message": message}}


@app.post("/api/verify-batch", response_model=None)  # returns dict or error JSONResponse
async def verify_batch(
    files: list[UploadFile] = File(...),
    manifest: UploadFile | None = File(None),
    brand: str | None = Form(None),
    class_type: str | None = Form(None),
    abv: str | None = Form(None),
    net_contents: str | None = Form(None),
    producer: str | None = Form(None),
    origin_country: str | None = Form(None),
    is_import: bool = Form(False),
    extractor: Extractor = Depends(get_extractor),
) -> JSONResponse | dict[str, Any]:
    """Verify many labels in one request (R4). See app/batch.py for the design.

    Two application-data modes: a CSV manifest (one row per file, matched by
    file name) or, without a manifest, one shared set of form fields applied
    to every file. Labels are processed concurrently under a semaphore
    (BATCH_CONCURRENCY, default 4); one bad file yields an error entry for
    that label and the batch continues.
    """
    batch_start = time.perf_counter()

    if len(files) > MAX_BATCH_SIZE:
        return _error(
            413,
            "batch_too_large",
            f"That's {len(files)} photos — we can check up to {MAX_BATCH_SIZE} in one batch. "
            "Please split the photos into smaller batches and try again.",
        )

    applications: dict[str, ApplicationData] | None = None
    shared: ApplicationData | None = None
    if manifest is not None:
        try:
            manifest_bytes = await _read_capped(manifest, max_form_bytes(), "CSV manifest")
            if isinstance(manifest_bytes, JSONResponse):
                return manifest_bytes
            applications = parse_manifest(manifest_bytes)
        except ManifestError as exc:
            return _error(400, "bad_manifest", str(exc))
    else:
        if not brand or not brand.strip():
            return _error(
                400,
                "missing_application",
                "Please either upload a CSV with each label's application details, "
                "or fill in at least the brand name to use for every photo.",
            )
        shared = ApplicationData(
            brand=brand,
            class_type=class_type,
            abv=abv,
            net_contents=net_contents,
            producer=producer,
            origin_country=origin_country,
            is_import=is_import,
        )

    payloads: list[tuple[str, bytes]] = []
    total_bytes = 0
    image_limit = max_image_bytes()
    batch_limit = max_batch_total_bytes()
    for position, upload in enumerate(files, start=1):
        data = await _read_capped(upload, image_limit, "photo")
        if isinstance(data, JSONResponse):
            return data
        total_bytes += len(data)
        if total_bytes > batch_limit:
            return _error(
                413,
                "payload_too_large",
                f"This batch is over {human_mb(batch_limit)} total. "
                "Please split the photos into smaller batches and try again.",
            )
        payloads.append((upload.filename or f"photo-{position}", data))

    semaphore = asyncio.Semaphore(batch_concurrency())

    async def check_one(filename: str, image_bytes: bytes) -> dict[str, Any]:
        if applications is not None:
            application = applications.get(normalize_filename(filename))
            if application is None:
                return _batch_error_entry(
                    filename,
                    "no_application",
                    f"The CSV doesn't have a row for '{filename}'. "
                    "Add a row with this file name and try again.",
                )
        else:
            assert shared is not None
            application = shared
        async with semaphore:
            try:
                # ClaudeExtractor.extract is sync; keep the event loop free.
                result = await asyncio.to_thread(run_label_check, image_bytes, application, extractor)
            except BadImageError:
                return _batch_error_entry(filename, "bad_file", BAD_FILE_MESSAGE)
            except ExtractionError as exc:
                return _batch_error_entry(filename, "extraction_failed", f"{exc} Please try again in a moment.")
            except NoLabelError:
                return _batch_error_entry(filename, "no_label", NO_LABEL_MESSAGE)
            except Exception:  # per-label isolation: never let one label sink the batch
                return _batch_error_entry(
                    filename, "internal_error", "Something went wrong checking this label. Please try again."
                )
        return {"filename": filename, **result}

    results = await asyncio.gather(*(check_one(name, data) for name, data in payloads))

    counts = {"match": 0, "review": 0, "mismatch": 0, "error": 0}
    for entry in results:
        counts["error" if "error" in entry else entry["overall_status"]] += 1

    return {
        "summary": {
            "total": len(results),
            **counts,
            "total_time_ms": int((time.perf_counter() - batch_start) * 1000),
        },
        "results": results,
    }
