"""FastAPI app: POST /api/verify (single label) and GET /api/health.

No persistence — uploads are processed in memory and discarded (R8).
"""

from __future__ import annotations

import time
from dataclasses import asdict
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

from app.extraction import BadImageError, ClaudeExtractor, ExtractionError, Extractor, prepare_image
from app.models import ApplicationData
from app.rules import overall_status, verify

load_dotenv()

app = FastAPI(title="TTB Label Verification", version="0.1.0")


@lru_cache(maxsize=1)
def get_extractor() -> Extractor:
    """Default production extractor; tests override this dependency."""
    return ClaudeExtractor()


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/verify", response_model=None)  # returns dict or error JSONResponse
def verify_label(
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
    start = time.perf_counter()
    image_bytes = file.file.read()

    try:
        prepare_image(image_bytes)  # validate early (and cheaply) -> friendly 400
    except BadImageError:
        return _error(
            400,
            "bad_file",
            "That file doesn't look like an image. Please upload a photo of the "
            "label as a JPG or PNG and try again.",
        )

    try:
        extracted = extractor.extract(image_bytes)
    except ExtractionError as exc:
        return _error(502, "extraction_failed", f"{exc} Please try again in a moment.")

    if not extracted.label_detected:
        return _error(
            422,
            "no_label",
            "We couldn't read a label in this image. Try a straight-on, well-lit "
            "photo where the label text fills most of the frame.",
        )

    application = ApplicationData(
        brand=brand,
        class_type=class_type,
        abv=abv,
        net_contents=net_contents,
        producer=producer,
        origin_country=origin_country,
        is_import=is_import,
    )
    results = verify(extracted, application)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    return {
        "overall_status": overall_status(results),
        "processing_time_ms": elapsed_ms,
        "fields": {result.field: asdict(result) for result in results},
    }
