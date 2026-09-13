# SNS Media List — Agent 工作指南

本文件提供在此儲存庫工作的 AI coding agent 專案背景、修改原則與驗證方式。所有指令均在儲存庫根目錄執行。

> 本文件依指定名稱保存為 `Agent.md`。若使用的工具只會自動載入 `AGENTS.md`，請明確引用本文件，或依工具要求調整檔名。

## 1. 溝通與程式規範

- 回覆、工作說明、文件及新增或修改的註解使用繁體中文；程式識別字沿用既有英文命名。
- 所有函數都必須有註解。Python 函數、方法、非同步函數、巢狀函數及測試 helper 使用 docstring；JavaScript 函數使用鄰近的說明註解，沿用既有 JSDoc 風格。
- 既有程式包含英文 docstring；修改函數時依上述規範維護，不為單次任務翻譯整個專案。
- Python 使用明確的型別標註，維持 `mypy` strict 檢查；不要以大量 `Any`、忽略註解或停用規則掩蓋問題。
- Ruff 設定以 `pyproject.toml` 為準：Python 3.12、每行 100 字元，啟用 `E/F/I/UP/B`。
- 維持變更聚焦，先確認工作目錄中的既有修改，避免覆寫使用者工作。只有使用者明確要求時才 commit、push 或建立 PR。

## 2. 專案定位與功能邊界

SNS Media List 是低併發、自架式媒體整理、預覽與下載工具，後端採 FastAPI，前端為直接提供的 HTML、CSS 與原生 JavaScript。

- 支援 Instagram 單篇貼文、Reel、`/stories/<username>/<numeric-media-id>/` 精確單則 Story，以及 X/Twitter 單篇狀態貼文。
- 媒體包含圖片、漸進式影片及 X 動畫 GIF，保留來源順序。單則 Story 只輸出一個主要媒體。
- 批次分析最多 5 個 URL，由前端循序呼叫單筆 API；不要將其改成後端批次工作佇列。
- 選取下載為逐項交給瀏覽器；UI 的「已開始下載」不等於檔案已完成保存。
- 不建立使用者帳號、歷史記錄或永久完整媒體儲存；目前不提供帳號 feed、全部 Stories、Highlights、ZIP、轉碼或 HLS/DASH 合併。
- 匿名存取受平台限制；operator 可透過唯讀 Cookie 檔提供對應平台的帳號可見權限。服務沒有個別使用者權限隔離。

若任務涉及擴充上述邊界，先確認需求與相關規格，再同步調整實作、測試和文件。

## 3. 先讀哪些檔案

| 路徑 | 用途 |
| --- | --- |
| `README.md` | 功能範圍、快速開始與開發入口 |
| `OPERATIONS.md` | 設定、部署、安全邊界、下載診斷與人工 smoke 流程 |
| `pyproject.toml`、`uv.lock` | 相依套件、鎖定版本及品質工具設定 |
| `openspec/specs/` | 各能力的正式需求與情境 |
| `openspec/changes/` | 變更提案及任務；`archive/` 保存歷史變更 |
| `src/sns_media_list/config.py` | `Settings`、預設值及環境變數驗證 |

處理有對應 OpenSpec change 的任務時，先閱讀其 proposal、design、specs 與 tasks（若存在），依任務更新完成狀態。不要把歷史 archive 當成目前待辦，也不要因一般小修擅自建立或封存 change。若規格與實作不一致，明確指出差異，不以猜測擴大修改。

## 4. 架構與責任分工

主要程式碼位於 `src/sns_media_list/`：

| 路徑 | 責任 |
| --- | --- |
| `app.py` | `create_app` 工廠、依賴組裝、lifespan、例外處理與靜態檔案掛載 |
| `api/routes.py` | 分析、下載、預覽 API，以及串流回應生命週期 |
| `api/limits.py`、`api/middleware.py` | 併發與請求次數限制、入口安全邊界 |
| `url_validation.py` | 支援的平台 URL 與精確目標驗證 |
| `services/extraction_service.py` | 驗證、擷取、正規化與 token 核發的協調 |
| `extractor/gallery_dl.py` | 受限 `gallery-dl` subprocess 與平台 Cookie 設定 |
| `extractor/normalizer.py` | 擷取結果正規化、媒體順序、檔名與 metadata |
| `network/dns.py`、`network/connect_proxy.py` | DNS/IP 政策與 extractor CONNECT proxy |
| `network/media_client.py` | 受限 upstream 媒體連線與讀取 |
| `security/tokens.py` | 有容量、TTL 與用途限制的記憶體 token store |
| `services/thumbnail.py`、`services/thumbnail_cache.py` | 受限縮圖生成、快取與併發協調 |
| `models.py`、`errors.py`、`logging_config.py` | 公開／私有資料模型、錯誤契約與安全日誌 |
| `static/index.html`、`static/app.js`、`static/styles.css` | 使用者介面、前端 queue、預覽與下載互動 |

主要流程：`POST /api/extractions` → URL 驗證 → extractor → 正規化 → token 核發 → 前端透過 `/api/media/{token}/preview` 或 `/api/media/{token}/download` 取得媒體。下載另有 `HEAD` 預檢；`GET /healthz` 不應存取外部平台。

修改時沿用依賴注入與既有分層。網路政策放在 `network/`，擷取格式相容性放在 `extractor/`，不要將各層邏輯堆入 route 或前端。

## 5. 必須維持的執行與資料邊界

- **單 process／單 worker：** token、限流與快取是 process-local；部署維持 `--workers 1`，不可直接增加 worker 或副本假設狀態共享。
- **受控對外連線：** 保留 HTTPS、host、port、DNS A/AAAA 與 IP 檢查，以及重新導向限制；不能為了通過某個 URL 放寬為任意網域或繞過 CONNECT proxy。
- **Extractor 隔離：** 保留 HOME、環境變數、proxy、plugins、使用者設定與 subprocess 資源限制。
- **公開／私有模型分離：** upstream URL、request headers 與憑證留在私有資料；不要序列化 `PrivateMediaRecord` 給前端。Preview/download token 必須維持用途綁定與期限。
- **Cookie 僅用於擷取：** 僅從 operator 掛載的對應平台唯讀檔讀取；不新增 UI/API Cookie 上傳，不把 Cookie 傳給 preview/download CDN。
- **日誌不洩漏敏感資料：** 不記錄完整 token 路徑、Cookie、request body、upstream media URL 或 raw extractor output。錯誤使用既有 `AppError` 與 `ErrorResponse` 契約。
- **串流可正確結束：** 修改下載時保留 timeout、byte limit、client disconnect、取消與清理行為，確保 upstream response 與併發 slot 被釋放；不得改成無界限地把完整檔案載入記憶體。
- **預覽有界限：** 優先使用可信 CDN poster，缺少時預設使用 `/placeholder.svg`。Generated preview 預設關閉；啟用時保留輸入／輸出大小、時間、併發及快取上限。
- **部署限制：** 維持 UID/GID 10001、唯讀 root filesystem、bounded tmpfs、loopback port 與無永久媒體 volume 的設計。

`secrets/` 與真實 Cookie 檔不是一般探索資料；不要將其內容加入程式、測試 fixture、文件或建置 context。平台實測依 `OPERATIONS.md` 使用 owner-controlled 內容。

## 6. 本機開發

使用 Python 3.12 與 `uv`；相依套件以 `pyproject.toml` 和 `uv.lock` 管理。

```bash
uv sync --extra dev
uv run uvicorn sns_media_list.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

介面位於 `http://127.0.0.1:8000`。啟動會使用 `127.0.0.1:8765` 作為內部 extractor proxy；避免同時啟動使用相同 port 的 instance。

前端沒有 Node.js 打包流程，直接修改 `static/`。保留繁體中文文案、鍵盤操作、可存取狀態，以及非同步請求取消與過期結果防護。

需要瀏覽器測試時先安裝 Chromium：

```bash
uv run playwright install chromium
```

## 7. 測試與驗證

先執行與變更相關的測試，再依影響範圍執行必要的整體檢查。以既有 fixture、fake extractor、注入式 client 和本機測試 server 驗證；不要讓一般測試依賴真實平台或 Cookie。

| 變更範圍 | 對應測試 |
| --- | --- |
| URL、模型、設定、token、extractor、網路與縮圖 | `tests/unit/` |
| API、錯誤、限流與串流生命週期 | `tests/api/` |
| 真實 HTTP 串流、下載及連線中斷 | `tests/integration/` |
| 前端互動、選取下載與批次 queue | `tests/browser/` |
| Docker／Compose／Nginx 契約 | `tests/container/` |
| README、操作指南與人工 smoke 工具 | `tests/test_readme.py`、`tests/test_operations_docs.py`、`tests/test_manual_smoke.py` |

常用檢查：

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python scripts/check_function_docstrings.py src tests scripts
```

Docstring checker 只檢查 Python 函數是否有 docstring；繁體中文內容與 JavaScript 函數註解仍須人工核對。瀏覽器測試可能因 Chromium 不可用而 skip，回報時應區分通過與跳過。

依變更追加檢查：

```bash
# gallery-dl 升級或 adapter／正規化／egress 變更
uv run python scripts/verify_gallery_contract.py

# 部署變更；container smoke 需要 Docker daemon，會操作測試容器
docker compose config --quiet
uv run python scripts/container_smoke.py

# Nginx 設定變更；使用本機 Nginx 或 Docker 驗證
uv run python scripts/check_nginx_config.py
```

相依套件或映像升級時，依 `OPERATIONS.md` 的升級及安全 gate 流程檢查 candidate image；`scripts/security_gate.py --image <candidate-image>` 需要對應映像與掃描工具。保留 pinned dependencies、映像 digest 及 `LICENSES/` notices，更新相依版本時同步維護 `uv.lock`。

真實平台 smoke 是部署特定的人工檢查；精確 Story 為短效、選用案例，不作為 CI 或 release gate。執行條件與敏感 URL 處理方式以 `OPERATIONS.md` 為準。

## 8. 完成交付

- 行為變更同步檢視相關測試與 `openspec/specs/`；使用者可見功能更新 `README.md`，設定與部署行為更新 `OPERATIONS.md`。
- 新增或修改環境變數時，同步確認 `config.py`、相關 Compose 設定、文件與測試；應用程式設定沿用 `SNS_MEDIA_` 前綴。
- 檢查 diff 是否只有本次任務所需內容，沒有憑證、暫存媒體、瀏覽器產物或無關 lockfile 變動。
- 最終回覆說明修改檔案、主要效果、實際執行的驗證及結果；若有未執行、跳過或失敗的檢查，清楚交代原因，不宣稱未驗證項目已通過。
