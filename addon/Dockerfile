# Home Assistant bouwt dit met BUILD_FROM (zie build.yaml). Standaard een gewone python-alpine, zodat
# dezelfde image ook buiten HA draait (tests, docker compose in e2e/).
ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM} AS base

# De HA-basisimages bevatten geen python; python:alpine wel.
RUN command -v python3 >/dev/null || apk add --no-cache python3 py3-pip
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY kkaddon ./kkaddon

FROM base AS test
RUN pip install --no-cache-dir pytest
COPY tests ./tests
CMD ["pytest", "-q", "tests"]

FROM base AS final
CMD ["python", "-m", "kkaddon"]
