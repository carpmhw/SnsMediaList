# SNS Media List Operations

## Deployment

Requirements: Docker Engine with Compose v2, outbound HTTPS DNS resolution, and a private or trusted operator audience. The service is intentionally not an anonymous public proxy.

```bash
docker compose build --pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8000/healthz
```

The Compose service runs one worker as UID 10001, uses a read-only root filesystem, and writes only to bounded tmpfs mounts. It has no persistent media or token volume. The application access log is disabled with `--no-access-log`; keep equivalent filtering at any reverse proxy.

The default limits are conservative:

| Setting | Default |
| --- | ---: |
| `SNS_MEDIA_TOKEN_TTL_SECONDS` | 600 |
| `SNS_MEDIA_TOKEN_CAPACITY` | 200 |
| `SNS_MEDIA_EXTRACTION_TIMEOUT_SECONDS` | 45 |
| `SNS_MEDIA_EXTRACTION_OUTPUT_LIMIT` | 2000000 bytes |
| `SNS_MEDIA_MAX_DOWNLOAD_BYTES` | 500000000 bytes |
| `SNS_MEDIA_CONNECT_TIMEOUT_SECONDS` | 10 seconds |
| `SNS_MEDIA_READ_TIMEOUT_SECONDS` | 30 seconds |
| `SNS_MEDIA_DOWNLOAD_TIMEOUT_SECONDS` | 120 seconds |
| `SNS_MEDIA_MAX_REDIRECTS` | 3 |
| `SNS_MEDIA_MAX_EXTRACTIONS` | 1 |
| `SNS_MEDIA_MAX_DOWNLOADS` | 4 |

Override values in a deployment-specific Compose override or environment file. Do not pass platform cookies, credentials, extractor configuration, or proxy credentials to the container.

## Upgrade And Rollback

1. Review the pinned dependency change and its `uv.lock` diff.
2. Run `uv run python scripts/verify_gallery_contract.py`.
3. Run `uv run python scripts/container_smoke.py`.
4. Build and tag the candidate image, then run the health check and owner-controlled smoke tests.
5. Deploy with `docker compose up -d --no-deps app`.

To roll back, stop the current service and redeploy the previous image tag or previous checkout:

```bash
docker compose stop -t 10 app
docker compose up -d app
```

Tokens and extraction state are intentionally lost whenever the container is replaced or restarted. Users must analyze the original post again.

## Reverse Proxy Logging

Do not record token-bearing application paths. The application filters Uvicorn access logs, but a reverse proxy can log the request before it reaches the app. For Nginx, use a separate access log with an empty format for token routes:

```nginx
map $request_uri $safe_access_log {
    default 1;
    ~^/api/media/[^/?]+/(?:preview|download)(?:\?|$) 0;
}

map $safe_access_log $access_log_name {
    0 off;
    1 /var/log/nginx/sns-media-list.access.log;
}

server {
    access_log $access_log_name combined;
    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

Also avoid logging query strings, request bodies, cookies, authorization headers, and upstream response headers. Never log the `POST /api/extractions` body or the complete upstream media URL.

## Trusted Proxies

By default, request limits use the socket peer address and ignore `Forwarded` and `X-Forwarded-For`. Only set `SNS_MEDIA_TRUSTED_PROXY_CIDRS` when the service is reachable exclusively through a controlled proxy. The value is a JSON array of CIDR strings, for example:

```yaml
environment:
  SNS_MEDIA_TRUSTED_PROXY_CIDRS: '["10.0.0.0/8", "192.168.10.0/24"]'
```

The proxy must overwrite, not append to, forwarded client headers. Do not trust arbitrary Internet clients or a broad public CIDR.

## Anonymous Platform Limitations

Only public single-post Instagram `/p/` and `/reel/` URLs and public X status URLs are supported. Private, follower-only, age-gated, deleted, login-required, or rate-limited content is expected to fail without credentials. The service never accepts platform cookies or credentials. Direct progressive media is required; adaptive HLS/DASH streams are not merged.

## Troubleshooting

- `docker compose ps` is unhealthy: inspect `docker compose logs --no-log-prefix app` and query `/healthz`; do not enable token-path access logs.
- `extraction_failed` or `post_unavailable`: verify the URL is a public single post and check anonymous platform availability. Do not add cookies or credentials.
- `upstream_rate_limited`: reduce operator concurrency and wait for the platform limit to clear.
- `local_rate_limited`: the configured process-wide or per-client slot is occupied; retry after the response's `Retry-After` interval.
- `token_not_found` or `token_expired`: the service restarted or the ten-minute token TTL elapsed; analyze the original post again.
- `capacity_exceeded`: wait for token expiry or raise the bounded token capacity only after reviewing memory limits.
- `unsafe_destination` or `upstream_media_invalid`: do not bypass the host, DNS, MIME, redirect, or byte-limit checks; review the pinned extractor contract instead.

## Automated Checks

Run the deployment checks from the repository root:

```bash
uv run python scripts/container_smoke.py
docker compose config --quiet
```

The smoke command builds the image, waits for health, verifies UID 10001 and the read-only root filesystem, confirms `/tmp` is ephemeral across restart, confirms no application media directory is present, and verifies a ten-second graceful stop.

## Owner-Controlled Manual Smoke Tests

Use only public posts that the service owner is authorized to test. Provide one owner-controlled URL for each case; do not put credentials or cookies in the command line:

```bash
uv run python scripts/manual_smoke.py \
  --instagram-image 'https://www.instagram.com/p/OWNER_CONTROLLED_IMAGE/' \
  --instagram-reel 'https://www.instagram.com/reel/OWNER_CONTROLLED_REEL/' \
  --instagram-mixed 'https://www.instagram.com/p/OWNER_CONTROLLED_MIXED/' \
  --x-image 'https://x.com/owner/status/OWNER_CONTROLLED_IMAGE' \
  --x-video 'https://x.com/owner/status/OWNER_CONTROLLED_VIDEO' \
  --x-gif 'https://x.com/owner/status/OWNER_CONTROLLED_GIF'
```

The script checks that each case returns an application-owned response, preserves media order, exposes only opaque application URLs, and provides downloadable media. Record only the case label, status, item count, and outcome. Replace the placeholder URLs with real owner-controlled public posts before running this check.
