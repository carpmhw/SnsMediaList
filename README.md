<div align="center">

<img src="src/sns_media_list/static/favicon.svg" width="72" alt="SNS Media List 圖示">

# SNS Media List

**自架式 Instagram 與 X 媒體整理、預覽及下載工具**

[功能特色](#功能特色) • [快速開始](#快速開始) • [MCP](#mcp-server) • [本機開發](#本機開發) • [操作指南](OPERATIONS.md)

</div>

SNS Media List 是一個低併發 Web 工具，可分析支援的 Instagram 貼文、Reel、精確單則 Story 與 X 狀態貼文，依來源順序列出可直接下載的圖片、漸進式影片與動畫 GIF。服務可匿名運作，也可由 operator 掛載平台 Cookie，存取該帳號可見的支援內容。

後端採 FastAPI 與受限的 `gallery-dl` 子程序，前端為原生 HTML、CSS、JavaScript，另提供選用的 MCP Server。部署使用單一 process／worker，無需資料庫。

> [!IMPORTANT]
> 請只保存你有權下載的內容，並自行確認著作權、平台條款與當地法規。平台 Cookie 是 bearer credential；啟用後只應部署於本人環境或可信內網，不要將服務直接暴露於不受信任的公開網路。

## 功能特色

- 支援 Instagram 單篇貼文、Reel、精確單則 Story，以及 X 單篇狀態貼文。
- 保留 carousel 與多媒體貼文的來源順序，支援圖片、漸進式影片及 X 動畫 GIF。
- 提供媒體多選、類型篩選、檔名複製與逐項啟動下載；瀏覽器可能要求多檔下載權限。
- 一般貼文、Reel 與 X 媒體使用含作者識別的智慧檔名。
- 批次分析最多 5 個 URL，透過循序前端 queue 呼叫既有單筆 API，不建立後端工作佇列。
- 使用短效、用途綁定的 opaque token 隱藏 upstream media URL。
- Instagram 與 X 漸進式影片符合條件的提前截斷可自動續傳一次；需 CDN 回傳有效的 Range response。
- 預覽優先使用可信 CDN poster；缺少 poster 時預設使用本機 placeholder，只有 operator 明確啟用後才按需生成受限 JPEG。
- 不建立使用者帳號、不保存歷史記錄，也不永久保存完整媒體檔案。
- 可透過 REST API 或選用的 MCP `extract_media` tool 取得同一套媒體清單與短效下載連結。

## 支援範圍與限制

| 項目 | 狀態 |
| --- | --- |
| Instagram `/p/` 貼文與 `/reel/` Reel | 支援，但受平台匿名存取限制 |
| Instagram `/stories/<username>/<numeric-media-id>/` 精確單則 Story | 支援；匿名擷取僅為 best effort，不保證成功 |
| X/Twitter `/status/` 單篇貼文 | 支援，但受平台匿名存取限制 |
| 圖片、漸進式影片、X 動畫 GIF | 支援 |
| Story 主要圖片或漸進式影片 | 支援，每則只回傳一個主要媒體 |
| Carousel 與混合媒體 | 支援，依來源順序列出 |
| 私人、Close Friends、受眾限定或登入後內容 | 匿名模式不支援；Cookie 模式限 operator 帳號可見範圍 |
| 帳號全部 Stories、Stories tray、Highlights 與 Story 批次下載 | 不支援 |
| 帳號頁、feed、搜尋、thread 批次、ZIP、轉碼與畫質選擇 | 不支援 |
| HLS/DASH adaptive stream 合併 | 不支援 |

公開可見不代表匿名模式一定能存取。Instagram、X 或 `gallery-dl` 可能要求登入、限制訪客 token 或套用 rate limit；未配置平台 Cookie 時，本服務不支援私人貼文，精確 Story 也只會嘗試匿名擷取。Instagram Story 通常需要 authenticated cookies；Cookie 部署與錯誤診斷請參閱[平台 Cookie 驗證](OPERATIONS.md#平台-cookie-驗證)及 [Story 擷取診斷](OPERATIONS.md#instagram-story-extraction-diagnostics)。

圖片 Story 不會另外輸出音訊或 audio；不提供 Story 批次、ZIP 或轉碼。選取下載不提供 ZIP、帳號 feed 批次封存或歷史保存；「已開始下載」只表示檔案已交給瀏覽器，不代表瀏覽器或 OS 已完成檔案保存。

## 快速開始

需求：[Docker Engine](https://docs.docker.com/engine/) 與 Docker Compose v2。取得專案後，在儲存庫根目錄執行：

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/healthz
```

開啟 <http://127.0.0.1:8000>。若 port 8000 已被使用：

```bash
export SNS_MEDIA_HOST_PORT=8080
docker compose up -d --build
curl --fail http://127.0.0.1:8080/healthz
```

停止服務：

```bash
docker compose stop -t 10 app
docker compose down
```

> [!NOTE]
> Compose 預設只綁定 `127.0.0.1`，不包含 TLS、身分驗證或公開 ingress。完整部署、升級、reverse proxy 與故障排除請參閱 [OPERATIONS.md](OPERATIONS.md)。

### 使用流程

1. 在「單筆分析」貼上支援的 HTTPS 內容 URL；多個 URL 可切換「批次分析」，每行一個、最多 5 個。
2. 分析後依媒體類型篩選、預覽，並選取要下載的項目；批次結果可切換不同來源群組。
3. 逐項啟動下載，依瀏覽器提示允許多檔下載。連結預設有效 10 分鐘，到期或服務重啟後請重新分析。

`/healthz` 只確認服務仍在執行，不代表 Instagram／X 可用；下載完成請以瀏覽器與本機檔案為準。

## 平台 Cookie 驗證（可信部署）

Cookie 不經 Web UI 或 API 傳入，只以唯讀檔案掛載提供給對應平台的 extractor。請使用專用、低權限帳號，並確認 Netscape `cookies.txt` 可由容器 UID 10001 讀取。

```bash
# Instagram
SNS_MEDIA_INSTAGRAM_COOKIE_HOST_FILE=/srv/secrets/instagram.cookies.txt \
  docker compose -f docker-compose.yaml -f docker-compose.instagram-auth.yaml up -d --build

# X
SNS_MEDIA_X_COOKIE_HOST_FILE=/srv/secrets/x.cookies.txt \
  docker compose -f docker-compose.yaml -f docker-compose.x-auth.yaml up -d --build
```

配置 Instagram Cookie 後，所有服務使用者都可能間接使用 operator 帳號的 Story 可見權限，包括私人、Close Friends 或受眾限定內容。服務不提供個別使用者的權限隔離，只適合本人或可信內網。兩平台同時啟用、輪替、撤銷與回復匿名模式請參閱 [操作指南](OPERATIONS.md#平台-cookie-驗證)。既有部署須沿用原 Compose project 與完整 overlays。

## REST API

| 方法與路徑 | 用途 |
| --- | --- |
| `POST /api/extractions` | 接受單一 `{"url":"…"}`，回傳來源資訊、有序媒體清單及短效連結 |
| `GET /api/media/{token}/preview` | 取得預覽；缺少可信 poster 時，結果可改指向 `/placeholder.svg` |
| `HEAD /api/media/{token}/download` | 僅驗證 download token，成功回傳 204，不連線上游媒體 |
| `GET /api/media/{token}/download` | 以附件串流下載媒體 |
| `GET /healthz` | 本機存活檢查，回傳 `{"status":"ok"}` |

REST 的媒體連結一律相對於服務 origin 根路徑。Preview 與 download token 不可互換；HEAD 成功不保證後續上游下載成功。公開錯誤使用 `{code, message, request_id}`，常見分類見[常見錯誤](#常見錯誤)。

## MCP Server

MCP Server **預設關閉**，啟用後透過同一 app 的 `/mcp` 提供 **Streamable HTTP**，僅公開 `extract_media` tool，與 REST 共用擷取服務、併發額度及短效 token。

使用明確的 Compose override 啟用：

```bash
docker compose -f docker-compose.yaml -f docker-compose.mcp.yaml up -d --build
```

將 MCP Client 的 Streamable HTTP endpoint 設為 `http://127.0.0.1:8000/mcp`，不需要新增 port。已有部署須沿用原 project 與全部 Cookie overlays；協定檢查、設定及回復流程見 [MCP 操作指南](OPERATIONS.md#mcp-deployment)。本機 Uvicorn 可使用 `SNS_MEDIA_MCP_ENABLED=true` 環境變數；Compose 的固定 environment 必須以 override 覆寫。

Tool input（只接受一個長度 1–2048 的 URL 字串）：

```json
{"url":"https://x.com/example/status/123/"}
```

結果包含來源資訊、檔名、媒體類型、可用的尺寸／時長，以及 preview／download URL；[完整結果範例](OPERATIONS.md#工具輸入與結果)見操作指南。MCP 不傳圖片／影片二進位內容，也不回傳 upstream URL、Cookie、headers 或 raw extractor output，下載仍使用 `/api/media/{token}/download`。

### 絕對媒體連結

- 未設定 `SNS_MEDIA_PUBLIC_BASE_URL`：回傳 root-relative **相對連結**，Client 依 operator 提供的服務 origin 解析，不能將 `/mcp` 當作路徑前綴。
- 設為 `https://sns-media.example.internal`：MCP 連結變為該 origin 加 `/api/media/{token}/preview` 或 `/api/media/{token}/download`；placeholder 也變為絕對連結，null preview 保持 null。
- **REST API 無論是否設定都維持相對連結**。此值只接受 HTTP(S) origin，不支援公開 URL path prefix，也不提供驗證或自動修改 Host／Origin allowlist。

Compose 請使用自行管理的 deployment-owned override；LAN／reverse proxy 範例與完整驗證規則見[公開媒體 Origin](OPERATIONS.md#公開媒體-origin-與-lan-override)。

### 請求限制與存取邊界

所有 MCP Client 共用限流身分：預設同時最多一個 MCP extraction，每 60 秒最多 **10 個 POST**，其中 1 個保留給匹配的 legacy cancellation；握手與工具列表也消耗一般額度。REST／MCP 合計受 `SNS_MEDIA_MAX_EXTRACTIONS` 約束，超限不排隊。

MCP 僅適合 loopback、可信網路或有驗證的 reverse proxy，目前沒有 OAuth／個別使用者授權；啟用平台 Cookie 後，MCP 使用者也可能間接使用 operator 帳號的可見權限。Host／Origin 設定及限流細節見[操作指南](OPERATIONS.md#mcp-deployment)。

## 安全設計

- 僅接受明確支援的 HTTPS URL 與平台/CDN host，拒絕不安全 scheme、port 與路徑。
- 每次連線前驗證所有 DNS A/AAAA 回應，拒絕 private、loopback、link-local 與 reserved IP。
- Extractor 經受限 loopback CONNECT proxy 執行，隔離 HOME、proxy variables、plugins 與 user config。
- Preview 與 download token 綁定用途，預設有效 10 分鐘，僅保存於單一 process 記憶體。
- Container 使用單 worker、UID 10001、read-only root filesystem 與 bounded tmpfs。
- 不掛載持久 media volume，且不應記錄 token path、Cookie、request body 或 upstream media URL。

## 關鍵設定

常用預設值包括 600 秒 token TTL、500 MB 單檔上限、10 秒連線 timeout、30 秒 read idle timeout、4 個 process-wide 與每 client 2 個下載 slot。Generated preview 預設由 `SNS_MEDIA_GENERATED_PREVIEWS_ENABLED=false` 關閉；環境變數見[設定](OPERATIONS.md#設定)，下載與預覽的期限差異見[timeout 說明](OPERATIONS.md#批次下載與-timeout)。

## 本機開發

需求：Python 3.12 與 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --extra dev
uv run uvicorn sns_media_list.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

應用程式啟動時會在 `127.0.0.1:8765` 建立內部 extractor CONNECT proxy；請勿同時啟動另一個使用相同 port 的 instance。

前端直接由 `src/sns_media_list/static/` 提供，沒有 Node.js 打包步驟。只有執行瀏覽器測試時才需安裝 Chromium：

```bash
uv run playwright install chromium
```

若啟用本機 generated preview，還須提供 `ffmpeg` 執行檔；Docker 映像已包含對應版本。

## 測試與品質檢查

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python scripts/check_function_docstrings.py src tests scripts
```

依變更範圍追加 [gallery-dl contract、容器、Nginx 與安全檢查](OPERATIONS.md#自動化檢查)。瀏覽器測試可能因 Chromium 不可用而跳過，須與通過結果分開記錄；真實平台驗證僅使用 [owner-controlled 內容](OPERATIONS.md#owner-controlled-manual-smoke-tests)。

## 專案結構

```text
src/sns_media_list/
  app.py              FastAPI 工廠與服務生命週期
  api/、mcp/          REST／MCP 入口與請求邊界
  services/           共用擷取協調、縮圖與快取
  extractor/          gallery-dl 子程序、格式轉換與正規化
  network/、security/ 目的地政策、CONNECT proxy、媒體串流與 token
  static/             原生 HTML、CSS、JavaScript 介面
tests/                單元、API、整合、瀏覽器、容器與文件測試
scripts/              協定、contract、品質、容器與安全檢查工具
deploy/nginx/         有身分驗證的 reverse proxy 設定範例
docker-compose*.yaml  基本部署與 Cookie／MCP overlays
Dockerfile            非 root 執行的正式映像
OPERATIONS.md         部署、設定、診斷與驗證指南
```

## 常見錯誤

| Code | HTTP | 說明 |
| --- | ---: | --- |
| `unsupported_url` | 400 | URL host、scheme 或媒體 path 不受支援 |
| `post_unavailable` | 404 | 貼文不存在、需要登入，或匿名 extractor 無法讀取 |
| `story_auth_required` | 403 | 未配置 Instagram Cookie，或匿名 Story 擷取需要登入驗證；請聯絡服務管理者 |
| `story_unavailable` | 404 | 精確 Story 不可取得；不細分或推斷已過期、刪除或不可見 |
| `local_rate_limited` | 429 | 本機請求額度用盡或 extraction/download slot 已滿，依 `Retry-After` 等待 |
| `upstream_rate_limited` | 429 | 平台暫時限制請求，請稍後再試 |
| `platform_authentication_failed` | 503 | 平台 session／驗證要求無法使用；不代表已確認 Cookie 過期 |
| `extraction_failed` | 502 | 其他擷取失敗；configured Story 僅有 HTTP 401/403 時保守歸類 |
| `extraction_timeout` | 504 | 擷取超過既有期限，請稍後再試 |
| `token_expired` | 410 | Token 已超過 TTL，請重新分析 |

API 不會回傳 upstream media URL、Cookie、credentials、raw extractor output 或 stack trace。請勿在 issue、log 或錯誤回報中貼出 media token、Cookie 或敏感 URL。管理者可依[故障排除](OPERATIONS.md#故障排除)查詢安全事件；Story 的公開錯誤與內部診斷分層見 [Story 診斷](OPERATIONS.md#instagram-story-extraction-diagnostics)。

本專案使用 pinned [`gallery-dl`](https://github.com/mikf/gallery-dl) 執行 extraction；其 GPL-2.0-only notice 位於 [`LICENSES/gallery-dl.txt`](LICENSES/gallery-dl.txt)。FFmpeg notice 位於 [`LICENSES/ffmpeg.txt`](LICENSES/ffmpeg.txt)，專案程式碼採 [MIT License](LICENSE)。
