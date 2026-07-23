# SNS Media List 操作指南

## 部署

需求：Docker Engine、Docker Compose v2、可解析 outbound HTTPS 目的地的 DNS，以及私人或可信任的 operator 使用環境。本服務刻意不設計為公開匿名 proxy。

```bash
docker compose build --pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8000/healthz
```

Compose service 使用單一 worker，以 UID 10001 的 non-root user 執行，root filesystem 為 read-only，且只能寫入有容量限制的 tmpfs。系統不掛載持久化 media 或 token volume。application access log 已透過 `--no-access-log` 停用；所有 reverse proxy 也必須套用同等的 filtering。

預設限制採保守設定：

| 設定 | 預設值 |
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
| `SNS_MEDIA_THUMBNAIL_INPUT_BYTES` | 32000000 bytes |
| `SNS_MEDIA_THUMBNAIL_OUTPUT_BYTES` | 1000000 bytes |
| `SNS_MEDIA_THUMBNAIL_TIMEOUT_SECONDS` | 10 seconds |
| `SNS_MEDIA_THUMBNAIL_CONCURRENCY` | 1 |
| `SNS_MEDIA_THUMBNAIL_CACHE_BYTES` | 32000000 bytes |
| `SNS_MEDIA_THUMBNAIL_MAX_EDGE` | 640 pixels |

請使用 deployment-specific Compose override 或 environment file 覆寫設定。預設 Compose 不掛載平台 Cookie；若啟用驗證，只能使用下方 read-only file mount。不得將 Cookie value、credentials、extractor config 或 proxy credentials 放入 environment、command line、request body 或 log。

## 平台 Cookie 驗證

平台 Cookie 是 bearer credential，會把 extraction 範圍擴大到該帳號可見的所有支援單篇貼文與精確 Story。配置 Instagram Cookie 後，任何服務使用者都可能透過精確 URL 間接使用 operator Instagram 帳號的 session，讀取該帳號看見的私人、Close Friends 或其他受眾限定 Story。服務不提供 per-user authorization，因此只適合本人管理的可信網路；請使用低權限專用帳號，不要使用個人主要帳號。

先將瀏覽器匯出的 Netscape `cookies.txt` 放在 host 的受限目錄，確認檔案由 container UID 10001 可讀取，且不要把 Cookie value 放在 shell command。Instagram 與 X 使用獨立 override：

```bash
SNS_MEDIA_INSTAGRAM_COOKIE_HOST_FILE=/srv/secrets/instagram.cookies.txt \
  docker compose -f docker-compose.yaml -f docker-compose.instagram-auth.yaml config --quiet
SNS_MEDIA_INSTAGRAM_COOKIE_HOST_FILE=/srv/secrets/instagram.cookies.txt \
  docker compose -f docker-compose.yaml -f docker-compose.instagram-auth.yaml up -d --build
```

```bash
SNS_MEDIA_X_COOKIE_HOST_FILE=/srv/secrets/x.cookies.txt \
  docker compose -f docker-compose.yaml -f docker-compose.x-auth.yaml config --quiet
SNS_MEDIA_X_COOKIE_HOST_FILE=/srv/secrets/x.cookies.txt \
  docker compose -f docker-compose.yaml -f docker-compose.x-auth.yaml up -d --build
```

override 只將 host file 以 read-only 方式掛載到固定 `/run/secrets/...` path，application 每次 subprocess 使用對應平台的 path，並要求 `cookies-update=false`。Cookie 不會進入 API response、token record、log、preview 或 download request。兩個平台可分開啟用；不使用 override 即維持匿名模式。

application 不快取 Cookie 內容；每個新啟動的 extractor process 都會重新開啟掛載路徑並立即讀取目前檔案。輪替或撤銷時，先在平台撤銷舊 session，再以新內容覆寫相同 host file 並保留 inode；若部署流程以 rename 更換 inode，必須重新建立 container，避免 bind mount 繼續指向舊檔。此後的新 extractor process 會立即使用新檔，進行中的 process 不會熱重載。

已發行的短效 token 不含 Cookie，Cookie 輪替或撤銷不會主動收回 token；token 只會在 TTL 到期或 service 重新啟動時失效，如需立即失效就重新啟動 service。CDN preview 與 download request 不會攜帶 Cookie；若 CDN 需要平台 session，系統會 fail closed，而不會轉送 operator credential。

目前兩個 override 與 application 使用同一個 container UID 10001。正常 invocation 只會把選定平台的 Cookie path 傳給 `gallery-dl`，但惡意或遭竄改的 extractor 若能自行探索同 UID 可讀檔案，仍可能讀取另一個 mounted Cookie；這是目前 threat model 的已知限制，不可視為 per-platform sandbox。若需要更強隔離，應另行設計分離 worker 或 sandbox change。

要 rollback 到匿名模式，先停止使用 auth override 的 service，再只用預設 Compose 啟動：

```bash
docker compose -f docker-compose.yaml -f docker-compose.instagram-auth.yaml down
docker compose -f docker-compose.yaml up -d --build
```

若同時啟用 X，將第一個 command 的 override 替換為 `docker-compose.x-auth.yaml`；兩個平台都啟用時先移除兩個 override。Rollback 不會恢復舊 token 或 extraction state。

## 升級與 rollback

1. 審查 pinned dependency 變更與 `uv.lock` diff。
2. 執行 `uv run python scripts/verify_gallery_contract.py`。
3. 執行 `uv run python scripts/container_smoke.py`。
4. 建置並標記 candidate image，接著執行 health check 與 owner-controlled smoke tests。
5. 使用 `docker compose up -d --no-deps app` 部署。

若需 rollback，停止目前 service，並重新部署前一個 image tag 或 checkout：

```bash
docker compose stop -t 10 app
docker compose up -d app
```

每次替換或重新啟動 container 時，token 與 extraction state 都會刻意遺失。使用者必須重新分析原始貼文。

## 預覽與縮圖

預覽會優先使用 gallery-dl metadata 或明確支援的平台 CDN raster preview。只有在沒有可信 CDN preview 時，第一次載入預覽才會由 application 透過受限的 FFmpeg 產生 JPEG；成功結果只存於 process-local、32 MB 的 bounded cache，最長不超過 token TTL。FFmpeg 不會自行連線、讀取平台 Cookie 或寫入持久媒體檔案。

生成流程最多讀取 32 MB、輸出 1 MB、執行 10 秒，並限制同時一個工作。超限、不支援格式或生成失敗會顯示本機 fallback，但不影響原始檔案下載。調高上述設定前，必須重新審查 768 MB memory、1 CPU 與 64 MB `/tmp` 限制。

## Reverse proxy logging

不得記錄 token-bearing application path。application 會過濾 Uvicorn access log，但 reverse proxy 可能在 request 到達 app 前就寫入記錄。Nginx 可針對 token route 使用空白格式的獨立 access log：

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

同時避免記錄 query string、request body、Cookie、authorization header 與 upstream response header。絕對不要記錄 `POST /api/extractions` body 或完整 upstream media URL。

## Trusted proxy

預設情況下，request limit 使用 socket peer address，並忽略 `Forwarded` 與 `X-Forwarded-For`。只有當 service 僅能透過受控 proxy 存取時，才可設定 `SNS_MEDIA_TRUSTED_PROXY_CIDRS`。此值為 CIDR 字串的 JSON array，例如：

```yaml
environment:
  SNS_MEDIA_TRUSTED_PROXY_CIDRS: '["10.0.0.0/8", "192.168.10.0/24"]'
```

proxy 必須覆寫 forwarded client header，而不是附加內容。不要信任任意 Internet client 或範圍過大的 public CIDR。

## 匿名平台限制

僅支援單篇 Instagram `/p/`、`/reel/`、精確單則 `/stories/<username>/<numeric-media-id>/` URL 與 X status URL。Instagram Story 的匿名擷取是 best effort，未配置 Cookie 時常因 login-required、過期、刪除或不可見而回傳 `story_unavailable`。帳號範圍 Stories URL、Stories tray 與 Highlights 不支援；配置 Cookie 後也只處理精確 URL 且該帳號可見的內容。本服務不會接受平台 Cookie 或 credentials 由使用者 request 傳入。媒體必須有 direct progressive file；系統不會合併 adaptive HLS/DASH stream。

## 故障排除

- `docker compose ps` 顯示 unhealthy：檢查 `docker compose logs --no-log-prefix app` 並查詢 `/healthz`；不要啟用 token path access log。
- `extraction_failed` 或 `post_unavailable`：確認 URL 是公開單篇貼文，並檢查平台是否允許匿名存取。不要加入 Cookie 或 credentials。
- `platform_authentication_failed`（503）：只在 extractor 回報明確的 session failure diagnostic 時使用，例如 session `invalid/expired`，或 redirect 至 login、challenge、consent page。operator 應先停止 authenticated smoke，在平台撤銷舊 session 並輪替 Cookie，再由新 extractor process 驗證。
- `story_unavailable`（404）：configured Story 遇到一般 `AuthRequired` 或 HTTP 401/403/404 時仍可能使用此結果，因系統無法可靠區分 session 有效但不可見與 Story 過期、刪除或其他 availability 問題。請先確認精確 URL 當下有效且配置帳號可見；不要只憑此結果判定 Cookie 已失效。
- 上述兩種情況都不會進行 anonymous retry，公開 response 也不暴露 operator session 狀態細節。排查時只依穩定 code/status 與受控帳號內的驗證操作，不要把 Cookie value、URL 或 upstream diagnostic 貼到 log 或 command line。
- `upstream_rate_limited`：降低 operator concurrency，並等待平台限制解除。
- `local_rate_limited`：設定的 process-wide 或 per-client slot 已被占用；請依 response 的 `Retry-After` 間隔重試。
- `token_not_found` 或 `token_expired`：service 已重新啟動或 10 分鐘 token TTL 已過；請重新分析原始貼文。
- `capacity_exceeded`：等待 token 到期；若要提高 bounded token capacity，必須先審查 memory limit。
- `unsafe_destination` 或 `upstream_media_invalid`：不要略過 host、DNS、MIME、redirect 或 byte-limit check；應改為審查 pinned extractor contract。
- 預覽顯示 fallback：確認 CDN poster 是否仍可匿名存取；若進入 generated mode，檢查 input/output/time limit、FFmpeg container dependency 與 `upstream_media_invalid`，不要轉送 Cookie 或放寬 FFmpeg network protocol。

## 自動化檢查

請在 repository root 執行 deployment checks：

```bash
uv run python scripts/container_smoke.py
docker compose config --quiet
```

smoke command 會建置 image、等待 health、驗證 UID 10001 與 read-only root filesystem、確認 `/tmp` 在 restart 後不保留資料、確認不存在 application media directory，並驗證 10 秒 graceful stop。

## Owner-controlled manual smoke tests

匿名 smoke test 只能使用 service owner 有權測試的公開貼文。若啟用平台驗證，另外使用 service owner 有權測試、且由配置帳號可見的 account-visible single posts；不要在 command line 放置 credentials 或 Cookie value：

```bash
uv run python scripts/manual_smoke.py \
  --instagram-image 'https://www.instagram.com/p/OWNER_CONTROLLED_IMAGE/' \
  --instagram-reel 'https://www.instagram.com/reel/OWNER_CONTROLLED_REEL/' \
  --instagram-mixed 'https://www.instagram.com/p/OWNER_CONTROLLED_MIXED/' \
  --x-image 'https://x.com/owner/status/OWNER_CONTROLLED_IMAGE' \
  --x-video 'https://x.com/owner/status/OWNER_CONTROLLED_VIDEO' \
  --x-gif 'https://x.com/owner/status/OWNER_CONTROLLED_GIF'
```

Story smoke 必須使用 owner-controlled 的精確單則 Story URL，且執行時仍當下有效。Story 最多約 24 小時即失效，所以此案例是選用的 ephemeral check，不得成為 CI 或 release gate，也不得將 URL 寫入 repository、CI 設定或 artifact、log、shell command history；不要以 shell command 參數傳入，避免 history 與 process listing 暴露。

需要執行選用 Story 案例時，在 repository 之外的 `/tmp` 建立 owner-only 暫存檔。下列 subshell 以互動式 shell builtin 讀取 URL，不會把值寫入 shell command history；URL 寫入後立即從 shell variable 移除，command line 只會帶檔案路徑。`trap` 會在正常完成或 shutdown/interruption 後執行 `rm -f`，不得保留檔案供之後重用：

```bash
(
  story_url_file="$(mktemp /tmp/sns-media-list-story.XXXXXX)"
  chmod 600 "$story_url_file"
  trap 'rm -f -- "$story_url_file"' EXIT
  trap 'exit 130' HUP INT TERM
  IFS= read -r -s -p 'Owner-controlled exact Story URL: ' story_url
  printf '\n'
  printf '%s\n' "$story_url" >"$story_url_file"
  unset story_url

  uv run python scripts/manual_smoke.py \
    --instagram-image 'https://www.instagram.com/p/OWNER_CONTROLLED_IMAGE/' \
    --instagram-reel 'https://www.instagram.com/reel/OWNER_CONTROLLED_REEL/' \
    --instagram-mixed 'https://www.instagram.com/p/OWNER_CONTROLLED_MIXED/' \
    --x-image 'https://x.com/owner/status/OWNER_CONTROLLED_IMAGE' \
    --x-video 'https://x.com/owner/status/OWNER_CONTROLLED_VIDEO' \
    --x-gif 'https://x.com/owner/status/OWNER_CONTROLLED_GIF' \
    --instagram-story-file "$story_url_file"
)
```

script 的所有案例都只能記錄 case label、status、item count 與 outcome，不得記錄 URL、token、Cookie 或 upstream media URL。script 會檢查每個 case 是否回傳 application-owned response、保留 media order、僅公開 opaque application URL，並提供可下載媒體。執行前必須將六個 placeholder URL 替換為真正的 owner-controlled 公開貼文；不需要 Story 時省略 `--instagram-story-file`，既有六個 required cases 保持不變。

Authenticated smoke test 應在對應 Compose override 啟用後執行，並確認 extraction、CDN raster preview、generated fallback preview 與 download 均成功。缺少 poster 的項目應只在首次載入生成一次，後續請求命中 bounded cache。下載與 generated preview 階段不得攜帶 Cookie；若 CDN 需要登入 Cookie，系統必須 fail closed 為 `upstream_media_invalid`，不可改用平台 session 轉送。Cookie 輪替後依「平台 Cookie 驗證」章節確認新 extractor process 已使用新檔，再以新的 account-visible URL 驗證。
