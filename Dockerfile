# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY --from=uv /uv /uvx /usr/local/bin/
RUN uv sync --frozen --no-dev --no-install-project

FROM ${PYTHON_IMAGE}

ARG FFMPEG_VERSION=7:5.1.9-0+deb12u1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    HOME=/tmp/app-home \
    XDG_CONFIG_HOME=/tmp/app-home/config \
    XDG_CACHE_HOME=/tmp/app-home/cache

RUN apt-get update \
    && apt-get install --no-install-recommends --yes "ffmpeg=${FFMPEG_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app /tmp/app-home \
    && chown -R app:app /app /tmp/app-home

COPY --from=builder /opt/venv /opt/venv
COPY src /app/src
COPY LICENSE /app/LICENSE
COPY LICENSES /app/LICENSES

WORKDIR /app
USER app

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "sns_media_list.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
