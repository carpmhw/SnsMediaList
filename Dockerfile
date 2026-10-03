# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.12-alpine@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111
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

ARG FFMPEG_VERSION=8.1.2-r0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    HOME=/tmp/app-home \
    XDG_CONFIG_HOME=/tmp/app-home/config \
    XDG_CACHE_HOME=/tmp/app-home/cache

RUN apk add --no-cache "ffmpeg=${FFMPEG_VERSION}" "pcre2=10.49-r0" \
    && addgroup -S -g 10001 app \
    && adduser -S -D -H -u 10001 -G app -s /sbin/nologin app \
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
