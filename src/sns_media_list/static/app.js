const form = document.querySelector('#extraction-form');
const input = document.querySelector('#post-url');
const button = document.querySelector('#analyze-button');
const status = document.querySelector('#status');
const results = document.querySelector('#results');
const platformLabel = document.querySelector('#platform-label');
const postDescription = document.querySelector('#post-description');
const sourceLink = document.querySelector('#source-link');
const resultsSummary = document.querySelector('#results-summary');
const unavailableWarning = document.querySelector('#unavailable-warning');
const mediaGrid = document.querySelector('#media-grid');
const LOCAL_PREVIEW_URL = '/placeholder.svg';
const PREVIEW_RETRY_DELAY_MS = 1000;
let submittedUrl = '';
let extractionGeneration = 0;
let previewGeneration = 0;
let previewQueue = [];
let activePreview = null;
const previewRetryTimers = new Set();

const ERROR_MESSAGES = {
  invalid_url: '請輸入 HTTPS Instagram 貼文、Reel、單則 Story 或 X 狀態貼文 URL。',
  unsupported_url: '僅支援 Instagram 貼文、Reel、單則 Story 與 X 狀態貼文 URL；帳號目前全部 Stories 與 Highlights 不支援。',
  post_unavailable: '此貼文無法使用、已刪除，或目前帳號無法讀取。',
  story_unavailable: '此 Story 目前無法使用。',
  no_media: '此內容沒有可直接串流的媒體。',
  extraction_limit_exceeded: '此內容的媒體數量超過服務可列出的上限。',
  request_too_large: '提交的請求太大，請只貼上一個內容 URL。',
  unsupported_media_type: '請使用未壓縮的 JSON 請求提交內容 URL。',
  local_rate_limited: '服務目前忙碌中，請稍候再試。',
  upstream_rate_limited: '平台暫時限制存取，請稍後再試。',
  platform_authentication_failed: '平台驗證工作階段無法使用，請聯絡服務管理者。',
  capacity_exceeded: '暫存結果容量已滿，請稍後重新分析。',
  extraction_failed: '目前無法分析此內容。',
  extraction_timeout: '平台回應時間過長，請稍後再試。',
  upstream_media_invalid: '其中一個媒體資源不是有效的可下載檔案。',
  token_expired: '下載參照已過期，請重新分析內容以建立新的參照。',
  token_not_found: '下載參照已不可用，請重新分析內容。',
};

/** Update the inline status region without exposing raw API details. */
function setStatus(message, state = 'info', canReanalyze = false) {
  status.replaceChildren();
  status.dataset.state = state;
  status.hidden = false;

  const messageNode = document.createElement('span');
  messageNode.textContent = message;
  status.append(messageNode);

  if (canReanalyze) {
    const lineBreak = document.createElement('br');
    const recoveryButton = document.createElement('button');
    recoveryButton.type = 'button';
    recoveryButton.className = 're-analyze';
    recoveryButton.textContent = '重新分析';
    recoveryButton.addEventListener('click', reAnalyze);
    status.append(lineBreak, recoveryButton);
  }
}

/** Hide and empty the previous extraction result before a new request. */
function clearResults() {
  cancelPreviewLoading();
  results.hidden = true;
  mediaGrid.replaceChildren();
  unavailableWarning.hidden = true;
  unavailableWarning.textContent = '';
  postDescription.textContent = '';
  sourceLink.textContent = '';
  sourceLink.removeAttribute('href');
}

/** Cancel all preview work belonging to the currently displayed result. */
function cancelPreviewLoading() {
  previewGeneration += 1;
  previewQueue = [];
  if (activePreview) {
    activePreview.image.removeAttribute('src');
    activePreview = null;
  }
  for (const timer of previewRetryTimers) {
    window.clearTimeout(timer);
  }
  previewRetryTimers.clear();
}

/** Format a positive media duration as a compact human-readable value. */
function formatDuration(duration) {
  if (!Number.isFinite(duration) || duration < 0) {
    return '';
  }
  const totalSeconds = Math.round(duration);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, '0');
  return `${minutes}:${seconds}`;
}

/** Build safe metadata text for one media card. */
function formatMediaMeta(media) {
  const values = [];
  if (media.width && media.height) {
    values.push(`${media.width} x ${media.height}`);
  }
  const duration = formatDuration(media.duration);
  if (duration) {
    values.push(duration);
  }
  return values.length ? values.join(' / ') : '直接串流檔案';
}

/** Create a local visual fallback for media without a raster preview. */
function createFallbackTile(media) {
  const fallback = document.createElement('div');
  fallback.className = 'fallback-tile';
  const text = document.createElement('div');
  const title = document.createElement('strong');
  title.textContent = media.media_type === 'video' ? '影片' : '媒體';
  const detail = document.createElement('span');
  detail.textContent = '找不到預覽。請下載原始檔案。';
  text.append(title, detail);
  fallback.append(text);
  return fallback;
}

/** Check whether a queued preview still belongs to the visible extraction result. */
function isCurrentPreviewEntry(entry) {
  return (
    entry.generation === previewGeneration &&
    entry.image.isConnected &&
    mediaGrid.contains(entry.image) &&
    entry.visual.contains(entry.image)
  );
}

/** Replace an exhausted preview with the local non-network fallback tile. */
function replacePreviewWithFallback(entry) {
  if (!isCurrentPreviewEntry(entry)) {
    return;
  }
  entry.visual.replaceChildren(createFallbackTile({ media_type: entry.mediaType }));
}

/** Queue one retry after the minimum delay advertised by local rate limits. */
function schedulePreviewRetry(entry) {
  const timer = window.setTimeout(() => {
    previewRetryTimers.delete(timer);
    if (!isCurrentPreviewEntry(entry)) {
      return;
    }
    previewQueue.push(entry);
    pumpPreviewQueue();
  }, PREVIEW_RETRY_DELAY_MS);
  previewRetryTimers.add(timer);
}

/** Finish one preview attempt and advance the bounded queue. */
function completePreviewAttempt(entry, loaded) {
  if (!isCurrentPreviewEntry(entry)) {
    return;
  }
  if (activePreview !== entry) {
    if (!loaded) {
      replacePreviewWithFallback(entry);
    }
    return;
  }
  activePreview = null;
  if (loaded) {
    pumpPreviewQueue();
    return;
  }
  if (entry.attempts < 2) {
    schedulePreviewRetry(entry);
  } else {
    replacePreviewWithFallback(entry);
  }
  pumpPreviewQueue();
}

/** Start the next valid opaque preview while keeping one request active. */
function pumpPreviewQueue() {
  if (activePreview || previewQueue.length === 0) {
    return;
  }
  const entry = previewQueue.shift();
  if (!isCurrentPreviewEntry(entry)) {
    pumpPreviewQueue();
    return;
  }
  activePreview = entry;
  entry.attempts += 1;
  entry.image.src = entry.url;
}

/** Create one ordered media card and queue only its opaque preview URL. */
function renderMediaCard(media, index) {
  const card = document.createElement('article');
  card.className = 'media-card';

  const visual = document.createElement('div');
  visual.className = 'media-visual';
  if (media.preview_url === LOCAL_PREVIEW_URL) {
    const image = document.createElement('img');
    image.src = LOCAL_PREVIEW_URL;
    image.alt = `${media.media_type === 'video' ? '影片' : '圖片'} ${index + 1} 預覽`;
    image.dataset.mediaType = media.media_type;
    visual.append(image);
  } else if (media.preview_url) {
    const image = document.createElement('img');
    image.alt = `${media.media_type === 'video' ? '影片' : '圖片'} ${index + 1} 預覽`;
    image.dataset.mediaType = media.media_type;
    visual.append(image);
    const entry = {
      generation: previewGeneration,
      image,
      mediaType: media.media_type,
      url: media.preview_url,
      visual,
      attempts: 0,
    };
    image.addEventListener('load', () => completePreviewAttempt(entry, true));
    image.addEventListener('error', () => completePreviewAttempt(entry, false));
    previewQueue.push(entry);
  } else {
    visual.append(createFallbackTile(media));
  }

  const body = document.createElement('div');
  body.className = 'media-body';
  const indexLabel = document.createElement('p');
  indexLabel.className = 'media-index';
  indexLabel.textContent = `項目 ${String(index + 1).padStart(2, '0')}`;
  const title = document.createElement('h3');
  title.className = 'media-title';
  title.textContent = media.media_type === 'video' ? '影片' : '圖片';
  const metadata = document.createElement('p');
  metadata.className = 'media-meta';
  metadata.textContent = formatMediaMeta(media);
  const downloadButton = document.createElement('button');
  downloadButton.type = 'button';
  downloadButton.className = 'download-action';
  downloadButton.textContent = '下載';
  downloadButton.dataset.downloadUrl = media.download_url;
  downloadButton.dataset.filename = media.filename;
  downloadButton.addEventListener('click', downloadMedia);
  body.append(indexLabel, title, metadata, downloadButton);
  card.append(visual, body);
  return card;
}

/** Render normalized extraction metadata and replace the previous card grid. */
function renderResults(payload) {
  platformLabel.textContent = payload.platform === 'instagram' ? 'Instagram 內容' : 'X 狀態貼文';
  postDescription.textContent = payload.description || payload.author || '來源內容';
  sourceLink.textContent = '開啟原始內容';
  sourceLink.href = payload.post_url;
  resultsSummary.textContent = `${payload.media.length} 個媒體項目已準備就緒`;
  mediaGrid.replaceChildren(...payload.media.map(renderMediaCard));

  if (payload.unavailable_media_count > 0) {
    unavailableWarning.textContent = `${payload.unavailable_media_count} 個媒體項目無法轉換為可直接下載的檔案。`;
    unavailableWarning.hidden = false;
  } else {
    unavailableWarning.hidden = true;
  }
  results.hidden = false;
  pumpPreviewQueue();
}

/** Convert a stable API error response into a safe browser Error. */
async function readApiError(response) {
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    payload = {};
  }
  const code = response.headers.get('x-sns-error-code') || payload.code || 'request_failed';
  const error = new Error(ERROR_MESSAGES[code] || '無法完成請求。');
  error.code = code;
  return error;
}

/** Submit the current content URL and replace the result state. */
async function analyze(event) {
  event?.preventDefault();
  const requestGeneration = ++extractionGeneration;
  submittedUrl = input.value.trim();
  button.disabled = true;
  clearResults();
  setStatus('正在分析內容...', 'loading');
  try {
    const response = await fetch('/api/extractions', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url: submittedUrl }),
    });
    if (requestGeneration !== extractionGeneration) {
      return;
    }
    if (!response.ok) {
      throw await readApiError(response);
    }
    const payload = await response.json();
    if (requestGeneration !== extractionGeneration) {
      return;
    }
    renderResults(payload);
    setStatus('準備就緒，請選擇個別下載。', 'success');
  } catch (error) {
    if (requestGeneration !== extractionGeneration) {
      return;
    }
    const canReanalyze = error.code === 'token_expired' || error.code === 'token_not_found';
    setStatus(error.message || '目前無法分析此內容。', 'error', canReanalyze);
  } finally {
    if (requestGeneration === extractionGeneration) {
      button.disabled = false;
    }
  }
}

/** Download a token-bound file and recover expired references inline. */
async function downloadMedia(event) {
  const downloadButton = event.currentTarget;
  downloadButton.disabled = true;
  try {
    const preflight = await fetch(downloadButton.dataset.downloadUrl, {
      method: 'HEAD',
      credentials: 'same-origin',
      cache: 'no-store',
    });
    if (!preflight.ok) {
      throw await readApiError(preflight);
    }
    const anchor = document.createElement('a');
    anchor.href = downloadButton.dataset.downloadUrl;
    anchor.download = downloadButton.dataset.filename || 'media-file';
    anchor.rel = 'noreferrer';
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setStatus('已開始下載。', 'success');
  } catch (error) {
    const canReanalyze = error.code === 'token_expired' || error.code === 'token_not_found';
    setStatus(error.message || '無法完成下載。', 'error', canReanalyze);
  } finally {
    downloadButton.disabled = false;
  }
}

/** Re-submit the last URL after a token recovery action. */
function reAnalyze() {
  input.value = submittedUrl;
  form.requestSubmit();
}

form.addEventListener('submit', analyze);
input.focus();
