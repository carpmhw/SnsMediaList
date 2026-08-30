<div align="center">

<img src="src/sns_media_list/static/favicon.svg" width="72" alt="SNS Media List icon">

# SNS Media List

**自架式 Instagram 與 X 媒體整理、預覽及下載工具**

[功能特色](#功能特色) • [支援範圍](#支援範圍與限制) • [快速開始](#快速開始) • [本機開發](#本機開發)

</div>

SNS Media List 是一個低併發 Web 工具，可分析支援的 Instagram 貼文、Reel、精確單則 Story 與 X 狀態貼文，依來源順序列出可直接下載的圖片、漸進式影片與動畫 GIF。服務可匿名運作，也可由 operator 掛載平台 Cookie，存取該帳號可見的支援內容。

> [!IMPORTANT]
> 請只保存你有權下載的內容，並自行確認著作權、平台條款與當地法規。平台 Cookie 是 bearer credential；啟用後只應部署於本人環境或可信內網，不要將服務直接暴露於不受信任的公開網路。

## 功能特色

- 支援 Instagram 單篇貼文、Reel、精確單則 Story，以及 X 單篇狀態貼文。
- 保留 carousel 與多媒體貼文的來源順序，支援圖片、漸進式影片及 X 動畫 GIF。
- 提供媒體多選、類型篩選、檔名複製與逐項啟動下載；瀏覽器可能要求多檔下載權限。
- 一般貼文、Reel 與 X 媒體使用含作者識別的智慧檔名。
- 批次分析最多 5 個 URL，透過循序前端 queue 呼叫既有單筆 API，不建立後端工作佇列。
- 使用短效、用途綁定的 opaque token 隱藏 upstream media URL。
- 預覽優先使用可信 CDN poster；缺少 poster 時預設使用本機 placeholder，只有 operator 明確啟用後才按需生成受限 JPEG。
- 不建立使用者帳號、不保存歷史記錄，也不永久保存完整媒體檔案。

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

公開可見不代表匿名模式一定能存取。Instagram、X 或 `gallery-dl` 可能要求登入、限制訪客 token 或套用 rate limit；未配置平台 Cookie 時，本服務不支援私人貼文，精確 Story 也只會嘗試匿名擷取。

圖片 Story 不會另外輸出音訊或 audio；不提供 Story 批次、ZIP 或轉碼。選取下載不提供 ZIP、帳號 feed 批次封存或歷史保存；「已開始下載」只表示檔案已交給瀏覽器，不代表瀏覽器或 OS 已完成檔案保存。

## 快速開始

需求：[Docker Engine](https://docs.docker.com/engine/) 與 Docker Compose v2。

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/healthz
```

開啟 <http://127.0.0.1:8000>。若 port 8000 已被使用：

```bash
SNS_MEDIA_HOST_PORT=8080 docker compose up -d --build
curl --fail http://127.0.0.1:8080/healthz
```

停止服務：

```bash
docker compose stop -t 10 app
docker compose down
```

> [!NOTE]
> Compose 預設只綁定 `127.0.0.1`，不包含 TLS、身分驗證或公開 ingress。完整部署、升級、reverse proxy 與故障排除請參閱 [OPERATIONS.md](OPERATIONS.md)。

## 平台 Cookie 驗證（可信部署）

Cookie 不經 Web UI 或 API 傳入，只以 read-only file mount 提供給對應平台的 extractor。請使用專用、低權限帳號，並保護 Netscape `cookies.txt`。

```bash
# Instagram
SNS_MEDIA_INSTAGRAM_COOKIE_HOST_FILE=/srv/secrets/instagram.cookies.txt \
  docker compose -f docker-compose.yaml -f docker-compose.instagram-auth.yaml up -d --build

# X
SNS_MEDIA_X_COOKIE_HOST_FILE=/srv/secrets/x.cookies.txt \
  docker compose -f docker-compose.yaml -f docker-compose.x-auth.yaml up -d --build
```

配置 Instagram Cookie 後，所有服務使用者都可能間接使用 operator 帳號的 Story 可見權限，包括私人、Close Friends 或受眾限定內容。服務不提供個別使用者的權限隔離，只適合本人或可信內網。輪替、撤銷與匿名模式 rollback 流程請參閱 [操作指南](OPERATIONS.md#平台-cookie-驗證)。

## 安全設計

- 僅接受明確支援的 HTTPS URL 與平台/CDN host，拒絕不安全 scheme、port 與路徑。
- 每次連線前驗證所有 DNS A/AAAA 回應，拒絕 private、loopback、link-local 與 reserved IP。
- Extractor 經受限 loopback CONNECT proxy 執行，隔離 HOME、proxy variables、plugins 與 user config。
- Preview 與 download token 綁定用途，預設有效 10 分鐘，僅保存於單一 process 記憶體。
- Container 使用單 worker、UID 10001、read-only root filesystem 與 bounded tmpfs。
- 不掛載持久 media volume，且不應記錄 token path、Cookie、request body 或 upstream media URL。

## 關鍵設定

常用預設值包括 600 秒 token TTL、500 MB 單檔上限、10 秒連線 timeout、30 秒 read idle timeout、4 個 process-wide 與每 client 2 個下載 slot。Generated preview 預設由 `SNS_MEDIA_GENERATED_PREVIEWS_ENABLED=false` 關閉；完整環境變數、timeout 語意與 trusted proxy 說明請參閱 [OPERATIONS.md](OPERATIONS.md)。

## 本機開發

需求：Python 3.12 與 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --extra dev
uv run playwright install chromium
uv run uvicorn sns_media_list.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

應用程式啟動時會在 `127.0.0.1:8765` 建立內部 extractor CONNECT proxy；請勿同時啟動另一個使用相同 port 的 instance。

## 測試與品質檢查

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python scripts/check_function_docstrings.py src tests scripts
uv run python scripts/verify_gallery_contract.py
uv run python scripts/container_smoke.py
```

`container_smoke.py` 需要可用的 Docker daemon。Live platform smoke tests 必須使用 owner-controlled 內容，詳見 [OPERATIONS.md](OPERATIONS.md#owner-controlled-manual-smoke-tests)。

## 專案結構

```text
src/sns_media_list/   FastAPI app、extractor、network policy、token store 與靜態 UI
tests/                Unit、API、integration、browser 與 container tests
scripts/              Contract、docstring、container 與 security 檢查工具
deploy/nginx/         Authenticated reverse proxy 設定範例
Dockerfile            Non-root production image
docker-compose*.yaml  單 worker 部署與平台 Cookie overlays
OPERATIONS.md         完整 operator guide
```

## 常見錯誤

| Code | HTTP | 說明 |
| --- | ---: | --- |
| `unsupported_url` | 400 | URL host、scheme 或媒體 path 不受支援 |
| `post_unavailable` | 404 | 貼文不存在、需要登入，或匿名 extractor 無法讀取 |
| `story_unavailable` | 404 | 精確 Story 不可用；不細分或推斷過期、刪除、登入需求或可見性原因 |
| `local_rate_limited` | 429 | 本機 extraction/download slot 已滿 |
| `platform_authentication_failed` | 503 | Operator 配置的平台 session 已過期、無效或遭 challenge |
| `token_expired` | 410 | Token 已超過 TTL，請重新分析 |

API 不會回傳 upstream media URL、Cookie、credentials、raw extractor output 或 stack trace。請勿在 issue、log 或錯誤回報中貼出 media token、Cookie 或敏感 URL。

本專案使用 pinned [`gallery-dl`](https://github.com/mikf/gallery-dl) 執行 extraction；其 GPL-2.0-only notice 位於 [`LICENSES/gallery-dl.txt`](LICENSES/gallery-dl.txt)。FFmpeg notice 位於 [`LICENSES/ffmpeg.txt`](LICENSES/ffmpeg.txt)，專案程式碼採 [MIT License](LICENSE)。
