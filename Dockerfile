# syntax=docker/dockerfile:1

FROM python:3.12.3-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN python -m pip install --no-cache-dir --prefix=/install .

FROM python:3.12.3-slim-bookworm

ARG FFMPEG_VERSION=7:5.1.9-0+deb12u1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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

COPY --from=builder /install /usr/local
COPY src /app/src
COPY LICENSE /app/LICENSE
COPY LICENSES /app/LICENSES

WORKDIR /app
USER app

EXPOSE 8000
CMD ["uvicorn", "sns_media_list.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
