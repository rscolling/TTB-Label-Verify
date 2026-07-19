# TTB Label Verify — runtime image.
# Build:  docker build -t ttb-label-verify .
# Run:    docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=your-key ttb-label-verify
# Optional: -e BATCH_CONCURRENCY=4 (parallel labels per batch)

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/ttb-label-verify

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

RUN useradd --create-home appuser
USER appuser

# Honor the platform's injected $PORT (Render, Cloud Run, …); default 8000 for
# a plain `docker run -p 8000:8000`. exec so SIGTERM reaches uvicorn directly.
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
