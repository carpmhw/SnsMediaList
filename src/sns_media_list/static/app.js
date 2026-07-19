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
let submittedUrl = '';

const ERROR_MESSAGES = {
  invalid_url: '請輸入 HTTPS Instagram 貼文/Reel 或 X 狀態貼文 URL。',
  unsupported_url: '僅支援 Instagram 貼文/Reel 與 X 狀態貼文 URL。',
  post_unavailable: '此貼文無法使用、已刪除，或目前帳號無法讀取。',
  no_media: '此貼文沒有找到可直接串流的媒體。',
  extraction_limit_exceeded: '此貼文的媒體數量超過服務可列出的上限。',
  local_rate_limited: '服務目前忙碌中，請稍候再試。',
  upstream_rate_limited: '平台暫時限制存取，請稍後再試。',
  platform_authentication_failed: '平台驗證工作階段無法使用，請聯絡服務管理者。',
  capacity_exceeded: '暫存結果容量已滿，請稍後重新分析。',
  extraction_failed: '目前無法分析此貼文。',
  extraction_timeout: '平台回應時間過長，請稍後再試。',
  upstream_media_invalid: '其中一個媒體資源不是有效的可下載檔案。',
  token_expired: '下載參照已過期，請重新分析貼文以建立新的參照。',
  token_not_found: '下載參照已不可用，請重新分析貼文。',
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
  results.hidden = true;
  mediaGrid.replaceChildren();
  unavailableWarning.hidden = true;
  unavailableWarning.textContent = '';
  postDescription.textContent = '';
  sourceLink.textContent = '';
  sourceLink.removeAttribute('href');
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

/** Replace a broken preview with a local non-network fallback tile. */
function handlePreviewError(event) {
  const image = event.currentTarget;
  const visual = image.closest('.media-visual');
  if (visual) {
    visual.replaceChildren(createFallbackTile({ media_type: image.dataset.mediaType }));
  }
}

/** Create one ordered media card from the public API model. */
function renderMediaCard(media, index) {
  const card = document.createElement('article');
  card.className = 'media-card';

  const visual = document.createElement('div');
  visual.className = 'media-visual';
  if (media.preview_url) {
    const image = document.createElement('img');
    image.src = media.preview_url;
    image.alt = `${media.media_type === 'video' ? '影片' : '圖片'} ${index + 1} 預覽`;
    image.dataset.mediaType = media.media_type;
    image.addEventListener('error', handlePreviewError);
    visual.append(image);
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
  platformLabel.textContent = payload.platform === 'instagram' ? 'INSTAGRAM 貼文' : 'X 貼文';
  postDescription.textContent = payload.description || payload.author || '公開貼文';
  sourceLink.textContent = '開啟原始貼文';
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
}

/** Convert a stable API error response into a safe browser Error. */
async function readApiError(response) {
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    payload = {};
  }
  const error = new Error(ERROR_MESSAGES[payload.code] || '無法完成請求。');
  error.code = payload.code || 'request_failed';
  return error;
}

/** Submit the current post URL and replace the result state. */
async function analyze(event) {
  event?.preventDefault();
  submittedUrl = input.value.trim();
  button.disabled = true;
  clearResults();
  setStatus('正在分析貼文...', 'loading');
  try {
    const response = await fetch('/api/extractions', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url: submittedUrl }),
    });
    if (!response.ok) {
      throw await readApiError(response);
    }
    renderResults(await response.json());
    setStatus('準備就緒，請選擇個別下載。', 'success');
  } catch (error) {
    const canReanalyze = error.code === 'token_expired' || error.code === 'token_not_found';
    setStatus(error.message || '目前無法分析此貼文。', 'error', canReanalyze);
  } finally {
    button.disabled = false;
  }
}

/** Download a token-bound file and recover expired references inline. */
async function downloadMedia(event) {
  const downloadButton = event.currentTarget;
  downloadButton.disabled = true;
  try {
    const response = await fetch(downloadButton.dataset.downloadUrl, { credentials: 'same-origin' });
    if (!response.ok) {
      throw await readApiError(response);
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = downloadButton.dataset.filename || 'media-file';
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
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
