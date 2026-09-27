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

## Instagram Story Extraction Diagnostics

進入 extraction route 的擷取失敗會以唯一 `extraction_failed` structured event 寫入 application log。`reason_code` 表示穩定的應用程式錯誤語意；選用的 `failure_stage` 是 server-side 技術分類，兩者不可互相推測。Stage 只出現在失敗事件，不會加入 API body、headers、OpenAPI 或前端訊息。事件不包含完整 URL、Story ID、Cookie path／value、token、upstream URL、gallery-dl command、raw stdout／stderr 或 exception chain。Story 分類依可辨識的 diagnostic 證據；配置 Cookie 本身不表示 session 已驗證有效。

檢視最近十分鐘的 application structured events：

```bash
docker compose logs --no-log-prefix --since 10m app
```

排錯時只使用上述 structured event。請保留 Uvicorn `--no-access-log`，不可為診斷啟用 access log，因為 token-bearing media path 不可寫入日誌；reverse proxy 也必須停用或遮蔽對應路徑的 access log。

| `reason_code` | HTTP | 說明與處置 |
| --- | ---: | --- |
| `story_auth_required` | 403 | 未配置 Instagram Cookie，或匿名 Story 明確要求登入。請由管理者依[平台 Cookie 驗證](#平台-cookie-驗證)設定受限的唯讀 Cookie，不要透過 request 上傳憑證。 |
| `platform_authentication_failed` | 503 | configured Story 回報 `AuthRequired`／`AuthenticationError`，或明確 `invalid/expired` session、login、challenge。這是 bounded 驗證診斷，不保證 Cookie 一定過期；管理者應檢查唯讀掛載、帳號狀態及授權後再輪替或撤銷 session。 |
| `story_unavailable` | 404 | `NotFoundError`／HTTP 404 或明確 Story 過期、刪除、private／不可見證據。系統不細分實際原因；請確認精確 URL 及 operator 帳號可見性。 |
| `upstream_rate_limited` | 429 | 平台 rate limit。降低 operator concurrency 並等待平台解除限制。 |
| `extraction_timeout` | 504 | extractor 超過設定期限。稍後重試；不要以延長 timeout 或取消 process 限制繞過。 |
| `extraction_failed` | 502 | 其他／無法分類的 extractor failure。configured Story 僅有 HTTP 401/403、沒有明確 session 或 availability 證據時也會保守回傳此 code；不可據此推斷 Cookie 過期或 Story 不存在。 |

### Failure stage

若 `AppError` 沒有 stage，日誌省略 `failure_stage`；任何無效或未識別的 stage 都會固定正規化為 `unknown`。下列九種 stage 不改變既有 `reason_code`、HTTP status 或 response：

| `failure_stage` | 發生條件與排錯方向 |
| --- | --- |
| `extractor_start` | gallery-dl subprocess 無法啟動；檢查 candidate image 內 executable 與 runtime。 |
| `extractor_timeout` | extractor operation deadline 到期；檢查 DNS、upstream network 與 CONNECT proxy，不直接提高 timeout。 |
| `extractor_output_limit` | stdout 超過既有大小上限；檢查 pinned DataJob output 是否異常增大，不放寬限制。 |
| `extractor_io` | stdout／stderr pipe 或 communicate 讀取發生 I/O failure；檢查 pipe、process 資源與 bounded cleanup。 |
| `extractor_process_unclassified` | 非零退出的 stderr 無法分類，或零退出 stdout 含合法但未知的 DataJob error record；configured Story 僅 HTTP 401/403 也屬 ambiguous，不能推斷 Cookie 失效。 |
| `extractor_invalid_output` | UTF-8／JSON／DataJob schema 無效，包含零 bytes 與純空白；請核對 pinned gallery-dl contract。 |
| `extractor_empty_output` | 成功退出且輸出合法 literal `[]`；Story 維持 `story_unavailable`，Post／Reel／X 維持 `extraction_failed`，不以此推論 Cookie 狀態。 |
| `extractor_no_media` | 輸出只有合法 directory／queue events，沒有 error 或 media record；這不代表 normalizer 過濾媒體後的 `no_media`，後者省略 stage。 |
| `extractor_platform_error` | 命中既有明確 authentication、availability 或 rate-limit 分類；請依上方 `reason_code` 表處理。 |

`[]` 是合法空陣列，與零 bytes 或空白輸出不同；後兩者是 `extractor_invalid_output`。`extractor_process_unclassified` 也可能來自**成功退出**但含未知合法 DataJob error，不代表一定是非零退出。

### Extractor diagnostic 欄位

失敗事件可選擇包含下列四個去識別欄位；沒有證據時省略，未知 enum 使用固定 `unknown`。舊錯誤呼叫端不會補值，也不會由 `reason_code`、`failure_stage` 或本服務 API status 推測診斷值。

| 欄位 | 值域與解讀 |
| --- | --- |
| `extractor_diagnostic_source` | `stderr`、`datajob_error` 或 `unknown`；指出既有證據來源，不代表錯誤原因。 |
| `extractor_error_type` | `auth_required`、`authentication_error`、`authorization_error`、`not_found`、`http_error`、`challenge_error`、`extraction_error`、`no_extractor` 或 `unknown`。Stderr 沒有結構化 type，固定為 `unknown`。 |
| `extractor_exit_code` | 自然完成程序的精確整數 `-255..255`，包含 `0`；負值依 process return code 表示 signal，不代表平台錯誤類別。零退出 DataJob error 及零退出 invalid／empty／non-media 結果會記為 `0`。 |
| `extractor_http_statuses` | 從移除 URL 後的 bounded 訊息中，依既有明確 HTTP／status 前綴或 reason phrase 語法取得；100..599、升冪、去重，最多保留最小 8 個，JSON 型別為 array。這是有界觀察值，不是完整請求追蹤。 |

Start、timeout、output limit、I/O 與 caller cancellation 不記錄退出碼，以免將 cleanup 的 terminate／kill signal 誤當失敗原因。狀態只來自 extractor 證據；裸數字、Story ID、URL／query 與本服務 API status 都不會產生狀態值。沒有狀態不代表沒有 upstream HTTP failure。`extractor_http_statuses: [403]` 表示觀察到的平台診斷，不能與本服務回傳的 HTTP 502 混為一談，也不能證明 Cookie session 有效或失效。

以下是合成事件；分享時只保留 allowlisted 欄位，不附加 raw extractor output：

```json
{"event":"extraction_failed","request_id":"0123456789abcdef0123456789abcdef","platform":"instagram","outcome":"failed","duration_ms":12.5,"reason_code":"extraction_failed","failure_stage":"extractor_process_unclassified","extractor_diagnostic_source":"datajob_error","extractor_error_type":"http_error","extractor_exit_code":0,"extractor_http_statuses":[403]}
```

HTTP 429 優先映射 rate limit；HTTP 404 映射 Story availability；只有 HTTP 401/403 的 configured Story 屬於 ambiguous refusal。所有 extraction 嘗試只執行一次，不會 anonymous retry。`extractor_invalid_output` 請執行 `uv run python scripts/verify_gallery_contract.py`；`extractor_empty_output` 請檢查精確 Story／Post 過濾與 upstream 結果；`extractor_io` 檢查 pipe 與程序資源；`extractor_timeout` 檢查 network／CONNECT proxy，不放寬期限。`extractor_process_unclassified` 請先依[Instagram Extractor Compatibility](#instagram-extractor-compatibility)完成 source／runtime、contract、release notes 與 isolated candidate gate 排查；只有 candidate 仍 unclassified 且取得安全去識別 diagnostic 時，才評估最小 classifier pattern。不可傾印或分享 raw stderr／stdout。

驗證時一併記錄 source 與 image 身分：`git rev-parse HEAD`、`git status --short`，以及 `docker image inspect --format '{{.Id}} {{json .RepoDigests}}' <candidate-image>` 的實際 image ID／digest。Deterministic fake-extractor／container 結果與 owner-controlled live Story 重試分開記錄；fixture 通過不表示原 Story 已修復。live Story 是選用人工檢查，僅在目標仍有效且 operator 有權存取時透過既有受保護輸入流程執行。若需回復版本，沿用[既有升級與 rollback 流程](#升級與-rollback)；不以此 diagnostics 變更改動部署邊界。

## Instagram Extractor Compatibility

`extractor_process_unclassified` 不表示 subprocess 一定以非零狀態結束；gallery-dl 可能零退出並輸出合法但未知的 DataJob error record。不要單靠此 stage 推斷 Cookie 失效、Story 過期或 redirect 根因，也不要為了取得線索記錄 raw stdout／stderr。

Instagram extractor 相容性問題依下列順序診斷，先核對版本與 contract，再評估分類變更：

1. **比對 source 與 runtime。** 記錄 `git rev-parse HEAD`、`git status --short`、source pin、實際 Compose project／service／container、image ID／digest 與 container 內的 gallery-dl 版本。若 `docker compose` 回報 `service app is not running`，可能是目前目錄指向不同 project；先依 Compose labels 找出實際執行個體，再以明確 project、Compose 檔、service 或 container 名稱查詢，不要先重建或替換服務：

   ```bash
   docker ps --filter label=com.docker.compose.service=app --format '{{.ID}} {{.Names}} {{.Image}} {{.Label "com.docker.compose.project"}} {{.Label "com.docker.compose.service"}}'
   docker inspect --format '{{.Id}} {{.Config.Image}} {{.Config.Labels}}' <container>
   docker compose -p <project> -f <compose-file> exec <service> gallery-dl --version
   docker logs --since 10m <container>
   ```

   `pyproject.toml` 的 source pin 不能代替運作中 container 的 runtime 證據。Cookie 檔案存在、同步、格式／權限檢查通過或未到輪替期限，都不能證明平台 session 已驗證有效；診斷欄位也不作此推論。
2. **驗證安裝套件 contract。** 執行 `uv run python scripts/verify_gallery_contract.py`；它會核對已安裝 gallery-dl 版本、`--resolve-json` 和離線 DataJob producer／adapter consumer 相容性。`extractor_invalid_output` 優先檢查這一層；不可依真實平台或 Cookie 取得 fixture。
3. **查官方 release notes。** 本次版本為 From `1.32.7` → To `1.32.13`；確認修正是否涵蓋觀察到的 Instagram 行為。`v1.32.13` release notes 列有 `fix redirect detection during user lookup`。版本更新是相容性假說的驗證，不單獨證明原 Story 已修復。
4. **建立唯一 candidate image 並跑 gate。** 目前 Dockerfile 固定 Python 3.12 Debian Trixie digest `python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`，FFmpeg pin 為 `7:7.1.5-0+deb13u1`。本次 Trixie candidate tag 為 `sns-media-list:candidate-gallery-dl-1.32.13-trixie`。使用獨立 candidate image tag，核對 gallery-dl／FFmpeg runtime、`/healthz`、container limits、deterministic smoke，並執行 `uv run python scripts/security_gate.py --image sns-media-list:candidate-gallery-dl-1.32.13-trixie`。Smoke image 與 candidate 必須分別記錄；container smoke 使用 per-run image tag，不得重標記共享 `sns-media-list:local` 或替換運作中的 service。Security gate 有未修復或可修復的 HIGH／CRITICAL finding 時，candidate 保持不可部署；不新增例外或由其它 image 的 smoke 結果替代。
5. **最後才評估 classifier。** 只有 candidate 仍是 `extractor_process_unclassified` 且取得安全去識別 diagnostic，才修訂規格並評估 narrow classifier。不可加入 broad `redirect|home page|instagram|failed` pattern；單獨 home-page redirect 不等於 Cookie 失效。沒有安全 fixture 時保留既有九種 stage 與 classifier。

owner-controlled live Story smoke 是選用檢查，不是 CI／release gate；只透過下方受保護檔案流程執行。若回復版本，使用已知的前一不可變 image reference，或只回復本 change 的 source 修改；不把可變 tag 當 rollback 證據。Baseline、candidate image ID／digest、release notes、gate 結果、未執行的 live 案例與 rollback reference 記錄於 OpenSpec change 的 `validation.md`。

## 故障排除

- Service unhealthy：執行 `docker compose logs --no-log-prefix app` 並查詢 `/healthz`。
- `[FATAL tini] exec uvicorn failed`：重建 image，再以 `docker compose up -d --force-recreate` 啟動。
- `extraction_failed` 或 `post_unavailable`：確認 URL 受支援，並確認平台允許目前模式存取。
- Instagram Story 的 error code／reason code 與安全查詢方式請參閱 [Instagram Story Extraction Diagnostics](#instagram-story-extraction-diagnostics)。
- `platform_authentication_failed` 不代表已證實 session 過期；Cookie 管理仍依平台 Cookie 驗證流程進行。
- `story_unavailable` 不細分過期、刪除、NotFound 與不可見原因，請確認精確 URL 及帳號可見性。
- 上述 Story classification 都不會 anonymous retry，公開 response 也不暴露 operator session 狀態細節。
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
