const form = document.querySelector('#extraction-form');
const input = document.querySelector('#post-url');
const button = document.querySelector('#analyze-button');
const singleModeButton = document.querySelector('#single-mode-button');
const batchModeButton = document.querySelector('#batch-mode-button');
const singleInputPanel = document.querySelector('#single-input-panel');
const batchInputPanel = document.querySelector('#batch-input-panel');
const batchInput = document.querySelector('#batch-post-urls');
const batchButton = document.querySelector('#analyze-batch-button');
const stopBatchButton = document.querySelector('#stop-batch-button');
const queueStatus = document.querySelector('#queue-status');
const queueProgress = document.querySelector('#queue-progress');
const queueItems = document.querySelector('#queue-items');
const status = document.querySelector('#status');
const results = document.querySelector('#results');
const resultGroups = document.querySelector('#result-groups');
const resultGroupNavigation = document.querySelector('#result-group-navigation');
const previousResultGroupButton = document.querySelector('#previous-result-group');
const nextResultGroupButton = document.querySelector('#next-result-group');
const resultGroupPosition = document.querySelector('#result-group-position');
const analysisWorkbench = document.querySelector('.analysis-workbench');
const analysisLoading = document.querySelector('#analysis-loading');
const LOCAL_PREVIEW_URL = '/placeholder.svg';
const PREVIEW_RETRY_DELAY_MS = 1000;
const BATCH_DOWNLOAD_DELAY_MS = 500;
const MAX_BATCH_URLS = 5;
let submittedUrl = '';
let extractionGeneration = 0;
let previewGeneration = 0;
let previewQueue = [];
let activePreview = null;
const previewRetryTimers = new Set();
let activeBatchRun = null;
let activeExtraction = null;
let activeResultGroupIndex = 0;

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

/** 依首次出現順序解析至多五個非空白 URL，並保留整批驗證錯誤。 */
function parseBatchUrls(value) {
  const urls = [];
  const seen = new Set();
  for (const line of value.split('\n')) {
    const url = line.trim();
    if (url && !seen.has(url)) {
      seen.add(url);
      urls.push(url);
    }
  }
  if (urls.length === 0) {
    return { urls: [], error: '請至少輸入 1 個 URL。' };
  }
  if (urls.length > MAX_BATCH_URLS) {
    return { urls: [], error: '最多只能分析 5 個 URL。' };
  }
  return { urls, error: '' };
}

/** 依目前 extraction 與 batch 狀態，統一鎖定或恢復可啟動分析的控制項。 */
function updateExtractionControls() {
  const locked = activeExtraction !== null || activeBatchRun !== null;
  button.disabled = locked;
  batchButton.disabled = locked;
  for (const recoveryButton of document.querySelectorAll('.group-recovery .re-analyze')) {
    recoveryButton.disabled = locked;
  }
}

/** 切換分析工作台的骨架與可存取忙碌狀態。 */
function setAnalysisLoading(loading) {
  analysisWorkbench.setAttribute('aria-busy', String(loading));
  analysisLoading.hidden = !loading;
}

/** 確認 callback 仍屬於目前可更新介面的批次執行。 */
function isCurrentBatchRun(run) {
  return activeBatchRun === run && extractionGeneration === run.generation;
}

/** 將 queue item 的內部狀態轉成不僅依賴顏色的可讀文字。 */
function queueStateLabel(state) {
  return {
    pending: '等待中',
    running: '執行中',
    success: '成功',
    error: '失敗',
    stopped: '已停止',
  }[state] || '等待中';
}

/** 以目前批次狀態更新可存取的整體進度與每筆生命週期。 */
function renderBatchQueue(run) {
  const completed = run.items.filter(item => item.state === 'success').length;
  const stopped = run.items.filter(item => item.state === 'stopped').length;
  queueStatus.hidden = false;
  queueProgress.textContent = `${completed} / ${run.items.length} 已完成，${stopped} 項已停止。`;
  queueItems.replaceChildren(...run.items.map((item, index) => {
    const row = document.createElement('li');
    row.className = 'queue-item';
    row.dataset.state = item.state;
    const url = document.createElement('span');
    url.className = 'queue-item-url';
    url.textContent = `${index + 1}. ${item.url}`;
    const state = document.createElement('strong');
    state.className = 'queue-item-state';
    state.textContent = queueStateLabel(item.state);
    row.append(url, state);
    return row;
  }));
}

/** 將目前 run 所有尚未開始的 queue item 安全標記為已停止。 */
function stopPendingBatchItems(run) {
  for (const item of run.items) {
    if (item.state === 'pending') {
      item.state = 'stopped';
    }
  }
  renderBatchQueue(run);
}

/** 在全頁同時只允許一個未完成的 extraction request，並保證完成後釋放控制項。 */
async function requestExtraction(url) {
  if (activeExtraction) {
    return null;
  }
  const request = {};
  activeExtraction = request;
  updateExtractionControls();
  try {
    const response = await fetch('/api/extractions', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!response.ok) {
      throw await readApiError(response);
    }
    return await response.json();
  } finally {
    if (activeExtraction === request) {
      activeExtraction = null;
      updateExtractionControls();
    }
  }
}

/** 切換單筆或批次輸入面板，並保留另一面板既有文字。 */
function setAnalysisMode(mode) {
  const single = mode === 'single';
  singleModeButton.setAttribute('aria-pressed', String(single));
  batchModeButton.setAttribute('aria-pressed', String(!single));
  singleInputPanel.hidden = !single;
  batchInputPanel.hidden = single;
  (single ? input : document.querySelector('#batch-post-urls')).focus();
}

/** 依序執行已解析的批次 URL，並保留每筆獨立生命週期與結果。 */
async function analyzeBatch() {
  if (activeExtraction || activeBatchRun) {
    return;
  }
  const parsed = parseBatchUrls(batchInput.value);
  if (parsed.error) {
    setStatus(parsed.error, 'error');
    return;
  }
  const run = {
    generation: ++extractionGeneration,
    stopped: false,
    items: parsed.urls.map(url => ({ url, state: 'pending' })),
  };
  activeBatchRun = run;
  updateExtractionControls();
  stopBatchButton.hidden = false;
  stopBatchButton.disabled = false;
  renderBatchQueue(run);
  setStatus('正在循序分析內容...', 'loading');
  setAnalysisLoading(true);
  try {
    for (const item of run.items) {
      if (!isCurrentBatchRun(run)) {
        return;
      }
      if (run.stopped) {
        stopPendingBatchItems(run);
        break;
      }
      item.state = 'running';
      renderBatchQueue(run);
      try {
        const payload = await requestExtraction(item.url);
        if (!isCurrentBatchRun(run)) {
          return;
        }
        if (payload) {
          item.state = 'success';
          renderResults(payload);
        } else {
          item.state = 'error';
        }
      } catch (_error) {
        if (!isCurrentBatchRun(run)) {
          return;
        }
        item.state = 'error';
      }
      renderBatchQueue(run);
      if (run.stopped) {
        stopPendingBatchItems(run);
        break;
      }
    }
    if (isCurrentBatchRun(run)) {
      setStatus('準備就緒，請選擇個別下載。', 'success');
    }
  } finally {
    setAnalysisLoading(false);
    if (activeBatchRun === run) {
      activeBatchRun = null;
      stopBatchButton.hidden = true;
      updateExtractionControls();
    }
  }
}

/** 停止目前批次中尚未開始的項目，並讓執行中的請求正常完成。 */
function stopBatch() {
  if (!activeBatchRun) {
    return;
  }
  activeBatchRun.stopped = true;
  stopBatchButton.disabled = true;
  stopPendingBatchItems(activeBatchRun);
}

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

/** 清除全部結果群組與其預覽工作，供單筆分析取代舊結果使用。 */
function clearResults() {
  cancelPreviewLoading();
  results.hidden = true;
  resultGroups.replaceChildren();
  activeResultGroupIndex = 0;
  updateResultGroupNavigation();
}

/** 依可見結果群組數量更新水平導覽按鈕與目前位置公告。 */
function updateResultGroupNavigation() {
  const groups = [...resultGroups.querySelectorAll('.result-group')];
  const total = groups.length;
  resultGroupNavigation.hidden = total < 2;
  if (total < 2) {
    resultGroupPosition.textContent = total ? '第 1/1 組' : '';
    return;
  }
  activeResultGroupIndex = Math.min(Math.max(activeResultGroupIndex, 0), total - 1);
  resultGroupPosition.textContent = `第 ${activeResultGroupIndex + 1}/${total} 組`;
  previousResultGroupButton.disabled = activeResultGroupIndex === 0;
  nextResultGroupButton.disabled = activeResultGroupIndex === total - 1;
}

/** 捲動至相鄰結果群組，讓鍵盤使用者能操作同一個水平軌道。 */
function moveResultGroup(offset) {
  const groups = [...resultGroups.querySelectorAll('.result-group')];
  const nextIndex = activeResultGroupIndex + offset;
  if (nextIndex < 0 || nextIndex >= groups.length) {
    return;
  }
  activeResultGroupIndex = nextIndex;
  groups[activeResultGroupIndex].scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'start' });
  updateResultGroupNavigation();
}

/** 依水平捲動後最接近軌道起點的群組更新位置摘要。 */
function syncResultGroupNavigation() {
  const groups = [...resultGroups.querySelectorAll('.result-group')];
  if (groups.length < 2) {
    return;
  }
  const trackLeft = resultGroups.getBoundingClientRect().left;
  activeResultGroupIndex = groups.reduce((nearestIndex, group, index) => (
    Math.abs(group.getBoundingClientRect().left - trackLeft)
      < Math.abs(groups[nearestIndex].getBoundingClientRect().left - trackLeft)
      ? index
      : nearestIndex
  ), 0);
  updateResultGroupNavigation();
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

/** 使單一群組的 preview 工作失效，不取消其他可見群組的佇列。 */
function invalidateGroupPreviews(group) {
  group.generation += 1;
  previewQueue = previewQueue.filter(entry => entry.group !== group);
  if (activePreview?.group === group) {
    activePreview.image.removeAttribute('src');
    activePreview = null;
  }
}

/** 將有效媒體秒數格式化為 MM:SS 或 HH:MM:SS。 */
function formatDuration(duration) {
  if (!Number.isFinite(duration) || duration < 0) {
    return '';
  }
  const totalSeconds = Math.round(duration);
  const totalHours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = String(totalSeconds % 60).padStart(2, '0');
  if (totalHours > 0) {
    return `${String(totalHours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${seconds}`;
  }
  return `${String(minutes).padStart(2, '0')}:${seconds}`;
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

/** 從正規化檔名取得安全且可顯示的副檔名格式。 */
function mediaFormat(filename) {
  const extension = filename.split('.').pop();
  return extension && /^[a-z0-9]+$/i.test(extension) ? extension.toUpperCase() : '';
}

/** 建立只包含有效 application-owned 資訊的結構化媒體 metadata。 */
function createMediaMetadata(media, index, payload) {
  const list = document.createElement('dl');
  list.className = 'media-metadata';
  const addRow = (label, value) => {
    if (!value) return;
    const term = document.createElement('dt');
    const detail = document.createElement('dd');
    term.textContent = label;
    detail.textContent = value;
    list.append(term, detail);
  };
  addRow('項目', String(index + 1));
  addRow('類型', media.media_type === 'video' ? '影片' : '圖片');
  addRow('格式', mediaFormat(media.filename));
  if (Number.isFinite(media.width) && media.width > 0 && Number.isFinite(media.height) && media.height > 0) {
    addRow('尺寸', `${media.width} × ${media.height}`);
  }
  addRow('長度', formatDuration(media.duration));
  addRow('平台', payload.platform === 'instagram' ? 'Instagram' : payload.platform === 'x' ? 'X' : '');
  addRow('作者', payload.author);
  addRow('檔名', media.filename);
  return list;
}

/** 嘗試複製正規化檔名，失敗時只顯示一般性錯誤。 */
async function copyFilename(filename, button) {
  try {
    await navigator.clipboard.writeText(filename);
    button.textContent = '已複製檔名';
  } catch (_error) {
    button.textContent = '無法複製檔名';
  }
  window.setTimeout(() => {
    button.textContent = '複製檔名';
  }, 2000);
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
    entry.groupGeneration === entry.group.generation &&
    entry.image.isConnected &&
    entry.group.root.isConnected &&
    entry.group.root.contains(entry.image) &&
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
function renderMediaCard(group, media, index) {
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
      groupGeneration: group.generation,
      group,
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

  const metadata = createMediaMetadata(media, index, group.payload);
  visual.append(metadata);

  const selectionLabel = document.createElement('label');
  selectionLabel.className = 'media-selection-label';
  const selection = document.createElement('input');
  selection.type = 'checkbox';
  selection.className = 'media-selection';
  selection.dataset.mediaIndex = String(index);
  selection.setAttribute('aria-label', `選取項目 ${String(index + 1).padStart(2, '0')}`);
  selection.addEventListener('change', () => {
    if (selection.checked) {
      group.selectedIndices.add(index);
    } else {
      group.selectedIndices.delete(index);
    }
    updateSelectionUi(group);
  });
  selectionLabel.append(selection, document.createTextNode(`選取項目 ${String(index + 1).padStart(2, '0')}`));
  visual.append(selectionLabel);

  const body = document.createElement('div');
  body.className = 'media-body';
  const filename = document.createElement('p');
  filename.className = 'media-filename';
  filename.textContent = media.filename;
  const copyButton = document.createElement('button');
  copyButton.type = 'button';
  copyButton.className = 'copy-filename';
  copyButton.textContent = '複製檔名';
  copyButton.addEventListener('click', () => copyFilename(media.filename, copyButton));
  const downloadButton = document.createElement('button');
  downloadButton.type = 'button';
  downloadButton.className = 'download-action';
  downloadButton.textContent = '下載';
  downloadButton.addEventListener('click', () => downloadMedia(group, media, downloadButton));
  body.append(filename, copyButton, downloadButton);
  card.append(visual, body);
  return card;
}

/** Render normalized extraction metadata and replace the previous card grid. */
/** 建立僅作用於單一結果群組的媒體選取與批次下載工具列。 */
function createSelectionToolbar(group) {
  const toolbar = document.createElement('div');
  toolbar.className = 'selection-toolbar';
  toolbar.setAttribute('aria-label', '媒體選取操作');
  const actions = [
    ['全選', () => true],
    ['取消全選', () => false],
    ['只選圖片', media => media.media_type === 'image'],
    ['只選影片', media => media.media_type === 'video'],
  ];
  for (const [label, action] of actions) {
    const actionButton = document.createElement('button');
    actionButton.type = 'button';
    actionButton.textContent = label;
    actionButton.addEventListener('click', () => {
      replaceSelection(group, action);
    });
    toolbar.append(actionButton);
  }
  const summary = document.createElement('p');
  summary.className = 'selection-summary';
  summary.setAttribute('role', 'status');
  summary.setAttribute('aria-live', 'polite');
  const downloadSelected = document.createElement('button');
  downloadSelected.type = 'button';
  downloadSelected.textContent = '下載選取項目';
  downloadSelected.addEventListener('click', () => downloadSelectedMedia(group));
  const downloadSummary = document.createElement('p');
  downloadSummary.className = 'download-summary';
  downloadSummary.setAttribute('role', 'status');
  downloadSummary.setAttribute('aria-live', 'polite');
  toolbar.append(summary, downloadSelected, downloadSummary);
  group.selectionSummary = summary;
  group.downloadSelectedButton = downloadSelected;
  group.downloadSummary = downloadSummary;
  return toolbar;
}

/** 在目標結果群組內公告安全的下載或處理狀態，不影響其他群組。 */
function setGroupStatus(group, message, state = 'info') {
  group.status.textContent = message;
  group.status.dataset.state = state;
}

/** 在目標結果群組內顯示經過對應的安全錯誤，不顯示原始 API 內容。 */
function setGroupError(group, message) {
  group.error.textContent = message;
  group.error.hidden = false;
}

/** 以群組本地的媒體索引集合更新選取控制項狀態。 */
function updateSelectionUi(group) {
  const selected = group.selectedIndices.size;
  group.selectionSummary.textContent = `${selected} / ${group.payload.media.length} 已選取`;
  group.downloadSelectedButton.disabled = selected === 0;
  for (const checkbox of group.root.querySelectorAll('.media-selection')) {
    checkbox.checked = group.selectedIndices.has(Number(checkbox.dataset.mediaIndex));
  }
}

/** 以媒體 predicate 取代單一結果群組的完整選取集合。 */
function replaceSelection(group, predicate) {
  group.selectedIndices = new Set(
    group.payload.media.flatMap((media, index) => (predicate(media) ? [index] : [])),
  );
  updateSelectionUi(group);
}

/** 在不讀取媒體 body 的前提下預檢 token 並啟動原生下載。 */
async function preflightAndStartDownload(media) {
  const preflight = await fetch(media.download_url, {
    method: 'HEAD',
    credentials: 'same-origin',
    cache: 'no-store',
  });
  if (!preflight.ok) {
    throw await readApiError(preflight);
  }
  const anchor = document.createElement('a');
  anchor.href = media.download_url;
  anchor.download = media.filename || 'media-file';
  anchor.rel = 'noreferrer';
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

/** 依來源順序安全啟動目前群組內被選取的多個下載。 */
async function downloadSelectedMedia(group) {
  const selectedMedia = group.payload.media.filter((_media, index) => group.selectedIndices.has(index));
  group.downloadSelectedButton.disabled = true;
  let started = 0;
  let unavailable = 0;
  let needsRecovery = false;
  for (const media of selectedMedia) {
    try {
      await preflightAndStartDownload(media);
      started += 1;
      await new Promise(resolve => window.setTimeout(resolve, BATCH_DOWNLOAD_DELAY_MS));
    } catch (error) {
      unavailable += 1;
      needsRecovery ||= error.code === 'token_expired' || error.code === 'token_not_found';
    }
  }
  const permissionNotice = selectedMedia.length > 1 ? '瀏覽器可能需要允許多檔下載。' : '';
  const summary = `${started} 個下載已開始，${unavailable} 個無法啟動。${permissionNotice}`;
  group.downloadSummary.textContent = summary;
  setGroupStatus(group, summary, started ? 'success' : 'error');
  if (needsRecovery) {
    offerGroupRecovery(group);
  }
  updateSelectionUi(group);
}

/** 顯示僅屬於指定群組的安全 token recovery 操作。 */
function offerGroupRecovery(group) {
  group.recovery.replaceChildren();
  const message = document.createElement('p');
  message.textContent = '部分下載參照已不可用。';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 're-analyze';
  button.textContent = '重新分析此項';
  button.addEventListener('click', () => reAnalyzeGroup(group, button));
  group.recovery.append(message, button);
}

/** 透過既有單筆 API 原地重新分析指定群組，不影響其他結果。 */
async function reAnalyzeGroup(group, recoveryButton) {
  if (activeExtraction || activeBatchRun) {
    return;
  }
  const recoveryGeneration = ++group.recoveryGeneration;
  recoveryButton.disabled = true;
  setGroupStatus(group, '正在重新分析此項...', 'loading');
  try {
    const payload = await requestExtraction(group.payload.post_url);
    if (!payload || group.recoveryGeneration !== recoveryGeneration) {
      return;
    }
    replaceResultGroup(group, payload);
    setStatus('準備就緒，請選擇個別下載。', 'success');
  } catch (error) {
    if (group.recoveryGeneration === recoveryGeneration) {
      setGroupStatus(group, '重新分析失敗。', 'error');
      setGroupError(group, error.message || '目前無法分析此內容。');
      setStatus(error.message || '目前無法分析此內容。', 'error');
    }
  }
}

/** 原地替換指定結果群組的 payload、選取狀態與 preview generation。 */
function replaceResultGroup(group, payload) {
  renderResults(payload, false, group);
}

/** 建立或原地更新一個獨立結果群組，並選擇性保留單筆流程的既有 DOM ID。 */
function renderResults(payload, isSingleResult = false, existingGroup = null) {
  const root = existingGroup?.root || document.createElement('section');
  const group = existingGroup || {
    selectedIndices: new Set(),
    root,
    generation: 0,
    recoveryGeneration: 0,
  };
  if (existingGroup) {
    invalidateGroupPreviews(group);
  }
  group.payload = payload;
  group.selectedIndices = new Set();
  root.className = 'result-group';
  root.replaceChildren();
  const heading = document.createElement('div');
  heading.className = 'results-heading';
  const details = document.createElement('div');
  const kicker = document.createElement('div');
  kicker.className = 'section-kicker';
  kicker.textContent = '02 / 結果';
  const platformLabel = document.createElement('p');
  platformLabel.className = 'eyebrow';
  platformLabel.textContent = payload.platform === 'instagram' ? 'Instagram 內容' : 'X 狀態貼文';
  const title = document.createElement('h2');
  title.textContent = '找到的媒體';
  const postDescription = document.createElement('p');
  postDescription.className = 'post-description';
  postDescription.textContent = payload.description || payload.author || '來源內容';
  const sourceLink = document.createElement('a');
  sourceLink.className = 'source-link';
  sourceLink.target = '_blank';
  sourceLink.rel = 'noreferrer noopener';
  sourceLink.textContent = '開啟原始內容';
  sourceLink.href = payload.post_url;
  const resultsSummary = document.createElement('p');
  resultsSummary.className = 'results-summary';
  resultsSummary.textContent = `${payload.media.length} 個媒體項目已準備就緒`;
  const unavailableWarning = document.createElement('p');
  unavailableWarning.className = 'warning';
  unavailableWarning.setAttribute('role', 'status');
  const mediaGrid = document.createElement('div');
  mediaGrid.className = 'media-grid';
  if (isSingleResult) {
    platformLabel.id = 'platform-label';
    postDescription.id = 'post-description';
    sourceLink.id = 'source-link';
    resultsSummary.id = 'results-summary';
    unavailableWarning.id = 'unavailable-warning';
    mediaGrid.id = 'media-grid';
  }
  details.append(kicker, platformLabel, title, postDescription);
  heading.append(details, sourceLink);
  const toolbar = createSelectionToolbar(group);
  const recovery = document.createElement('div');
  recovery.className = 'group-recovery';
  const groupStatus = document.createElement('p');
  groupStatus.className = 'group-status';
  groupStatus.setAttribute('role', 'status');
  groupStatus.setAttribute('aria-live', 'polite');
  const groupError = document.createElement('p');
  groupError.className = 'group-error';
  groupError.setAttribute('role', 'status');
  groupError.setAttribute('aria-live', 'polite');
  groupError.hidden = true;
  group.recovery = recovery;
  group.status = groupStatus;
  group.error = groupError;
  mediaGrid.replaceChildren(...payload.media.map((media, index) => renderMediaCard(group, media, index)));
  root.append(heading, resultsSummary, toolbar, recovery, groupStatus, groupError, unavailableWarning, mediaGrid);

  if (payload.unavailable_media_count > 0) {
    unavailableWarning.textContent = `${payload.unavailable_media_count} 個媒體項目無法轉換為可直接下載的檔案。`;
  } else {
    unavailableWarning.hidden = true;
  }
  if (!existingGroup) {
    resultGroups.append(root);
  }
  results.hidden = false;
  updateResultGroupNavigation();
  updateSelectionUi(group);
  pumpPreviewQueue();
  return group;
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

/** 分析目前 URL；批次項目保留既有群組，單筆分析取代全部舊結果。 */
async function analyze(event, preserveResults = false) {
  event?.preventDefault();
  if (activeExtraction || (activeBatchRun && !preserveResults)) {
    return;
  }
  const requestGeneration = ++extractionGeneration;
  submittedUrl = input.value.trim();
  if (!preserveResults) {
    clearResults();
  }
  setStatus('正在分析內容...', 'loading');
  setAnalysisLoading(true);
  try {
    const payload = await requestExtraction(submittedUrl);
    if (!payload) {
      return;
    }
    if (requestGeneration !== extractionGeneration) {
      return;
    }
    renderResults(payload, !preserveResults);
    setStatus('準備就緒，請選擇個別下載。', 'success');
  } catch (error) {
    if (requestGeneration !== extractionGeneration) {
      return;
    }
    const canReanalyze = error.code === 'token_expired' || error.code === 'token_not_found';
    setStatus(error.message || '目前無法分析此內容。', 'error', canReanalyze);
  } finally {
    setAnalysisLoading(false);
  }
}

/** Download a token-bound file and recover expired references inline. */
async function downloadMedia(group, media, downloadButton) {
  downloadButton.disabled = true;
  setGroupStatus(group, '正在啟動下載...', 'loading');
  try {
    await preflightAndStartDownload(media);
    setGroupStatus(group, '已開始下載。', 'success');
    setStatus('已開始下載。', 'success');
  } catch (error) {
    const canReanalyze = error.code === 'token_expired' || error.code === 'token_not_found';
    if (canReanalyze) {
      setGroupStatus(group, '無法啟動下載。', 'error');
      setGroupError(group, error.message || '無法完成下載。');
      setStatus(error.message || '無法完成下載。', 'error');
      offerGroupRecovery(group);
    } else {
      setGroupStatus(group, '無法啟動下載。', 'error');
      setGroupError(group, error.message || '無法完成下載。');
      setStatus(error.message || '無法完成下載。', 'error');
    }
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
singleModeButton.addEventListener('click', () => setAnalysisMode('single'));
batchModeButton.addEventListener('click', () => setAnalysisMode('batch'));
batchButton.addEventListener('click', analyzeBatch);
stopBatchButton.addEventListener('click', stopBatch);
previousResultGroupButton.addEventListener('click', () => moveResultGroup(-1));
nextResultGroupButton.addEventListener('click', () => moveResultGroup(1));
resultGroups.addEventListener('scroll', syncResultGroupNavigation);
input.focus();
