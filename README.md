# SNS Media List

SNS Media List 是一個低併發、自架式 Web 工具，可分析公開 Instagram 貼文/Reel 與 X 狀態貼文，依原始順序列出可直接下載的圖片、漸進式影片與動畫 GIF。

> 請只下載你有權保存的內容。使用者需自行確認內容的著作權、平台條款與當地法規。

## 主要功能

- 支援公開 Instagram 單篇貼文、Reel 與 X 單篇狀態貼文。
- 保留 carousel 或多媒體貼文的來源順序。
- 每個媒體項目提供獨立預覽、資訊與下載操作。
- 使用短效 opaque token 隱藏 upstream media URL。
- 不建立帳號、不保存歷史記錄，也不永久保存完整媒體檔案。
- 提供繁體中文 responsive Web 介面與 Docker Compose 部署。

## 支援範圍與限制

| 項目 | 支援狀態 |
| --- | --- |
| Instagram 公開 `/p/` 貼文與 `/reel/` Reel | 支援，但受匿名平台存取限制 |
| X/Twitter 公開 `/status/` 貼文 | 支援，但受匿名平台存取限制 |
| 圖片、漸進式影片、X 動畫 GIF | 支援 |
| 混合 carousel | 支援，依來源順序列出 |
| 私人、追蹤者限定、年齡限制或登入後內容 | 不支援 |
| Stories、Highlights、帳號頁、feed、搜尋與 thread 批次下載 | 不支援 |
| HLS/DASH 合併、轉碼、ZIP 與畫質選擇 | 不支援 |

公開可見不代表匿名模式一定能存取。Instagram、X 或 `gallery-dl` 可能要求登入、限制訪客 token 或套用 rate limit；本服務不接受平台 Cookie、credentials 或使用者 extractor config。

本服務不支援私人貼文，也不會要求或儲存平台登入資料。

## 安全設計

- 僅接受明確支援的 HTTPS 貼文 URL 與平台/CDN host。
- 每次連線前驗證所有 DNS A/AAAA 回應，拒絕 private、loopback、link-local、reserved 與其他非 public IP。
- outbound transport 連接已驗證的 IP，同時保留原 hostname 作為 TLS SNI、憑證驗證與 HTTP Host。
- extractor 透過受限的 loopback CONNECT proxy 執行，並隔離 HOME、proxy variables、credentials、plugins 與 user config。
- 預覽與下載 token 綁定用途，預設存活 10 分鐘，僅存於單一 process 記憶體。
- container 使用單 worker、UID 10001、read-only root filesystem 與 bounded tmpfs，不掛載持久 media volume。
- application 與 reverse proxy 不應記錄 token path、query string、Cookie、request body 或 upstream media URL。

此工具定位為個人或小型可信群組使用，不適合直接作為公開、多租戶下載服務。

## Docker Compose 快速啟動

需求：Docker Engine 與 Docker Compose v2。

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/healthz
```

開啟 <http://127.0.0.1:8000>。

若 host port 8000 已被使用：

```bash
SNS_MEDIA_HOST_PORT=8080 docker compose up -d --build
curl --fail http://127.0.0.1:8080/healthz
```

重新建置或停止：

```bash
docker compose up -d --build app
docker compose stop -t 10 app
docker compose down
```

完整部署、升級、rollback、trusted proxy 與 reverse proxy log filtering 請參閱 [OPERATIONS.md](OPERATIONS.md)。

## 本機開發

需求：Python 3.12 與 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --extra dev
uv run playwright install chromium
uv run uvicorn sns_media_list.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

應用程式啟動時會在 `127.0.0.1:8765` 建立 extractor CONNECT proxy；請勿另外啟動第二個使用相同 port 的 instance。

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

`container_smoke.py` 需要可用的 Docker daemon。Live platform smoke tests 必須使用 owner-controlled 公開貼文，操作方式請參閱 [OPERATIONS.md](OPERATIONS.md)。

## 專案結構

```text
src/sns_media_list/   FastAPI app、API、extractor、network policy、token store 與靜態 UI
tests/                unit、API、integration、browser 與 container tests
scripts/              contract、docstring、container 與 manual smoke 工具
docs/                 設計、實作計畫與 gallery-dl 更新流程
Dockerfile            non-root production image
docker-compose.yaml   單 worker hardened deployment
OPERATIONS.md         完整 operator guide
```

## 常見錯誤

| Code | HTTP | 說明 |
| --- | ---: | --- |
| `unsupported_url` | 400 | URL host、scheme 或貼文 path 不受支援 |
| `post_unavailable` | 404 | 貼文不存在、需要登入，或匿名 extractor 無法讀取 |
| `no_media` | 422 | 沒有可直接串流的媒體 |
| `local_rate_limited` | 429 | 本機 extraction/download slot 已滿 |
| `upstream_rate_limited` | 429 | 平台限制匿名存取 |
| `extraction_failed` | 502 | extractor output 或 upstream 行為不符合 pinned contract |
| `upstream_media_invalid` | 502 | 媒體狀態、MIME、signature 或大小不符合安全要求 |
| `capacity_exceeded` | 503 | token store 無法原子保留完整結果 |
| `extraction_timeout` | 504 | extractor 超過 deadline |
| `token_expired` | 410 | token 已超過 TTL，請重新分析 |
| `token_not_found` | 404 | token 不存在、已清理或服務已重新啟動 |

API 不會回傳 upstream media URL、Cookie、credentials、request headers、raw extractor output 或 stack trace。請勿在 issue、log 或錯誤回報中貼出 media token 或敏感 URL。

## 第三方元件與授權

本專案使用 pinned `gallery-dl` 執行公開貼文 extraction。`gallery-dl` 採 GPL-2.0-only，相關 notice 位於 [`LICENSES/gallery-dl.txt`](LICENSES/gallery-dl.txt)。

SNS Media List 專案程式碼採 [MIT License](LICENSE)。
