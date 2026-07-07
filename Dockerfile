FROM python:3.12-slim

LABEL org.opencontainers.image.title="pdf-dispatch"
LABEL org.opencontainers.image.description="Self-hosted PDF splitting service — splits PDFs on barcode/QR code detection with web UI and REST API"
LABEL org.opencontainers.image.source="https://github.com/Lheriss/pdf-dispatch"

RUN apt-get update && apt-get install -y --no-install-recommends \
    libzbar0 \
    poppler-utils \
    libgl1 \
    gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY splitter/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY splitter/app.py .
COPY splitter/dispatch ./dispatch
COPY splitter/openapi.yaml ./openapi.yaml
COPY splitter/openapi.json ./openapi.json
COPY splitter/i18n ./i18n
COPY splitter/templates ./templates
COPY splitter/static ./static

RUN mkdir -p /data /data/input /data/output /data/output/error /data/output/processed /data/output/no_code

COPY splitter/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/healthz')" || exit 1

# Inject Git SHA at build time so /api/runtime can expose the exact version.
# Set by GitHub Actions: --build-arg GIT_SHA=${{ github.sha }}
# The docker-compose APP_VERSION must NOT override this (remove that line).
ARG GIT_SHA=unknown
ENV APP_VERSION=${GIT_SHA}

ENTRYPOINT ["/entrypoint.sh"]
