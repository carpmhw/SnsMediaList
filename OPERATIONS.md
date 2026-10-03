# SNS Media List 操作指南

所有指令在 repository root 執行。版本與映像 pin 以 `pyproject.toml`、`uv.lock`、`Dockerfile` 為準；功能範圍及開發入口見 [README](README.md)。

[部署](#部署) · [Cookie](#平台-cookie-驗證) · [升級／回復](#升級與-rollback) · [下載診斷](#安全下載診斷) · [Story 診斷](#instagram-story-extraction-diagnostics) · [人工驗證](#owner-controlled-manual-smoke-tests)

## 部署

需求：Docker Engine、Docker Compose v2、可解析 outbound HTTPS 目的地的 DNS，以及私人或可信任的 operator 環境。本服務不是公開匿名 proxy。

```bash
docker compose build --pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8000/healthz
```

Compose 使用單一 worker、UID/GID 10001、read-only root filesystem、bounded tmpfs、`no-new-privileges`、drop all capabilities 與 PID limit。不掛載持久 media／token volume；預設綁定 `127.0.0.1:${SNS_MEDIA_HOST_PORT:-8000}:8000`，並停用 Uvicorn access log。遠端存取須配置驗證或 network ACL。

既有部署先依 [Compose labels 確認實際服務](#instagram-extractor-compatibility)；後續指令沿用相同 `-p <project>`、`-f` 檔案與 Cookie overlays，避免啟動第二個 project。

### 設定

以下為預設值；完整欄位及合法範圍見 `src/sns_media_list/config.py`。Compose 中明列的 `environment` 請用 deployment override 覆寫，單改 shell／`.env` 不會取代這些固定值；`SNS_MEDIA_HOST_PORT`、`SNS_MEDIA_IMAGE_TAG` 等 `${…}` 插值才由 shell／`.env` 提供。

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
| `SNS_MEDIA_MCP_ENABLED` | false |
| `SNS_MEDIA_MCP_MAX_REQUEST_BODY_BYTES` | 65536 bytes |
| `SNS_MEDIA_MCP_ALLOWED_HOSTS` | `[]`，使用 SDK localhost defaults |
| `SNS_MEDIA_MCP_ALLOWED_ORIGINS` | `[]`，使用 SDK localhost defaults |
| `SNS_MEDIA_THUMBNAIL_INPUT_BYTES` | 32000000 bytes |
| `SNS_MEDIA_THUMBNAIL_OUTPUT_BYTES` | 1000000 bytes |
| `SNS_MEDIA_THUMBNAIL_TIMEOUT_SECONDS` | 10 seconds |
| `SNS_MEDIA_THUMBNAIL_CONCURRENCY` | 1 |
| `SNS_MEDIA_THUMBNAIL_CACHE_BYTES` | 32000000 bytes |
| `SNS_MEDIA_THUMBNAIL_MAX_EDGE` | 640 pixels |

## MCP deployment

MCP 預設關閉，啟用時在同一 port 提供 `/mcp` **Streamable HTTP** 與單一 `extract_media`。沿用 loopback host publish、單 worker、唯讀 filesystem 與既有 CONNECT proxy／Cookie 邊界；`/healthz` 始終只表示 liveness，不建立 MCP session 或連線平台。

### 啟用與協定 smoke

```bash
docker compose -f docker-compose.yaml -f docker-compose.mcp.yaml config --quiet
docker compose -f docker-compose.yaml -f docker-compose.mcp.yaml up -d --build
uv run python scripts/mcp_smoke.py http://127.0.0.1:8000/mcp
uv run python scripts/mcp_smoke.py http://127.0.0.1:8000/mcp --mode legacy
```

已有部署先確認 Compose labels，沿用**相同 project**、原 `-p` 與完整 Cookie／image overlays；啟用 overlay 只改 environment，不新增 port。Base Compose 的 `SNS_MEDIA_MCP_ENABLED` 為固定 false，單改 shell／`.env` 不會啟用 MCP。本機 Uvicorn 可用 `SNS_MEDIA_MCP_ENABLED=true`，仍須 `--workers 1 --no-access-log`。

Smoke 預設只驗證連線、SDK 所需的 initialize／version negotiation、server 名稱與唯一 tool schema，不呼叫 Instagram／X。現代協定可能沒有 session ID，不能據此判定失敗。選用 live extraction 使用 `--url-file <暫存 URL 檔>`，僅限 owner-controlled 內容；不把真實 URL 放入 repository、CI、log 或 shell command history，完成後移除暫存檔。輸出不回顯 URL、token 或結果全文，live smoke 不作為 release gate。

### 設定與 request budget

- `SNS_MEDIA_MCP_ENABLED=false`：不建立 SDK server／manager，`/mcp` 404。
- `SNS_MEDIA_MCP_MAX_REQUEST_BODY_BYTES=65536`：合法範圍 1–1048576 bytes；僅接受未壓縮 JSON。Declared 與 streamed bytes 都在 SDK parsing 前受限；超限 413，representation 不支援 415。
- `SNS_MEDIA_MCP_ALLOWED_HOSTS`／`SNS_MEDIA_MCP_ALLOWED_ORIGINS`：JSON array，各最多 **32** 筆 exact allowlist。自訂值不允許 wildcard、port glob、credentials、path、query 或 fragment；Origin 必須是完整 HTTP(S) origin。
- 兩組未自訂時保留 SDK localhost defaults；自訂 hosts 卻不提供 origins 時，所有帶 Origin 的 request 被拒絕，非瀏覽器 Client 仍須匹配 Host。
- 自訂 origins 必須同時配置 hosts；單獨 Origin allowlist 在 Settings validation 拒絕，避免啟動後全部 Host 都被 421 拒絕。
- 所有 MCP Client 共用 `mcp` identity，預設同時最多一個 MCP extraction；REST／MCP 共用 `SNS_MEDIA_MAX_EXTRACTIONS`，不建立第二組 slot 或 queue。
- 每 60 秒最多 **10 個 MCP POST**（沿用 `SNS_MEDIA_RATE_LIMIT_EXTRACTION_ATTEMPTS`／window），其中 9 個一般 POST、1 個只保留給同一 session 中 active `extract_media` 的 legacy cancellation。initialize、notifications、list-tools、tool calls 與 malformed POST 消耗一般額度；不匹配的 cancellation 也算一般 POST，每個 HTTP POST 只計一次。一般額度用盡時，boundary 仍讀取一個有界 body 以辨識匹配 cancellation；其他 request 回傳 HTTP 429／`local_rate_limited`／Retry-After。REST／media identity churn 不會重置固定 MCP bucket；GET／DELETE 不消耗這個 POST budget。
- MCP 啟用時 extraction attempt limit 至少 2；過低的 quota 可能不足以完成 legacy handshake，建議維持預設值。固定 MCP bucket 與 client identity LRU 分離，總 retained state 上限為 identity capacity 加一個固定大小 bucket。
- SDK legacy session 固定最多 32 個，閒置期限沿用 token TTL；沒有持久 session store。Transport sessionless 不表示 token／limiters 能跨 worker，共享狀態仍只在單 process。

### Reverse proxy、Host／Origin 與驗證

`deploy/nginx/sns-media-list.conf` 的 exact `/mcp` location 繼承原 authentication、停用 access log、移除 credentials，並提供 64 KiB body、無 buffering 的 Streamable HTTP 與 300 秒 read timeout；REST 的 4 KiB body 與媒體串流設定維持獨立。調整 app body limit／extraction timeout 時須同步 proxy 的 body／timeout 邊界。

範例 proxy 以 `$host` 轉送 Host（不含 port），必須為該精確值配置 app allowlist；Origin 使用 Client 實際發送的 scheme／host／port。以下是示意 deployment override，替換成自己的 ingress 值並沿用原部署 overlays：

```yaml
services:
  app:
    environment:
      SNS_MEDIA_MCP_ALLOWED_HOSTS: '["sns-media.example.internal"]'
      SNS_MEDIA_MCP_ALLOWED_ORIGINS: '["https://sns-media.example.internal"]'
```

SDK DNS-rebinding protection 保持啟用；不匹配 Host 為 **421**，不匹配 Origin 為 **403**。不能以關閉防護、任意 `X-Forwarded-For` 或 wildcard 修正；檢查 proxy 真正轉送的值，不從 Host 推導 absolute media URL。

Host／Origin 防護不是身份驗證。MCP 只適合本人／可信網路或有 authentication／network ACL 的 ingress；若配置平台 Cookie，任何被允許的 MCP 使用者都可能間接使用 operator 帳號可見權限，沒有 per-user isolation。MCP 不提供 Cookie 上傳、OAuth 或憑證管理。

### 結果、錯誤與排錯

Tool 成功回傳 metadata 與 root-relative 相對 preview／download URL，Client 依明確的服務 origin 解析；TTL／purpose／capacity、HEAD／GET、預覽與到期 410 均沿用原 API。沒有 media binary、upstream URL、headers 或 raw output。

Tool failure 使用 SDK `isError=true`，文字 content 為固定 `{code,message}` JSON；沒有成功 structured content 或 internal diagnostics。Protocol／validation error 不回顯 arguments；每次 tool（包含 validation failure）都有 safe `mcp_extraction_started` 與唯一 complete／failed／aborted event，使用 server-generated request ID；既有 `mcp_server_started`／startup failure event 也僅含安全欄位。保留 `--no-access-log`，不以 debug／request dump 排錯。

上述 tool validation 指合法 RPC arguments object 內的 URL／額外欄位驗證；RPC envelope 本身無效（例如 arguments 是字串）由 SDK 回傳安全 `-32602` 等協定錯誤，未進入 tool lifecycle。取消 request ID 使用 SDK correlation 規則，數字字串與整數可相互匹配；仍須同一 session。既存 SDK logger／子 logger handlers 會被隔離，不輸出 raw SDK 訊息。

| 現象 | 檢查方向 |
| --- | --- |
| `/mcp` 404 | 確認 runtime image／有效 Compose 設定為 enabled，且不是 `/mcp/mcp`。 |
| 421／403 | 核對精確 Host／Origin 與 proxy 轉送；不放寬為 wildcard。 |
| HTTP 429 | 區分 aggregate POST window、保留 cancellation 額度與 extraction slot；handshake 也計入一般 POST budget，等待 Retry-After。 |
| 413／415 | 核對 app／proxy body limit 與未壓縮 application/json。 |
| 406 | Client Accept 必須支援 application/json 與 text/event-stream，建議使用官方 SDK Client。 |
| task-group-not-initialized | 核對 parent lifespan 是否管理 manager；不要只啟動 mounted sub-app。 |
| Client 取消後仍有工作 | 使用 SDK 預設 Streamable HTTP response 模式；legacy 必須使用同 session 的 active request ID，純 JSON 分支無現代 disconnect cancellation。 |
| 重啟後媒體／session 失效 | 記憶體狀態不恢復，Client 重新連線並重新分析。 |

### 關閉與 rollback

移除 MCP enable override，保留相同 project、其他必要的 Cookie／image overlays，再重新建立 app。例如最小部署：

```bash
docker compose -f docker-compose.yaml up -d --no-deps app
```

或在 deployment-owned override 明確設 `SNS_MEDIA_MCP_ENABLED: "false"`。確認 `/mcp` 回到 404、health／UI／REST 正常；重建會清除 token 與 SDK session。Image rollback 仍依下方不可變 reference 流程，不把另一個 smoke image 的結果套用到實際 candidate。

## 安全不變條件

- 不得將 Cookie value、credentials、extractor config、proxy credentials、token、request body 或完整 upstream media URL 放入 environment、command line 或 log。
- 平台 Cookie 只能透過下方 read-only file mount 提供；服務仍須維持 loopback binding 或受信任 ingress。
- 維持 Uvicorn `--no-access-log`；application 與 reverse proxy 都不得記錄 token path 或 raw extractor output，也不得為排錯略過 host、DNS、MIME、redirect 或 byte-limit check。

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

Application logger 預設以 INFO 將每筆事件寫成一行 JSON 至 stderr，Docker 可直接收集；不需開啟 debug 或 access log。沿用[擷取診斷的日誌查詢](#instagram-story-extraction-diagnostics)，以 `request_id` 串接事件。

下載事件為 `media_download_started`，以及唯一的 `media_download_completed`、`media_download_failed` 或 `media_download_aborted` terminal event。X 影片符合既有條件時另有 `media_download_resume_attempted`、`media_download_resume_succeeded` 或 `media_download_resume_failed`，`resume_attempt` 固定為 1。Instagram 不增加 Range resume 或伺服器重試；瀏覽器本身可能重新發出 GET，每次 GET 都有不同 request ID。

分享時只保留事件的安全欄位：`event`、`request_id`、`platform`、`media_class`、`outcome`、`duration_ms`、`bytes_streamed`、`reason_code`。不附 URL、token、Cookie、headers、raw ETag 或原始 exception。

| Reason Code | 意義與檢查方向 |
| --- | --- |
| `idle_timeout` | 上游讀取或下游 send 閒置期限到期；需比對 client 與 ingress，不能僅由此判定 CDN 根因。 |
| `size_limit` | 超過既有 byte limit；不得為重現直接放寬限制。 |
| `client_disconnect` | Client disconnect 或取消，terminal outcome 為 `aborted`。 |
| `upstream_truncation` | 媒體提前結束且未恢復；Instagram 不續傳。 |
| `upstream_validation` | 狀態、MIME、媒體內容或 resume response 等驗證失敗。 |
| `unexpected_upstream_failure` | 未落入上述分類的讀取、送出或 cleanup 錯誤；不輸出 raw exception。 |

`started` 不保證 headers 已送出；`bytes_streamed` 不是瀏覽器落盤 bytes。`completed` 僅在最後 body 與 cleanup 均成功後記錄；cleanup 失敗仍記為 failed，但不能撤回已送出的資料。

Headers 已送出但 body 未完成時，`StreamAborted: Media stream aborted.` 會中止連線，不補送成功結尾或第二份 error response。這類安全 traceback 表示傳輸中止，根因仍須看 terminal event。

### Operator 診斷清單

1. 確認執行版本、帳號可見性及內容仍有效，再由 UI 重新分析並下載；部署重啟後舊 token 已失效。
2. 記錄測試時間、瀏覽器版本、直連／proxy 與安全事件；瀏覽器重試的每次 GET 分別核對。不要匯出含敏感資料的 HAR 或 request dump。
3. 成功時核對本機檔名、大小與可讀性；失敗時比對 `reason_code` 與瀏覽器結果。缺少 terminal event 時查程序終止或日誌收集，不假定成功。
4. 若直連正常而 ingress 失敗，檢查 `proxy_buffering off`、idle timeout 與連線中止；內容失效則記錄「無法重現」。

## 平台 Cookie 驗證

平台 Cookie 是 bearer credential。任何服務使用者都可能間接使用 operator Instagram 帳號，讀取其可見的私人、Close Friends 或受眾限定 Story；服務沒有 per-user authorization，只適合可信網路與低權限專用帳號。

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

輪替時撤銷舊 session。Application 不快取 Cookie：保留 inode 覆寫 host file 後，新的 extractor process 會重新讀取；若用 rename 更換 inode，以原部署設定重新建立 container，確保掛載新檔。進行中的 process 不會熱重載。

短效 token 不含 Cookie；Cookie 輪替不會自動撤銷已發出的 token，須等待 TTL 到期或重新啟動 service。CDN preview／download 不會攜帶 Cookie，需要 session 的 CDN 會 fail closed；兩平台共用 UID，並非 per-platform sandbox。

回復匿名模式：以原 project 和全部使用中的 overlays 執行 `docker compose down`，再以相同 project、不含 auth overlays 的設定啟動；同時撤銷平台 session。舊 token 與記憶體狀態不會恢復。

## 升級與 rollback

1. 核對 source、lockfile 與目前 image 身分，保存可回復的 image ID／digest；可變 tag 不是不可變版本。
2. 依[自動化檢查](#自動化檢查)執行 contract 與隔離 smoke。使用唯一 tag 建置 candidate image，對該 candidate 核對 gallery-dl／FFmpeg runtime、`/healthz`、container limits 及 security gate；另一個 smoke image 通過不能代替 candidate 驗證。
3. Gate 通過後，在 deployment override 將 `image:` 指向已驗證的不可變映像 reference，再以原 project 與完整 overlays 執行 `docker compose up -d --no-deps app`。Live 平台檢查依[人工驗證](#owner-controlled-manual-smoke-tests)，不作為 CI／release gate。
4. Rollback：將 `image:` 恢復為先前的不可變 reference，再執行相同啟動命令。若需從 source 重建，須連同 lockfile 與映像 pin 核對，不能假設重建等同原 image。

每次替換或重新啟動 container 都會清除記憶體 token／狀態，使用者須重新分析貼文。部署紀錄保存 source、image ID／digest、gate 結果與 rollback reference，不把單次版本紀錄寫入通用指南。

## 預覽與縮圖

優先使用可信 CDN raster，缺少時顯示 `/placeholder.svg`。Generated preview 預設關閉；candidate 通過 decoder vulnerability policy 或具有效的 narrow risk acceptance 後，才可設定 `SNS_MEDIA_GENERATED_PREVIEWS_ENABLED=true`。

啟用後以受限 FFmpeg 按需產生 JPEG，只存於 process-local bounded cache，最長不超過 token TTL。輸入上限 32 MB、輸出 1 MB、時間 10 秒、併發 1；FFmpeg 不得自行連線、讀取 Cookie 或寫入持久媒體。

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

## Instagram Story Extraction Diagnostics

進入 extraction route 的失敗以唯一 `extraction_failed` 事件記錄。`reason_code` 對應公開錯誤，`failure_stage` 與下列 diagnostic 欄位僅供 server-side 排錯，不加入 API body、headers、OpenAPI 或前端。配置 Cookie 本身不表示 session 已驗證有效。

檢視最近十分鐘的 application structured events：

```bash
docker compose logs --no-log-prefix --since 10m app
```

排錯只分享安全 structured event，保留 `--no-access-log`；不傾印 raw stderr／stdout、Story URL、Cookie 或 exception chain。

| `reason_code` | HTTP | 說明與處置 |
| --- | ---: | --- |
| `story_auth_required` | 403 | 未配置 Cookie 的 Story 要求登入或回報 HTTP 401/403；依[Cookie 設定](#平台-cookie-驗證)提供唯讀 session。 |
| `platform_authentication_failed` | 503 | configured Story 回報 `AuthRequired`／`AuthenticationError` 或明確 `invalid/expired` session、login、challenge；檢查掛載及帳號狀態，不保證 Cookie 已過期。 |
| `story_unavailable` | 404 | `NotFoundError`／HTTP 404 或過期、刪除、private／不可見證據；系統不細分實際原因，請核對精確 URL 與帳號可見性。 |
| `upstream_rate_limited` | 429 | 降低請求頻率並等待平台解除限制。 |
| `extraction_timeout` | 504 | extractor 超過期限；查 network／CONNECT proxy，不直接提高 timeout。 |
| `extraction_failed` | 502 | 無法細分的失敗，包含僅有 5xx 或 configured Story 僅有 HTTP 401/403；不能據此判定 Cookie 或 Story 狀態。 |

### Failure stage

Stage 不改變公開錯誤；未提供時省略，無效值正規化為 `unknown`。

| `failure_stage` | 發生條件與排錯方向 |
| --- | --- |
| `extractor_start` | 程序無法啟動；查 executable 與 runtime。 |
| `extractor_timeout` | 總期限到期；查 DNS、network 與 CONNECT proxy。 |
| `extractor_output_limit` | stdout 超限；查 DataJob 大小，不放寬限制。 |
| `extractor_io` | Pipe／communicate I/O failure；查程序資源與 cleanup。 |
| `extractor_process_unclassified` | Stderr 或 DataJob error 無法細分；可包含零退出的 `HttpError`，依[相容性流程](#instagram-extractor-compatibility)排查。 |
| `extractor_invalid_output` | UTF-8／JSON／schema 無效，含零 bytes／純空白；執行 `uv run python scripts/verify_gallery_contract.py`。 |
| `extractor_empty_output` | 成功退出且為 literal `[]`；Story 回傳 `story_unavailable`，其他目標回傳 `extraction_failed`。 |
| `extractor_no_media` | 只有 directory／queue events；與 normalizer 過濾後、不帶 stage 的 `no_media` 不同。 |
| `extractor_platform_error` | 已命中驗證、可見性或限流分類；依上表處理。 |

### Extractor diagnostic 欄位

以下欄位只來自實際 extractor 證據；缺少時省略，未知 enum 為 `unknown`。非零退出取 stderr；零退出取第一筆合法 DataJob error，不混入後續 error 或 stderr。

| 欄位 | 值域與解讀 |
| --- | --- |
| `extractor_diagnostic_source` | `stderr`、`datajob_error` 或 `unknown`，表示證據來源。 |
| `extractor_error_type` | `auth_required`、`authentication_error`、`authorization_error`、`not_found`、`http_error`、`challenge_error`、`extraction_error`、`no_extractor` 或 `unknown`。Stderr 沒有結構化 type，固定為 `unknown`。 |
| `extractor_exit_code` | 自然完成程序的整數 `-255..255`；`0` 不代表擷取成功，負值表示 signal。零退出 invalid／empty／non-media 結果只附此欄位。 |
| `extractor_http_statuses` | 移除 URL 後從明確 HTTP／status 前綴或下列原因片語取得；100..599、升冪、去重，最多保留最小 8 個，JSON 為 array。 |

- Start、timeout、output limit、I/O、取消不記錄退出碼，避免誤用 cleanup 的 terminate／kill signal。
- 無前綴仍可辨識：`401 Unauthorized`、`403 Forbidden`、`404 Not Found`、`429 Too Many Requests`、`500 Internal Server Error`、`502 Bad Gateway`、`503 Service Unavailable`、`504 Gateway Timeout`；不區分大小寫，數字與片語須配對。
- 裸數字、Story ID、URL／query 不形成狀態證據；沒有狀態不代表沒有 upstream HTTP failure。上游 500 不等於本服務回傳的 HTTP 502，也不能證明 Cookie 狀態。
- HTTP 429 優先映射限流、HTTP 404 映射 Story availability；僅有 5xx 保留一般失敗分類，不推測平台內部原因。

合成事件範例：

```json
{"event":"extraction_failed","request_id":"0123456789abcdef0123456789abcdef","platform":"instagram","outcome":"failed","duration_ms":12.5,"reason_code":"extraction_failed","failure_stage":"extractor_process_unclassified","extractor_diagnostic_source":"datajob_error","extractor_error_type":"http_error","extractor_exit_code":0,"extractor_http_statuses":[500]}
```

## Instagram Extractor Compatibility

零退出的 DataJob error 仍可能是擷取失敗。依序排查，不先重建運作中的服務：

1. **核對 source pin 與 runtime。** 版本以 `pyproject.toml`／`uv.lock`、映像 pin 以 `Dockerfile` 為準。遇到 `service app is not running`，先用 Compose labels 找到真正的 project／service，再查容器與映像身分；以下 `<…>` 須換成實際值：

   ```bash
   docker ps --filter label=com.docker.compose.service=app --format '{{.ID}} {{.Names}} {{.Image}} {{.Label "com.docker.compose.project"}} {{.Label "com.docker.compose.service"}}'
   git rev-parse HEAD
   git status --short
   docker inspect --format '{{.Id}} {{.Image}} {{.Config.Image}}' '<container>'
   docker image inspect --format '{{.Id}} {{json .RepoDigests}}' '<image-id>'
   docker exec '<container>' gallery-dl --version
   docker logs --since 10m '<container>'
   ```

2. **驗證 contract。** 執行 `uv run python scripts/verify_gallery_contract.py`，核對已安裝套件、`--resolve-json`、離線 DataJob 格式及 adapter；不使用真實 Cookie 作 fixture。
3. **核對 Instagram Story 相容性。** 在 pinned `gallery-dl` 1.32.14 中，精確 Story 使用套件預設 video mode，不強制 `extractor.instagram.videos=merged`；Instagram Post／Reel 繼續使用 merged。若 Story URL event 是 `ytdl:` pseudo URL，adapter 僅在 validated Instagram Story context 中，將同一 URL media record 的 top-level 非空 `video_url` 作為 source 候選，再交由既有驗證流程處理。
4. **維持來源安全邊界。** `video_url` 搬移只是候選轉換，不是安全授權：既有 HTTPS URL 形狀檢查與 downstream host／port／DNS A／AAAA／IP／redirect／MIME／byte-limit 政策都必須通過。missing／unsafe source fail closed；不 unwrap `ytdl:`、不向其他 record／directory／queue／nested metadata 借 URL，也不新增 generic downloader 或 retry。
5. **查官方 release notes。** 比對觀察到的行為與候選版本修正，再決定是否升級，不以版本更新本身宣稱修復。
6. **驗證 candidate image。** 依[升級與 rollback](#升級與-rollback)及[自動化檢查](#自動化檢查)完成隔離驗證與 gate。
7. **最後才改 classifier。** 有安全診斷與合成 fixture 才評估窄範圍規則；不以泛用 `redirect`／`failed` 字串推論驗證失敗。

此相容性處理不表示所有 `KeyError: 'width'` 或 HTTP 403 都由相同原因造成；configured Story 模糊 403 仍依既有一般錯誤契約處理。

### Story GraphQL 靜態資源與 CONNECT proxy

Pinned gallery-dl 的 Story 流程會先取得使用者資訊與頁面，再從 `static.cdninstagram.com` 讀取靜態 JavaScript，以尋找 GraphQL `doc_id`。Extractor 的精確 host 白名單包含此單一網域；不授權整個 `*.cdninstagram.com`，仍須通過 HTTPS CONNECT、443 port、完整 DNS A／AAAA 公開 IP 檢查與目的地 pinning。此授權用於擷取期間的靜態資源，不改變 preview／download 的媒體政策或 Cookie 邊界。

若使用者查詢與頁面讀取皆為 HTTP 200，但靜態資源請求出現 `ProxyError`／CONNECT tunnel 403，應先核對執行中映像的 extractor 白名單。本機 CONNECT proxy 拒絕目的地時也會回覆 403，不能僅由此認定 Instagram 拒絕 session 或 Story GraphQL 請求。診斷只保留固定請求類別、狀態碼與是否攜帶 Cookie 的布林旗標，不記錄實際資源 URL、Cookie 值或原始例外。

原始碼白名單更新須經映像建置及部署替換才會生效；重新啟動舊映像不會取得修正。更新後以仍有效的 owner-controlled Story 確認流程可繼續到 GraphQL，再驗證分析與完整下載；解除靜態資源阻擋不代表後續平台請求必然成功。

Cookie 檔案存在、同步、格式／權限檢查通過、瀏覽器可見或一般貼文成功，都不能證明 Story API 的 session 有效。Deterministic fake-extractor／container 結果與 owner-controlled live Story 重試分開記錄；合成測試通過不代表原事件已修復。

## 故障排除

| 症狀 | 處置 |
| --- | --- |
| Service unhealthy／程序無法啟動 | 查實際容器日誌、`/healthz` 與 executable；先核對 image 身分再決定重建。 |
| 擷取失敗／`post_unavailable` | 核對 URL 與平台可見性；Story 依[診斷表](#instagram-story-extraction-diagnostics)。 |
| `local_rate_limited` | 依 `Retry-After` 等待 slot／限流窗口；勿直接提高併發。 |
| `token_not_found`／`token_expired` | 重新分析；token 可能到期或隨程序重啟清除。 |
| `capacity_exceeded` | 等待 token 到期；提高容量前審查 memory limit。 |
| `unsafe_destination`／`upstream_media_invalid` | 查 contract、目的地與媒體驗證，不繞過安全檢查。 |
| 預覽 fallback | 查 CDN poster、generated preview 設定及 FFmpeg；見[預覽與縮圖](#預覽與縮圖)。 |

每次 extraction 只嘗試一次，不會 anonymous retry；公開 response 不暴露 operator session 細節。

## 自動化檢查

依變更範圍執行；`<candidate-image>` 替換為已建置的唯一 candidate tag 或 image ID：

```bash
uv run python scripts/verify_gallery_contract.py
docker compose config --quiet
uv run python scripts/container_smoke.py
uv run python scripts/check_nginx_config.py
uv run python scripts/security_gate.py --image '<candidate-image>'
```

- `verify_gallery_contract.py` 為離線套件／adapter 驗證，不代表真實平台可用。
- `container_smoke.py` 需要 Docker daemon，使用獨立 project、動態 port、每次專用 image tag，驗證 health、執行限制、tmpfs、10 秒 graceful stop，並以假 Cookie／fake extractor 驗證 Story 日誌。不重標記共享映像或替換既有服務。
- `check_nginx_config.py` 使用本機 Nginx 或固定 digest 的 Docker 映像驗證語法。
- `security_gate.py` 稽核 production dependencies 與指定 candidate；HIGH／CRITICAL finding 依 gate policy 阻擋。例外只接受 `security/vulnerability-exceptions.json` 中精確 CVE/package、版本、artifact digest、owner、理由、mitigation、expiry；不匹配、wildcard 或過期均失敗。Decoder policy 未通過時 generated preview 保持停用。

程式品質與各層測試入口見 [AGENTS.md](AGENTS.md#7-測試與驗證)。

## Owner-controlled manual smoke tests

只使用 owner-controlled 公開貼文，或配置帳號可見且有權測試的內容；這是部署特定人工檢查，不是 CI／release gate。工具要求六種一般貼文案例；Story 為選用。

owner-controlled Story URL 須當下有效，通常約 24 小時內失效。不得寫入 repository、CI、artifact、log 或 shell command history。下例使用 Bash，先替換六個一般貼文 placeholder；Story 由隱藏輸入取得，在 repository 之外建立 owner-only 暫存檔並自動清除，直接 Enter 可略過：

```bash
(
  set +x
  story_args=()
  IFS= read -r -s -p '選用精確 Story URL（Enter 略過）：' story_url || exit 1
  printf '\n'
  if [ -n "$story_url" ]; then
    story_url_file="$(mktemp /tmp/sns-media-list-story.XXXXXX)" || exit 1
    trap 'rm -f -- "$story_url_file"' EXIT
    trap 'exit 130' HUP INT TERM
    chmod 600 "$story_url_file" || exit 1
    printf '%s\n' "$story_url" >"$story_url_file" || exit 1
    story_args=(--instagram-story-file "$story_url_file")
  fi
  unset story_url
  uv run python scripts/manual_smoke.py \
    --instagram-image 'https://www.instagram.com/p/OWNER_CONTROLLED_IMAGE/' \
    --instagram-reel 'https://www.instagram.com/reel/OWNER_CONTROLLED_REEL/' \
    --instagram-mixed 'https://www.instagram.com/p/OWNER_CONTROLLED_MIXED/' \
    --x-image 'https://x.com/owner/status/OWNER_CONTROLLED_IMAGE' \
    --x-video 'https://x.com/owner/status/OWNER_CONTROLLED_VIDEO' \
    --x-gif 'https://x.com/owner/status/OWNER_CONTROLLED_GIF' \
    "${story_args[@]}"
)
```

工具預設驗證 extraction 與下載首個 byte；加 `--verify-previews` 可檢查 raster preview，預設 placeholder 不符合此檢查。這不等於整檔下載驗證，完整保存仍須依[下載診斷](#安全下載診斷)核對。非預設服務位址用 `--base-url` 指定。

輸出只保留 case label、status、item count 與 outcome，不得記錄 URL、token、Cookie 或 upstream media URL。Cookie 輪替後先確認新的 extractor process 已讀取新檔，再做人工檢查；preview／download 仍不得攜帶 Cookie。
