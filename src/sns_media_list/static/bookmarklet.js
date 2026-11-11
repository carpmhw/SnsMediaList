/** @typedef {{kind: 'none'} | {kind: 'valid', source: string} | {kind: 'invalid'}} SourceResult */

/**
 * 將可信的服務入口固定於書籤碼；執行時僅讀網址，以隔離的新分頁傳遞來源。
 * @param {string} entryUrl
 * @returns {string}
 */
export function buildBookmarklet(entryUrl) {
  const script = `void(window.open(${JSON.stringify(entryUrl)}+'#source='+encodeURIComponent(location.href),'_blank','noopener,noreferrer'))`;
  return 'javascript:' + encodeURIComponent(script);
}

/**
 * 一次性消費來源入口；先移除自有 fragment，再驗證未知輸入，不發出網路請求。
 * @param {Location} location
 * @param {History} history
 * @returns {SourceResult}
 */
export function consumeSourceFragment(location, history) {
  const fragment = location.hash.slice(1);
  const entries = fragment.split('&');
  if (!entries.some(/** 辨識精確的自有入口鍵，不碰普通錨點。 */
    (entry) => entry === 'source' || entry.startsWith('source='))) {
    return { kind: 'none' };
  }
  history.replaceState(history.state, '', location.pathname + location.search);
  if (entries.length !== 1 || !fragment.startsWith('source=')) {
    return { kind: 'invalid' };
  }

  let source;
  let parsed;
  try {
    source = decodeURIComponent(fragment.slice('source='.length));
    if (!source || source.length > 2048 || /[\u0000-\u0020\u007f\\#]/u.test(source)) {
      return { kind: 'invalid' };
    }
    parsed = new URL(source);
  } catch {
    return { kind: 'invalid' };
  }

  // 保留原始路徑供比對，避免 URL parser 將點號路徑或 authority 改寫成另一個目標。
  const target = source.match(/^https:\/\/([^/?#]+)([^?#]*)/i);
  if (!target || parsed.protocol !== 'https:' || parsed.username || parsed.password
      || parsed.port || !/^(?:www\.)?(?:instagram\.com|x\.com|twitter\.com)\.*(?::443)?$/i.test(target[1])) {
    return { kind: 'invalid' };
  }
  const hostname = parsed.hostname.replace(/\.+$/, '');
  const path = target[2];
  const parts = path.split('/').filter(Boolean);
  let supported = false;
  if (hostname === 'instagram.com' || hostname === 'www.instagram.com') {
    if (path.startsWith('/stories/')) {
      const story = path.match(/^\/stories\/([A-Za-z0-9._]{1,30})\/(?:(\d+)\/)?$/);
      if (story) {
        const username = story[1];
        supported = username.toLowerCase() !== 'highlights'
          && !username.startsWith('.') && !username.endsWith('.') && !username.includes('..')
          && (story[2] !== undefined || username.toLowerCase() !== 'me');
      }
    } else {
      supported = (parts.length === 2 && ['p', 'reel', 'tv'].includes(parts[0]))
        || (parts.length === 3 && ['p', 'reel', 'tv'].includes(parts[1]));
    }
  } else if (['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'].includes(hostname)) {
    supported = parts.length === 3 && /^\d+$/.test(parts[2])
      && (parts[1] === 'status' || (parts[0] === 'i' && parts[1] === 'web'));
  }
  return supported ? { kind: 'valid', source } : { kind: 'invalid' };
}
