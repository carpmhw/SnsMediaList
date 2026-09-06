# SNS Media List 操作指南

## 部署

需求：Docker Engine、Docker Compose v2、可解析 outbound HTTPS 目的地的 DNS，以及私人或可信任的 operator 環境。本服務不是公開匿名 proxy。

```bash
docker compose build --pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8000/healthz
```

Compose 使用單一 worker 與 UID 10001，並啟用 read-only root filesystem、bounded tmpfs、`no-new-privileges`、drop all capabilities 及 PID limit。不掛載持久 media 或 token volume，預設只綁定 `127.0.0.1:${SNS_MEDIA_HOST_PORT:-8000}:8000`，Uvicorn access log 也已停用。遠端存取前必須配置 authenticated 或 network-ACL-restricted reverse proxy。

主要限制如下；請以 deployment-specific Compose override 或 environment file 覆寫：

| 設定 | 預設值 |
| --- | ---: |
| `SNS_MEDIA_TOKEN_TTL_SECONDS` | 600 |
| `SNS_MEDIA_TOKEN_CAPACITY` | 200 |
| `SNS_MEDIA_EXTRACTION_TIMEOUT_SECONDS` | 45 |
| `SNS_MEDIA_EXTRACTION_OUTPUT_LIMIT` | 2000000 bytes |
| `SNS_MEDIA_EXTRACTION_BODY_LIMIT_BYTES` | 4096 bytes |
| `SNS_MEDIA_MAX_DOWNLOAD_BYTES` | 500000000 bytes |
| `SNS_MEDIA_CONNECT_TIMEOUT_SECONDS` | 10 seconds |
| `SNS_MEDIA_READ_TIMEOUT_SECONDS` | 30 seconds |
| `SNS_MEDIA_MEDIA_RESPONSE_TIMEOUT_SECONDS` | 120 seconds |
| `SNS_MEDIA_MAX_REDIRECTS` | 3 |
| `SNS_MEDIA_MAX_EXTRACTIONS` | 1 |
| `SNS_MEDIA_MAX_DOWNLOADS` | 4 |
| `SNS_MEDIA_MAX_DOWNLOADS_PER_CLIENT` | 2 |
| `SNS_MEDIA_RATE_LIMIT_WINDOW_SECONDS` | 60 seconds |
| `SNS_MEDIA_RATE_LIMIT_EXTRACTION_ATTEMPTS` | 10 |
| `SNS_MEDIA_RATE_LIMIT_MEDIA_ATTEMPTS` | 120 |
| `SNS_MEDIA_RATE_LIMIT_IDENTITY_CAPACITY` | 2048 |
| `SNS_MEDIA_GENERATED_PREVIEWS_ENABLED` | false |
| `SNS_MEDIA_THUMBNAIL_INPUT_BYTES` | 32000000 bytes |
| `SNS_MEDIA_THUMBNAIL_OUTPUT_BYTES` | 1000000 bytes |
| `SNS_MEDIA_THUMBNAIL_TIMEOUT_SECONDS` | 10 seconds |
| `SNS_MEDIA_THUMBNAIL_CONCURRENCY` | 1 |
| `SNS_MEDIA_THUMBNAIL_CACHE_BYTES` | 32000000 bytes |
| `SNS_MEDIA_THUMBNAIL_MAX_EDGE` | 640 pixels |

## 安全不變條件

- 不得將 Cookie value、credentials、extractor config、proxy credentials、token、request body 或完整 upstream media URL 放入 environment、command line 或 log。
- 平台 Cookie 只能透過下方 read-only file mount 提供；服務仍須維持 loopback binding 或受信任 ingress。
- Application 與 reverse proxy 都不得記錄 token path；不得為排錯而略過 host、DNS、MIME、redirect 或 byte-limit check。

## 批次、下載與 timeout

batch UI 不會改變 `SNS_MEDIA_MAX_EXTRACTIONS`；最多五個 URL 由瀏覽器循序呼叫單筆 API，不是 server job queue。媒體多選仍受現有 download limits 與 rate limit 約束，不得因 UI 批次功能任意提高 concurrency。

| 設定 | 語意 |
| --- | --- |
| `SNS_MEDIA_CONNECT_TIMEOUT_SECONDS` | 上游連線建立與 request write 的期限。 |
| `SNS_MEDIA_READ_TIMEOUT_SECONDS` | Response headers 與每次 body read 的 idle timeout；成功 read 後重新計時，不是整檔期限。 |
| `SNS_MEDIA_MEDIA_RESPONSE_TIMEOUT_SECONDS` | CDN preview 與 generated thumbnail 的完整回應期限，不套用於 attachment download。 |
| `SNS_MEDIA_MAX_DOWNLOAD_BYTES` | 單檔硬限制；先檢查可信 `Content-Length`，未知長度則串流累計，超限即中止。 |

長時間下載會持續占用 process-wide 與 per-client slot；read idle timeout、client disconnect 或取消都會關閉 upstream connection 並釋放 lease。`SNS_MEDIA_DOWNLOAD_TIMEOUT_SECONDS` 已移除，舊環境中的同名變數會被忽略，升級時應刪除。

瀏覽器可能要求多檔下載權限；「已開始下載」不代表瀏覽器或 OS 已完成檔案保存，必要時請在 client 端確認實際檔案。

## 安全下載診斷

預設 application logger 以 INFO 等級將每筆事件寫成一行 JSON 至 stderr，Docker 可直接收集，不需要額外 handler 或 debug 設定。重複初始化不會增加輸出，也不修改 root logger 或向 root propagation。Uvicorn access log 必須維持停用；**不得為排錯開啟可能包含 token path 的 application、Uvicorn 或 reverse proxy access log**。

下載事件為 `media_download_started`，以及唯一的 `media_download_completed`、`media_download_failed` 或 `media_download_aborted` terminal event。X 影片符合既有條件時另有 `media_download_resume_attempted`、`media_download_resume_succeeded` 或 `media_download_resume_failed`，`resume_attempt` 固定為 1。Instagram 不增加 Range resume 或伺服器重試；瀏覽器本身可能重新發出 GET，每次 GET 都有不同 request ID。

安全欄位包含 `event`、`request_id`、`platform`、`media_class`、`outcome`、`duration_ms`、`bytes_streamed`，失敗時另有 `reason_code`。既有 `extraction_complete` 事件仍可輸出 `item_count`。任意 extra、完整 URL、query、token、Cookie、Authorization、raw ETag 與原始 transport exception 均不屬於輸出內容。

| Reason Code | 意義與檢查方向 |
| --- | --- |
| `idle_timeout` | 上游讀取或下游 send 閒置期限到期；需比對 client 與 ingress，不能僅由此判定 CDN 根因。 |
| `size_limit` | 超過既有 byte limit；不得為重現直接放寬限制。 |
| `client_disconnect` | Client disconnect 或取消，terminal outcome 為 `aborted`。 |
| `upstream_truncation` | 媒體提前結束且未恢復；Instagram 不續傳。 |
| `upstream_validation` | 狀態、MIME、媒體內容或 resume response 等驗證失敗。 |
| `unexpected_upstream_failure` | 未落入上述分類的讀取、送出或 cleanup 錯誤；不輸出 raw exception。 |

`started` 是下載流程開始，不保證 HTTP headers 已送出。`bytes_streamed` 是串流流程交付並恢復迭代後累計的媒體 bytes，不是瀏覽器落盤 bytes，也無法精確計算失敗 send 已傳出的部分資料。`completed` 只在最後 body 與 cleanup 均成功後記錄。最後 body 成功後若 cleanup 才失敗，仍記錄 failed，但不能撤回瀏覽器已收到的檔案。

Headers 已開始而 body 未完成時，服務用固定的 `StreamAborted: Media stream aborted.` 訊號讓 Uvicorn 關閉連線，不補送最後 body、不移除可信 Content-Length，也不建立第二份 error response。Uvicorn 可能輸出安全 traceback；這不代表應停用所有錯誤日誌。`ASGI callable returned without completing response` 是舊版正常返回未完成回應的症狀，本身不能證明 Cookie、CDN 或 timeout 是原始 Story 失敗原因。

### Operator 診斷清單

本清單僅供**另經授權部署後**操作，不授權建置、推送、重啟或部署，也不表示原始 Story 已修復。

1. 確認執行版本與授權範圍、access log 關閉、ingress authentication/ACL 及媒體 `proxy_buffering off`。部署重啟會使舊 token 失效，需重新解析。
2. 在有權使用的帳號確認原 Story 仍有效且可存取。若已過期、不可見或外部服務不可用，記錄「無法重現」與安全原因，不繞過限制，也不改用 deterministic fixture 宣稱已重現原事件。
3. 以既有 UI 重新分析並立即下載；記錄測試時間、瀏覽器版本、直連或受控 proxy、儲存確認時機，以及瀏覽器最終成功或失敗。不要匯出含 token、Cookie 或 URL 的 HAR、request dump 或截圖。
4. 使用 `docker compose logs --no-log-prefix --since 10m app` 在受限終端查看事件；分享前只保留上述安全 JSON 欄位。以 request ID 串接 started 與唯一 terminal event，若瀏覽器重試則分開記錄每次 GET；不要分享 token-bearing request path。
5. 成功案例在 client 核對檔名、實際大小與可讀性；失敗案例記錄 `reason_code`、`duration_ms`、`bytes_streamed` 與瀏覽器結果。只有 started 或 download event，不足以判斷落盤完成。缺少 terminal event 時另查程序終止、日誌收集或輸出故障，不假定傳輸成功。
6. 若直連正常而 ingress 失敗，按既有安全設定比對 buffering、idle timeout 與連線中止；不要開 access log 或任意調高 timeout。只有取得原始失敗的去敏證據後，才另提上游傳輸修正。

Deterministic HTTP 與 Chromium 驗證、舊行為重現及限制記錄於 `openspec/changes/diagnose-download-stream-failures/verification.md`；這些驗證不接觸 Instagram 或 operator Cookie。

## 平台 Cookie 驗證

平台 Cookie 是 bearer credential。配置 Instagram Cookie 後，任何服務使用者都可能間接使用 operator Instagram 帳號的 session，讀取該帳號可見的私人、Close Friends 或受眾限定 Story。服務沒有 per-user authorization，只適合本人管理的可信網路；請使用低權限專用帳號。

將 Netscape `cookies.txt` 放在受限目錄，確認 container UID 10001 可讀，再使用對應 override：

```bash
# Instagram
SNS_MEDIA_INSTAGRAM_COOKIE_HOST_FILE=/srv/secrets/instagram.cookies.txt \
  docker compose -f docker-compose.yaml -f docker-compose.instagram-auth.yaml up -d --build

# X
SNS_MEDIA_X_COOKIE_HOST_FILE=/srv/secrets/x.cookies.txt \
  docker compose -f docker-compose.yaml -f docker-compose.x-auth.yaml up -d --build
```

部署前可將 `up -d --build` 改成 `config --quiet` 驗證 Compose。Override 只掛載 read-only file，並停用 Cookie 更新；不使用 override 即為匿名模式。

Application 不快取 Cookie。覆寫相同 host file 並保留 inode 後，新的 extractor process 會立即重新讀取；若以 rename 更換 inode，必須重建 container。輪替時先在平台撤銷舊 session，進行中的 process 不會熱重載。

短效 token 不含 Cookie，只會在 TTL 到期或 service 重新啟動時失效。CDN preview 與 download 不會攜帶 Cookie；若 CDN 需要 session，系統會 fail closed。同一 UID 的惡意 extractor 理論上可讀取另一個已掛載的平台 Cookie，因此目前不是 per-platform sandbox。

回復匿名模式時，先移除所有已啟用的 auth override，再啟動預設 Compose：

```bash
docker compose -f docker-compose.yaml -f docker-compose.instagram-auth.yaml down
docker compose -f docker-compose.yaml up -d --build
```

若啟用 X 或同時啟用兩個平台，`down` 時須包含所有使用中的 override。Rollback 不會恢復舊 token 或 extraction state。

## 升級與 rollback

1. 審查 pinned dependency 與 `uv.lock` diff。
2. 執行 `uv run python scripts/verify_gallery_contract.py`。
3. 執行 `uv run python scripts/container_smoke.py`。
4. 建置 candidate image，完成 health check、security gate 與 owner-controlled smoke tests。
5. 在 deployment override 將 `image:` 指向不可變 candidate tag，再執行 `docker compose up -d --no-deps app`。

Rollback 時，先把 deployment override 的 `image:` 恢復為前一個不可變 tag，再執行：

```bash
docker compose stop -t 10 app
docker compose up -d --no-deps app
```

若以 checkout 部署，須先 checkout 前一版本並重新 build。每次替換或重新啟動 container 都會刻意清除 token 與 extraction state，使用者必須重新分析貼文。

## 預覽與縮圖

預覽優先使用 gallery-dl metadata 或受支援的 CDN raster。沒有可信 preview 時預設顯示 `/placeholder.svg`；只有 candidate image 通過 decoder vulnerability policy，或已有期限的 narrow risk acceptance，才可設定 `SNS_MEDIA_GENERATED_PREVIEWS_ENABLED=true`。此模式按需透過受限 FFmpeg 產生 JPEG，只保存在 process-local bounded cache，最長不超過 token TTL。

生成流程最多讀取 32 MB、輸出 1 MB、執行 10 秒且同時只執行一項。調高限制前須重新審查 768 MB memory、1 CPU、64 MB `/tmp` 與 decoder findings；FFmpeg 不得自行連線、讀取 Cookie 或寫入持久媒體。

## Reverse proxy logging

不得記錄 token-bearing application path。請直接使用並檢查 repository 的 `deploy/nginx/sns-media-list.conf`，不要在本文件維護第二份設定：

```bash
uv run python scripts/check_nginx_config.py
```

部署設定必須提供 authentication 或 restrictive network ACL、全站 `access_log off`、媒體路由 `proxy_buffering off`，並清除 forwarded credentials。`/api/media/` 的 `proxy_read_timeout` 必須不短於 `SNS_MEDIA_READ_TIMEOUT_SECONDS`；這是 read idle boundary，不是整檔期限。啟用前須建立 deployment-owned `.htpasswd`。

## Trusted proxy

預設使用 socket peer address，忽略 `Forwarded` 與 `X-Forwarded-For`。只有 service 僅能經受控 proxy 存取時，才能設定：

```yaml
environment:
  SNS_MEDIA_TRUSTED_PROXY_CIDRS: '["10.0.0.0/8", "192.168.10.0/24"]'
```

Proxy 必須覆寫 forwarded client header，不可附加內容；不要信任任意 Internet client 或過大的 public CIDR。

## 匿名平台限制

僅支援 Instagram `/p/`、`/reel/`、精確 `/stories/<username>/<numeric-media-id>/` 與 X status URL。Story 匿名擷取是 best effort；帳號全部 Stories、Stories tray 與 Highlights 不支援。配置 Cookie 後仍只處理精確 URL 及帳號可見內容。本服務不會接受平台 Cookie 或 credentials 由 request 傳入，也不合併 HLS/DASH stream。

## 故障排除

- Service unhealthy：執行 `docker compose logs --no-log-prefix app` 並查詢 `/healthz`。
- `[FATAL tini] exec uvicorn failed`：重建 image，再以 `docker compose up -d --force-recreate` 啟動。
- `extraction_failed` 或 `post_unavailable`：確認 URL 受支援，並確認平台允許目前模式存取。
- `platform_authentication_failed`（503）：僅在明確 session failure diagnostic 使用，例如 session `invalid/expired`，或 redirect 至 login、challenge、consent page；撤銷並輪替 Cookie 後再驗證。
- `story_unavailable`（404）：configured Story 遇到 `AuthRequired` 或 HTTP 401/403/404 時仍可能回傳此結果；系統無法可靠區分 session 有效但不可見與 Story availability 問題，請確認精確 URL 與帳號可見性。
- 上述兩種情況都不會 anonymous retry，公開 response 也不暴露 operator session 狀態細節。
- `upstream_rate_limited`：降低 operator concurrency 並等待平台解除限制。
- `local_rate_limited`：slot 已滿，依 `Retry-After` 重試。
- `token_not_found` 或 `token_expired`：token 到期或 service 已重新啟動，請重新分析。
- `capacity_exceeded`：等待 token 到期；提高容量前須審查 memory limit。
- `unsafe_destination` 或 `upstream_media_invalid`：審查 pinned extractor contract，不要繞過安全檢查。
- 預覽 fallback：檢查 CDN poster、generated preview 限制、FFmpeg dependency 與 `upstream_media_invalid`。

## 自動化檢查

在 repository root 執行：

```bash
uv run python scripts/container_smoke.py
uv run python scripts/check_nginx_config.py
docker compose config --quiet
uv run python scripts/security_gate.py --image sns-media-list:candidate
```

`security_gate.py` 會稽核 production dependency graph 與 candidate image。未修復 finding 只能透過 `security/vulnerability-exceptions.json` 記錄 exact CVE/package、版本、artifact digest、owner、理由、mitigation 與 expiry；不匹配、wildcard 或過期例外都會失敗。Decoder policy 未通過時，generated preview 必須保持停用。

`container_smoke.py` 會驗證 health、UID 10001、read-only root、loopback port、capabilities、`no-new-privileges`、PID limit、tmpfs、不持久保存 media，以及 10 秒 graceful stop。

## Owner-controlled manual smoke tests

Live smoke 只能使用 owner-controlled 公開貼文；authenticated smoke 則使用配置帳號可見且有權測試的內容。Repository 未提供安全的 URL 或 Cookie，因此這些案例是 deployment-specific、non-gating manual release checks：

```bash
uv run python scripts/manual_smoke.py \
  --instagram-image 'https://www.instagram.com/p/OWNER_CONTROLLED_IMAGE/' \
  --instagram-reel 'https://www.instagram.com/reel/OWNER_CONTROLLED_REEL/' \
  --instagram-mixed 'https://www.instagram.com/p/OWNER_CONTROLLED_MIXED/' \
  --x-image 'https://x.com/owner/status/OWNER_CONTROLLED_IMAGE' \
  --x-video 'https://x.com/owner/status/OWNER_CONTROLLED_VIDEO' \
  --x-gif 'https://x.com/owner/status/OWNER_CONTROLLED_GIF'
```

owner-controlled Story URL 必須當下有效，通常約 24 小時內失效。Story 是選用 ephemeral check，不得成為 CI 或 release gate，也不得把 URL 寫入 repository、CI、artifact、log 或 shell command history。請在 repository 之外建立 owner-only 暫存檔：

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

Script 只能記錄 case label、status、item count 與 outcome，不得記錄 URL、token、Cookie 或 upstream media URL。執行前須替換六個 placeholder；不測 Story 時省略 `--instagram-story-file`。

Authenticated smoke 應驗證 extraction、CDN raster preview、generated fallback preview 與 download。Preview 與 download 不得攜帶 Cookie；需要登入的 CDN 必須 fail closed。Cookie 輪替後應先確認新的 extractor process 已讀取新檔，再測試新的 account-visible URL。
