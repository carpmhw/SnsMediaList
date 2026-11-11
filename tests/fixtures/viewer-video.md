# 原始媒體檢視影片 fixture

`viewer-video.webm` 是本專案自行產生的約四秒、320×180、無音訊 VP8／WebM 測試影片，不含任何第三方或真實平台素材。

產生方式：Playwright Chromium 的 Canvas 以 `#101314` 背景與 `#edb37c` 移動方塊繪圖，`canvas.captureStream(15)`，每 50ms 更新方塊位置；使用 `MediaRecorder`（`video/webm;codecs=vp8`，100000 bits/s）錄製 4000ms，停止後保存完整 Blob。保留瀏覽器輸出的容器、duration 與索引，不修改編碼 bytes。此檔案用於真正播放／定位驗證，不可替換成 signature-only fixture。
