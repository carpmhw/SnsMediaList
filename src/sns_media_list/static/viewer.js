/** 以單一 native dialog 管理原圖檢視、共享選取與可取消授權預檢。 */
export function createMediaViewer({ onSelection, onRecover, focusFallback }) {
  const dialog = document.querySelector('#media-viewer');
  const filename = document.querySelector('#viewer-filename');
  const kind = document.querySelector('#viewer-kind');
  const position = document.querySelector('#viewer-position');
  const content = document.querySelector('#viewer-content');
  const message = document.querySelector('#viewer-message');
  const closeButton = document.querySelector('#viewer-close');
  const previous = document.querySelector('#viewer-previous');
  const next = document.querySelector('#viewer-next');
  const selected = document.querySelector('#viewer-selected');
  const size = document.querySelector('#viewer-size');
  const play = document.querySelector('#viewer-play');
  const recovery = document.querySelector('#viewer-recovery');
  let current = null;
  let opener = null;
  let savedOverflow = '';

  /** 確認 callback 仍屬於目前有效的群組與媒體節點。 */
  function isCurrent(entry) {
    return current === entry && entry.group.generation === entry.groupGeneration
      && entry.group.root.isConnected && dialog.open;
  }

  /** 只從完整、同源 root-relative download path 建立檢視路徑。 */
  function viewUrl(value) {
    if (typeof value !== 'string') return null;
    const match = value.match(/^\/api\/media\/[A-Za-z0-9_-]+\/download$/);
    return match && match[0] === value ? value.slice(0, -8) + 'view' : null;
  }

  /** 使舊請求與媒體事件失效，解除來源且不影響已啟動下載。 */
  function releaseCurrent() {
    const old = current;
    current = null;
    if (old) {
      old.controller.abort();
      if (old.node) {
        old.node.onload = null;
        old.node.onerror = null;
        old.node.onloadeddata = null;
        if (old.node.tagName === 'VIDEO') old.node.pause();
        old.node.removeAttribute('src');
        if (old.node.tagName === 'VIDEO') old.node.load();
      }
    }
    content.replaceChildren();
    content.classList.remove('actual-size');
    content.scrollTop = 0;
    content.scrollLeft = 0;
  }

  /** 關閉並恢復背景捲動，將焦點交還有效入口或分析欄位。 */
  function close(restoreFocus = true) {
    const group = current?.group;
    const wasOpen = dialog.open;
    releaseCurrent();
    if (wasOpen) {
      dialog.close();
      document.body.style.overflow = savedOverflow;
      if (restoreFocus) {
        const target = opener?.isConnected ? opener : group?.root.querySelector('.media-view');
        if (target?.isConnected) target.focus();
        else focusFallback();
      }
    }
    opener = null;
  }

  /** 將授權或媒體失敗轉成固定訊息，不顯示來源 body 或例外。 */
  function showFailure(entry, status = 0) {
    if (!isCurrent(entry)) return;
    const expired = status === 404 || status === 410;
    message.textContent = expired ? '檢視參照已不可用，請重新分析此群組。'
      : status === 429 ? '服務目前忙碌中，請稍後再檢視。'
        : '目前無法檢視此媒體，請關閉後使用原有下載功能。';
    message.hidden = false;
    recovery.hidden = !expired;
    size.disabled = true;
    play.disabled = true;
    if (entry.node?.tagName === 'VIDEO') {
      entry.node.onerror = null;
      entry.node.onloadeddata = null;
      entry.node.pause();
      entry.node.removeAttribute('src');
      entry.node.load();
    }
  }

  /** 媒體 DOM 錯誤僅補做一次 token-only HEAD，不重送原始媒體。 */
  async function mediaError(entry) {
    if (!isCurrent(entry) || entry.errorChecked) return;
    entry.errorChecked = true;
    showFailure(entry);
    try {
      const response = await fetch(entry.url, {
        method: 'HEAD', credentials: 'same-origin', cache: 'no-store', signal: entry.controller.signal,
      });
      if (isCurrent(entry)) showFailure(entry, response.status === 204 ? 0 : response.status);
    } catch {
      if (isCurrent(entry)) showFailure(entry);
    }
  }

  /** 切換目前項目，先驗授權再顯示原圖，不預載相鄰項目。 */
  async function show(group, index) {
    releaseCurrent();
    const media = group.payload.media[index];
    const entry = {
      group, index, groupGeneration: group.generation, controller: new AbortController(),
      url: viewUrl(media.download_url), node: null, errorChecked: false,
    };
    current = entry;
    filename.textContent = media.filename;
    kind.textContent = media.media_type === 'video' ? '影片' : '圖片';
    position.textContent = `${index + 1} / ${group.payload.media.length}`;
    previous.disabled = index === 0;
    next.disabled = index === group.payload.media.length - 1;
    selected.checked = group.selectedIndices.has(index);
    size.hidden = media.media_type !== 'image';
    size.disabled = true;
    size.textContent = '原始大小';
    play.hidden = media.media_type !== 'video';
    play.disabled = true;
    recovery.hidden = true;
    recovery.disabled = false;
    message.textContent = '正在載入原始媒體…';
    message.hidden = false;
    if (!entry.url) {
      showFailure(entry);
      return;
    }
    try {
      const response = await fetch(entry.url, {
        method: 'HEAD', credentials: 'same-origin', cache: 'no-store', signal: entry.controller.signal,
      });
      if (!isCurrent(entry)) return;
      if (response.status !== 204) {
        showFailure(entry, response.status);
        return;
      }
      if (media.media_type !== 'image') {
        const video = document.createElement('video');
        video.controls = true;
        // 明確設定屬性，避免未支援 playsInline property 的引擎僅建立自訂欄位。
        video.setAttribute('playsinline', '');
        video.preload = 'none';
        video.setAttribute('aria-label', media.filename);
        const poster = media.preview_url;
        video.poster = typeof poster === 'string'
          && /^\/api\/media\/[A-Za-z0-9_-]+\/preview$/.test(poster) && poster.endsWith('/preview')
          ? poster : '/placeholder.svg';
        /** 只有目前播放器的事件能更新狀態，不自動載入或接續下一則。 */
        video.onloadeddata = () => { if (isCurrent(entry)) message.hidden = true; };
        video.onerror = () => mediaError(entry);
        entry.node = video;
        content.append(video);
        play.disabled = false;
        message.textContent = '按「播放影片」開始載入；不自動播放下一則。';
        return;
      }
      const image = document.createElement('img');
      image.alt = media.filename;
      entry.node = image;
      /** 只讓目前原圖的成功載入解除尺寸控制。 */
      image.onload = () => {
        if (isCurrent(entry)) {
          message.hidden = true;
          size.disabled = false;
        }
      };
      /** 舊圖片失敗不得改寫新項目的狀態。 */
      image.onerror = () => mediaError(entry);
      content.append(image);
      image.src = entry.url;
    } catch {
      if (isCurrent(entry)) showFailure(entry);
    }
  }

  /** 開啟指定群組項目，保存觸發入口並隔離背景互動。 */
  function open(group, index, trigger) {
    close(false);
    opener = trigger;
    savedOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.showModal();
    closeButton.focus();
    void show(group, index);
  }

  /** 邊界不循環、不跨群組的前後導覽。 */
  function move(offset) {
    if (!current) return;
    const { group, index } = current;
    const target = index + offset;
    if (target >= 0 && target < group.payload.media.length) void show(group, target);
  }

  /** 同步唯一選取來源，不建立檢視器私有的選取集合。 */
  function sync(group) {
    if (current?.group === group) selected.checked = group.selectedIndices.has(current.index);
  }

  /** 只有正在檢視的群組失效時才關閉。 */
  function invalidate(group) {
    if (current?.group === group) close();
  }

  /** 控制列動作明確分開，不攔截原生輸入或媒體控制。 */
  closeButton.addEventListener('click', () => close());
  previous.addEventListener('click', () => move(-1));
  next.addEventListener('click', () => move(1));
  selected.addEventListener('change', () => {
    if (current) onSelection(current.group, current.index, selected.checked);
  });
  size.addEventListener('click', () => {
    const actual = content.classList.toggle('actual-size');
    size.textContent = actual ? '適合視窗' : '原始大小';
    content.scrollTop = 0;
    content.scrollLeft = 0;
  });
  /** 原始 src 與 play 都在同一次使用者操作中設定，避免自動播放。 */
  play.addEventListener('click', () => {
    const entry = current;
    if (!entry?.node || !isCurrent(entry)) return;
    entry.node.src = entry.url;
    play.hidden = true;
    entry.node.focus();
    entry.node.play().catch(() => mediaError(entry));
  });
  recovery.addEventListener('click', () => {
    const group = current?.group;
    close();
    if (group) onRecover(group, recovery);
  });
  /** 原生 Esc 與 backdrop 都經過同一清理流程。 */
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.addEventListener('close', () => { if (!dialog.open && current) close(); });
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right
      || event.clientY < rect.top || event.clientY > rect.bottom) close();
  });
  dialog.addEventListener('keydown', event => {
    if (event.key === 'Tab') {
      const controls = [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), video[controls]')]
        .filter(control => control.getClientRects().length > 0);
      const target = event.shiftKey ? controls.at(-1) : controls[0];
      if ((!event.shiftKey && event.target === controls.at(-1))
        || (event.shiftKey && event.target === controls[0])) {
        event.preventDefault();
        target?.focus();
      }
      return;
    }
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey
      || event.target.closest('video, input, textarea, select, [contenteditable]')) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      move(event.key === 'ArrowLeft' ? -1 : 1);
    }
  });
  return { open, close, sync, invalidate };
}
