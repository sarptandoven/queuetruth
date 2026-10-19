# syntax=docker/dockerfile:1.7
# Multi-stage build. Stage 1 builds the queuetruth abi3 wheel (maturin, Rust) and the queuetruth-service wheel and
# exports the runtime dependencies of queuetruth-service from uv.lock as hash-pinned requirements. Stage 2 installs
# exactly those (pip --require-hashes --no-deps: nothing is resolved at build time, every artifact must match its
# locked sha256), then the two local wheels without dependencies, and runs as a non-root user.
# Base images are pinned by digest; refresh a tag and its digest together, e.g.
#   docker buildx imagetools inspect python:3.13-slim-bookworm
# Build from the repository root:
#   docker build -f deploy/docker/Dockerfile -t queuetruth:dev .
FROM rust:1.97-slim-bookworm@sha256:2775a09d208ff0d7c1f50490c45b62db929e87ba1dcbc3f2132ac71a704bcdd3 AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.31@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437 /uv /usr/local/bin/uv
WORKDIR /src
COPY . .
ENV CARGO_TARGET_DIR=/tmp/target
RUN mkdir -p /wheels \
 && uv export --frozen --no-dev --no-emit-workspace --package queuetruth-service \
      --format requirements-txt --output-file /wheels/requirements.txt \
 && uv build --wheel --out-dir /wheels . \
 && uv build --wheel --out-dir /wheels service

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS runtime
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin qt \
 && mkdir -p /var/lib/queuetruth/jobs \
 && chown 10001:10001 /var/lib/queuetruth/jobs \
 && chmod 0700 /var/lib/queuetruth/jobs
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-deps --require-hashes -r /wheels/requirements.txt \
 && pip install --no-cache-dir --no-deps /wheels/*.whl \
 && pip check \
 && rm -rf /wheels
USER 10001:10001
WORKDIR /tmp
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 QT_STORE_ROOT=/tmp/qt-store QT_JOB_ROOT=/var/lib/queuetruth/jobs
EXPOSE 8080
CMD ["queuetruth-service", "api", "--host", "0.0.0.0", "--port", "8080"]
