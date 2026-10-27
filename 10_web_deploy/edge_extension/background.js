importScripts("config.js");

const BRIDGE_PORT = 48272;
const TOKEN = String(globalThis.XIAORUO1_BRIDGE_TOKEN || "");
const WS_URL = `ws://127.0.0.1:${BRIDGE_PORT}/ws?token=${encodeURIComponent(TOKEN)}`;
const CONNECT_ALARM = "xiaoruo1-edge-bridge-connect";
const EXTENSION_VERSION = "1.8.4";
const READ_LOAD_GRACE_MS = 2500;
const READ_CONTENT_TIMEOUT_MS = 6000;
const READ_SCRIPT_TIMEOUT_MS = 8000;
const READ_FALLBACK_TIMEOUT_MS = 6000;
const READ_RECOVERY_LOAD_TIMEOUT_MS = 5000;
const READ_RECOVERY_MESSAGE_TIMEOUT_MS = 4000;
const READ_RECOVERY_SCRIPT_TIMEOUT_MS = 5000;
const OPEN_TAB_READABLE_WAIT_MS = 20000;
const READ_STARTUP_URL_WAIT_MS = 5000;
const ACTION_VERIFY_TIMEOUT_MS = 3000;
const ACTION_VERIFY_INTERVAL_MS = 100;
const DOWNLOAD_WAIT_TIMEOUT_MS = 12000;
const DOWNLOAD_POLL_INTERVAL_MS = 200;
const DOWNLOAD_LOCATOR_PATTERN = /download|export|save|pdb|zip|mol2|gro|下载|导出|保存/i;

let socket = null;
let connected = false;
let reconnectTimer = null;

// ChatGPT conversations can outlive an Edge restart, and more than one
// conversation can operate the same browser at once.  Chromium tab ids are
// not stable across a browser restart, so remember the last URL/title for
// every id we have returned.  If a later call presents an old id, we can
// rebind it only when there is one unambiguous matching tab; otherwise we
// return a bounded, actionable error instead of silently operating the wrong
// page.
const rememberedTabs = new Map();
const tabRedirects = new Map();
const MAX_REMEMBERED_TABS = 500;

function rememberTab(tab) {
  if (!tab || tab.id === undefined || tab.id === null) return;
  const key = String(tab.id);
  rememberedTabs.set(key, {
    tab_id: key,
    title: String(tab.title || ""),
    url: String(tab.url || ""),
    window_id: tab.windowId === undefined ? "" : String(tab.windowId)
  });
  while (rememberedTabs.size > MAX_REMEMBERED_TABS) {
    const first = rememberedTabs.keys().next().value;
    if (first === undefined) break;
    rememberedTabs.delete(first);
  }
}

function rememberTabs(tabs) {
  for (const tab of tabs || []) rememberTab(tab);
}

function isReadablePageUrl(url) {
  return /^(https?:|file:|about:blank$)/i.test(String(url || ""));
}

function isTransientStartupUrl(url) {
  return /^(?:about:blank$|(?:chrome|edge):\/\/newtab\/?$|(?:chrome|edge):\/\/new-tab-page\/?$)/i.test(String(url || ""));
}

async function waitForReadablePageUrl(id, timeout = READ_STARTUP_URL_WAIT_MS) {
  let current = await getTab(id);
  if (isReadablePageUrl(current.url) || !isTransientStartupUrl(current.url)) return current;
  const deadline = Date.now() + Math.max(250, Number(timeout || READ_STARTUP_URL_WAIT_MS));
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 100));
    current = await getTab(id);
    if (isReadablePageUrl(current.url) || !isTransientStartupUrl(current.url)) return current;
  }
  return current;
}

function sameTabUrl(left, right) {
  const a = String(left || "").trim();
  const b = String(right || "").trim();
  return Boolean(a && b && a === b);
}

async function resolveTab(value) {
  const requested = tabId(value);
  const requestedKey = String(requested);
  const previous = rememberedTabs.get(requestedKey);
  const redirected = tabRedirects.get(requestedKey);
  const candidatesToTry = redirected === undefined ? [requested] : [redirected, requested];
  let pageChangedError = null;
  for (const candidate of candidatesToTry) {
    try {
      const tab = await chrome.tabs.get(Number(candidate));
      // A Chromium id can be reused after a restart, and another conversation
      // can navigate a shared tab between calls.  Never silently treat a
      // different page as the old page; force the caller to list tabs and
      // choose an explicit current id instead.
      if (
        Number(candidate) === requested &&
        previous &&
        previous.url &&
        tab.url &&
        !sameTabUrl(tab.url, previous.url) &&
        !(isTransientStartupUrl(tab.url) && isReadablePageUrl(previous.url))
      ) {
        pageChangedError = new Error(
          `Edge 标签页 ${requestedKey} 的页面已变化（旧 URL: ${previous.url}；当前 URL: ${tab.url}）；` +
          `请重新调用 browser_list_tabs 后使用新的 tab_id`
        );
        break;
      }
      rememberTab(tab);
      if (Number(candidate) !== requested) {
        return {id: Number(candidate), tab, recovered_from_tab: requestedKey};
      }
      return {id: requested, tab, recovered_from_tab: ""};
    } catch (_) {
      // Try the remembered URL below when the original id is gone.
    }
  }

  if (pageChangedError) throw pageChangedError;

  if (!previous) {
    throw new Error(`Edge 标签页 ${requestedKey} 已失效；请先重新读取当前标签页列表`);
  }
  const tabs = await chrome.tabs.query({});
  rememberTabs(tabs);
  let matches = tabs.filter((tab) => sameTabUrl(tab.url, previous.url));
  if (matches.length > 1 && previous.title) {
    const titled = matches.filter((tab) => String(tab.title || "") === previous.title);
    if (titled.length) matches = titled;
  }
  if (matches.length !== 1) {
    const available = matches.slice(0, 5).map((tab) => ({
      tab_id: String(tab.id),
      title: String(tab.title || ""),
      url: String(tab.url || "")
    }));
    throw new Error(
      `Edge 标签页 ID ${requestedKey} 已失效，无法安全重绑定；` +
      `请重新调用 browser_list_tabs 后使用新的 tab_id` +
      (available.length ? `；同 URL 候选: ${JSON.stringify(available)}` : "")
    );
  }
  const recovered = matches[0];
  const recoveredKey = String(recovered.id);
  tabRedirects.set(requestedKey, Number(recovered.id));
  rememberTab(recovered);
  return {id: Number(recovered.id), tab: recovered, recovered_from_tab: requestedKey};
}

function setBadge(online) {
  connected = online;
  chrome.action.setBadgeText({text: online ? "ON" : "OFF"}).catch(() => {});
  chrome.action.setBadgeBackgroundColor({color: online ? "#15803d" : "#6b7280"}).catch(() => {});
}

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 2000);
}

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
  }
}

function reply(id, result, error) {
  if (error) {
    send({id, ok: false, error: String(error.message || error)});
  } else {
    send({id, ok: true, result});
  }
}

function tabId(value) {
  const id = Number(value);
  if (!Number.isInteger(id) || id < 0) throw new Error("Invalid Edge tab id");
  return id;
}

async function getTab(id) {
  const resolved = await resolveTab(id);
  return resolved.tab;
}

function downloadSummary(item) {
  if (!item) return null;
  return {
    id: Number(item.id),
    state: String(item.state || "unknown"),
    filename: String(item.filename || ""),
    file_path: String(item.filename || ""),
    url: String(item.url || ""),
    final_url: String(item.finalUrl || ""),
    mime: String(item.mime || ""),
    danger: String(item.danger || "safe"),
    exists: item.exists === undefined ? null : Boolean(item.exists),
    bytes_received: Number(item.bytesReceived || 0),
    total_bytes: Number(item.totalBytes || 0),
    start_time: String(item.startTime || ""),
    end_time: String(item.endTime || "")
  };
}

async function recentDownloadIds() {
  if (!chrome.downloads || typeof chrome.downloads.search !== "function") return new Set();
  try {
    const items = await chrome.downloads.search({orderBy: ["-startTime"], limit: 100});
    return new Set((items || []).map((item) => Number(item.id)));
  } catch (_) {
    return new Set();
  }
}

function downloadLikely(locator, before) {
  const values = [
    locator && locator.selector,
    locator && locator.name,
    before && before.page && before.page.url,
    before && before.target && before.target.id,
    before && before.target && before.target.text
  ];
  return DOWNLOAD_LOCATOR_PATTERN.test(values.filter(Boolean).join(" "));
}

async function waitForNewDownload(previousIds, timeout = DOWNLOAD_WAIT_TIMEOUT_MS) {
  if (!chrome.downloads || typeof chrome.downloads.search !== "function") return null;
  const known = previousIds || new Set();
  const deadline = Date.now() + Math.max(500, Number(timeout || DOWNLOAD_WAIT_TIMEOUT_MS));
  let latest = null;
  while (Date.now() <= deadline) {
    try {
      const items = await chrome.downloads.search({orderBy: ["-startTime"], limit: 100});
      const candidates = (items || [])
        .filter((item) => !known.has(Number(item.id)))
        .sort((a, b) => String(b.startTime || "").localeCompare(String(a.startTime || "")));
      if (candidates.length) {
        latest = candidates[0];
        const summary = downloadSummary(latest);
        if (latest.state === "complete" || latest.state === "interrupted") return summary;
      }
    } catch (_) {
      // Downloads can be queried while Chromium is updating the item. Keep
      // polling instead of turning a successful browser click into a failure.
    }
    await new Promise((resolve) => setTimeout(resolve, DOWNLOAD_POLL_INTERVAL_MS));
  }
  if (latest) {
    const summary = downloadSummary(latest);
    if (summary) summary.timed_out = true;
    return summary;
  }
  return null;
}

async function listDownloads(params) {
  if (!chrome.downloads || typeof chrome.downloads.search !== "function") {
    return {downloads: []};
  }
  const limit = Math.max(1, Math.min(100, Number(params && params.limit || 20)));
  const items = await chrome.downloads.search({orderBy: ["-startTime"], limit});
  return {downloads: (items || []).map(downloadSummary)};
}

async function waitForComplete(id, timeout) {
  const tab = await getTab(id);
  if (tab.status === "complete") return tab;
  return new Promise((resolve, reject) => {
    let timer = null;
    const finish = (fn, value) => {
      chrome.tabs.onUpdated.removeListener(listener);
      if (timer !== null) clearTimeout(timer);
      fn(value);
    };
    const listener = (updatedId, changeInfo, updatedTab) => {
      if (updatedId === tab.id && changeInfo.status === "complete") finish(resolve, updatedTab);
    };
    chrome.tabs.onUpdated.addListener(listener);
    timer = setTimeout(() => finish(reject, new Error("Edge 页面加载超时")), Math.max(1000, timeout));
  });
}

async function execute(tab, func, args = [], timeout = 15000) {
  const operation = chrome.scripting.executeScript({
    target: {tabId: tabId(tab)},
    func,
    args
  });
  let timer = null;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("Edge 页面脚本执行超时")), Math.max(1000, timeout));
  });
  try {
    const results = await Promise.race([operation, guard]);
    return results && results[0] ? results[0].result : undefined;
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

async function executeMain(tab, func, args = [], timeout = 15000) {
  const operation = chrome.scripting.executeScript({
    target: {tabId: tabId(tab)},
    world: "MAIN",
    func,
    args
  });
  let timer = null;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("Edge 页面主世界脚本执行超时")), Math.max(1000, timeout));
  });
  try {
    const results = await Promise.race([operation, guard]);
    return results && results[0] ? results[0].result : undefined;
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

function installDownloadSafetyHooks() {
  if (globalThis.__xiaoruo1DownloadSafetyInstalled) return {installed: true, reused: true};
  const urlObject = globalThis.URL;
  if (!urlObject || typeof urlObject.revokeObjectURL !== "function") return {installed: false};
  const originalRevoke = urlObject.revokeObjectURL.bind(urlObject);
  urlObject.revokeObjectURL = (value) => {
    // Some sites revoke a freshly-created Blob URL immediately after calling
    // anchor.click(). Edge can cancel the download before Chromium observes
    // it. Keep the URL alive long enough for the downloads subsystem to take
    // ownership, then revoke it normally.
    globalThis.setTimeout(() => {
      try { originalRevoke(value); } catch (_) {}
    }, 5000);
  };
  globalThis.__xiaoruo1DownloadSafetyInstalled = true;
  return {installed: true, reused: false};
}

async function executeAllFrames(tab, func, args = [], timeout = 20000) {
  const operation = chrome.scripting.executeScript({
    target: {tabId: tabId(tab), allFrames: true},
    func,
    args
  });
  let timer = null;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("Edge 页面多帧脚本执行超时")), Math.max(1000, timeout));
  });
  try {
    return await Promise.race([operation, guard]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

async function injectReader(tab, timeout = 3000) {
  const operation = chrome.scripting.executeScript({
    target: {tabId: tabId(tab)},
    files: ["reader.js"]
  });
  let timer = null;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("Edge 阅读器注入超时")), Math.max(1000, timeout));
  });
  try {
    await Promise.race([operation, guard]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

async function sendReaderMessage(id, params, timeout = READ_CONTENT_TIMEOUT_MS) {
  const operation = chrome.tabs.sendMessage(id, {
    type: "xiaoruo1_read_page",
    max_chars: params.max_chars,
    selector: params.selector,
    mode: params.mode
  });
  let timer = null;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("Edge 阅读器响应超时")), Math.max(1000, timeout));
  });
  try {
    const response = await Promise.race([operation, guard]);
    if (!response || !response.ok) {
      throw new Error(String(response && response.error || "Edge 阅读器没有返回页面内容"));
    }
    return response.result || {};
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

async function readViaContentScript(id, params) {
  try {
    return await sendReaderMessage(id, params);
  } catch (firstError) {
    // A stale renderer can leave tabs.sendMessage pending forever. Do not
    // spend another injection round on that same renderer; readPage() will
    // use the bounded duplicate-tab recovery below instead.
    if (String(firstError && firstError.message || firstError).includes("超时")) {
      throw firstError;
    }
    // Existing tabs may have been opened before the extension was reloaded.
    // Inject only the small message listener, then read through that stable
    // listener instead of running a large function in the page's world.
    try {
      await injectReader(id);
      return await sendReaderMessage(id, params);
    } catch (secondError) {
      throw new Error(`${String(firstError && firstError.message || firstError)}; inject: ${String(secondError && secondError.message || secondError)}`);
    }
  }
}

async function listTabs() {
  const tabs = await chrome.tabs.query({});
  rememberTabs(tabs);
  return {
    tabs: tabs.map((tab) => ({
      tab_id: String(tab.id),
      title: tab.title || "",
      url: tab.url || "",
      active: Boolean(tab.active),
      window_id: String(tab.windowId),
      incognito: Boolean(tab.incognito)
    }))
  };
}

async function openTab(params) {
  const url = params.url || "about:blank";
  const tab = await chrome.tabs.create({url, active: true});
  const id = tabId(tab && tab.id);
  rememberTab(tab);
  let settled = tab;
  // chrome.tabs.create can briefly return about:blank/newtab while the
  // requested HTTPS navigation is being committed. Returning that transient
  // URL makes the very next browser_read_page hit the security boundary even
  // though the visible tab soon becomes a normal webpage.
  if (isReadablePageUrl(url) && isTransientStartupUrl(tab && tab.url)) {
    try {
      settled = await waitForReadablePageUrl(id, OPEN_TAB_READABLE_WAIT_MS);
    } catch (_) {
      settled = tab;
    }
  }
  rememberTab(settled);
  return {
    tab_id: String(id),
    title: settled.title || "",
    url: settled.url || url
  };
}

async function navigate(params) {
  const resolved = await resolveTab(params.tab_id);
  const id = Number(resolved.id);
  const url = String(params.url || "");
  if (!/^https?:\/\//i.test(url)) throw new Error("Only HTTP(S) URLs are allowed");
  await chrome.tabs.update(id, {url});
  let tab = await waitForComplete(id, Number(params.timeout || 30000));
  try {
    tab = await waitForReadablePageUrl(id, Math.min(OPEN_TAB_READABLE_WAIT_MS, Number(params.timeout || 30000)));
  } catch (_) {
    // Preserve the completed navigation result; readPage will emit the
    // concrete protected-page diagnostic if the final URL is not readable.
  }
  rememberTab(tab);
  return {tab_id: String(id), title: tab.title || "", url: tab.url || url};
}

async function activeTabId() {
  const tabs = await chrome.tabs.query({active: true, lastFocusedWindow: true});
  if (!tabs.length || tabs[0].id === undefined) throw new Error("找不到当前活动浏览器标签页");
  rememberTab(tabs[0]);
  return tabId(tabs[0].id);
}

async function waitForSettle(id, settleMs) {
  const tab = await getTab(id);
  if (tab.status === "loading") {
    try {
      // Many SPAs keep the document in a loading state while background
      // requests, streaming responses, or analytics remain open. The DOM is
      // still readable, so use a short grace period and then continue with the
      // content that is already present instead of blocking for 30 seconds.
      await waitForComplete(id, READ_LOAD_GRACE_MS);
    } catch (_) {
      // A long-running SPA may never emit a final complete event. It is still
      // useful to read the DOM that is already visible.
    }
  }
  const delay = Math.max(0, Math.min(5000, Number(settleMs || 0)));
  if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
}

function readErrorMessage(error) {
  return String(error && error.message || error || "");
}

function isReadTimeoutError(error) {
  return /超时|timed out|timeout/i.test(readErrorMessage(error));
}

async function recoverReadFromDuplicate(originalId, params) {
  const original = await getTab(originalId);
  const targetUrl = String(original.url || "");
  if (!/^(https?:|file:)/i.test(targetUrl)) {
    throw new Error("当前页面类型不支持临时副本恢复");
  }

  const temporary = await chrome.tabs.create({url: targetUrl, active: false});
  const temporaryId = tabId(temporary && temporary.id);
  try {
    try {
      await waitForComplete(temporaryId, READ_RECOVERY_LOAD_TIMEOUT_MS);
    } catch (_) {
      // Some SPAs keep loading while their DOM is already readable.
    }

    const readerParams = {
      max_chars: params.max_chars,
      selector: params.selector,
      mode: params.mode
    };
    let result;
    try {
      result = await sendReaderMessage(temporaryId, readerParams, READ_RECOVERY_MESSAGE_TIMEOUT_MS);
    } catch (_) {
      // The content script can be absent when the temporary page is restored
      // from cache. Inject the tiny reader once, then use a short direct
      // fallback as a last resort.
      try {
        await injectReader(temporaryId, 2000);
        result = await sendReaderMessage(temporaryId, readerParams, READ_RECOVERY_MESSAGE_TIMEOUT_MS);
      } catch (_) {
        result = await execute(
          temporaryId,
          extractReadableDocument,
          [params.max_chars, params.selector, params.mode],
          READ_RECOVERY_SCRIPT_TIMEOUT_MS
        );
      }
    }
    if (!result || !String(result.text || "").trim()) {
      throw new Error("临时副本没有返回页面正文");
    }
    return {
      frames: [{frameId: 0, result}],
      recovery: "duplicate_tab"
    };
  } finally {
    try {
      await chrome.tabs.remove(temporaryId);
    } catch (_) {
      // The temporary tab may already have been closed by the browser.
    }
  }
}

function extractReadableDocument(limit, requestedSelector, requestedMode) {
  const max = Math.max(100, Math.min(100000, Number(limit || 20000)));
  const mode = ["auto", "visible", "fast"].includes(String(requestedMode || "auto"))
    ? String(requestedMode || "auto")
    : "auto";
  const hostname = String(location.hostname || "").toLowerCase();
  const isGitHub = hostname === "github.com" || hostname.endsWith(".github.com");
  const selector = typeof requestedSelector === "string" ? requestedSelector.trim().slice(0, 1000) : "";
  let root = null;
  let rootSelector = "";

  const collectInteractiveElements = () => {
    const visibleNow = (el) => {
      if (!el) return false;
      try {
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
      } catch (_) {
        return false;
      }
    };
    const compact = (value, limit = 160) => String(value || "").replace(/\\s+/g, " ").trim().slice(0, limit);
    const selectorFor = (el) => {
      const id = String(el.id || "").trim();
      if (id) {
        try { return "#" + CSS.escape(id); } catch (_) { return "#" + id.replace(/[^a-zA-Z0-9_-]/g, "\\\\$&"); }
      }
      const tag = String(el.tagName || "").toLowerCase();
      const name = String(el.getAttribute("name") || "").trim();
      if (name) return tag + "[name=\\\"" + name.replace(/\\\\/g, "\\\\\\\\").replace(/\\\"/g, "\\\\\\\"") + "\\\"]";
      return tag;
    };
    const elements = Array.from(document.querySelectorAll("input,textarea,button,select,a,summary,[role='button'],[role='link'],[role='textbox'],[contenteditable='true']"));
    return elements.filter(visibleNow).slice(0, 100).map((el, index) => {
      const tag = String(el.tagName || "").toLowerCase();
      const nativeRole = tag === "select" ? "combobox" : (tag === "button" || el.type === "submit" || el.type === "button" ? "button" : (tag === "a" ? "link" : ((tag === "input" && ["text", "search", "email", "password"].includes(String(el.type || "text"))) || tag === "textarea" || el.isContentEditable ? "textbox" : "")));
      const optionLabels = tag === "select" ? Array.from(el.options || []).slice(0, 100).map((option) => ({
        label: String(option.textContent || "").replace(/\s+/g, " ").trim().slice(0, 160),
        value: String(option.value || "").slice(0, 160),
        selected: Boolean(option.selected),
      })) : undefined;
      return {
        index,
        tag,
        id: String(el.id || ""),
        name: String(el.getAttribute("name") || ""),
        type: String(el.getAttribute("type") || ""),
        role: String(el.getAttribute("role") || nativeRole),
        aria_label: compact(el.getAttribute("aria-label")),
        placeholder: compact(el.getAttribute("placeholder")),
        text: compact(el.innerText || el.textContent),
        selector: selectorFor(el),
        disabled: Boolean(el.disabled),
        ...(optionLabels ? {options: optionLabels, selected_index: Number(el.selectedIndex), selected_value: String(el.value || "")} : {})
      };
    });
  };

  const pick = (value, label, minimumChars = 0) => {
    if (!value) return false;
    try {
      const size = String(value.textContent || "").trim().length;
      if (size >= minimumChars) {
        root = value;
        rootSelector = label;
        return true;
      }
    } catch (_) {}
    return false;
  };

  if (selector) {
    try { pick(document.querySelector(selector), selector, 1); } catch (_) {}
  }
  // GitHub pages are very large and innerText forces a full layout pass. The
  // main content root contains the useful account/repository text without the
  // application-wide navigation and is safe to read through textContent.
  if (!root && isGitHub) {
    for (const candidate of ["main", "[role='main']", "#js-pjax-container", "article"]) {
      try {
        if (pick(document.querySelector(candidate), candidate, 80)) break;
      } catch (_) {}
    }
  }
  if (!root) {
    for (const candidate of ["main", "[role='main']", "article"]) {
      try {
        if (pick(document.querySelector(candidate), candidate, 80)) break;
      } catch (_) {}
    }
  }
  if (!root) {
    root = document.body || document.documentElement;
    rootSelector = root === document.body ? "body" : "documentElement";
  }
  if (!root) return {text: "", truncated: false, extraction: "dom-text-fast", interactive_elements: []};

  let raw = "";
  let extraction = "dom-text-native";
  const useFast = mode === "fast" || (mode === "auto" && (isGitHub || root !== document.body));
  if (!useFast) {
    // innerText follows rendered visibility, but it can force expensive layout
    // on very large SPAs. It remains the default for ordinary pages.
    try { raw = String(root.innerText || ""); } catch (_) {}
  }
  if (useFast || !raw.trim()) {
    try {
      raw = String(root.textContent || "");
      extraction = "dom-text-fast";
    } catch (_) {}
  }

  const sourceLength = raw.length;
  // Bound normalization work before running regular expressions. This avoids
  // spending the script budget on megabytes of hidden SPA markup.
  raw = raw.slice(0, Math.max(max * 8, max));
  const text = raw
    .replace(/\u00a0/g, " ")
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t\f\v]+/g, " ")
    .replace(/\n[ \t]+/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return {
    text: text.slice(0, max),
    truncated: sourceLength > max || text.length > max,
    extraction,
    strategy: rootSelector,
    frame_url: String(location.href || ""),
    frame_title: String(document.title || ""),
    interactive_elements: collectInteractiveElements()
  };
}

function countFrames() {
  return {count: document.querySelectorAll ? document.querySelectorAll("iframe,frame").length : 0};
}

async function readPage(params) {
  const id = params.tab_id === undefined || params.tab_id === null || String(params.tab_id).trim() === ""
    ? await activeTabId()
    : Number((await resolveTab(params.tab_id)).id);
  let tabBeforeRead = await getTab(id);
  if (isTransientStartupUrl(tabBeforeRead.url)) {
    try {
      tabBeforeRead = await waitForReadablePageUrl(id, READ_STARTUP_URL_WAIT_MS);
    } catch (_) {
      // Fall through to the explicit page-type error below.
    }
  }
  const pageUrl = String(tabBeforeRead.url || "");
  if (!isReadablePageUrl(pageUrl)) {
    throw new Error("Edge 安全边界禁止读取此页面类型；请切换到普通 HTTP、HTTPS 或已允许文件访问的 file 页面");
  }
  const maxChars = Math.max(100, Math.min(100000, Number(params.max_chars || 20000)));
  const selector = typeof params.selector === "string" ? params.selector.trim().slice(0, 1000) : "";
  const mode = ["auto", "visible", "fast"].includes(String(params.mode || "auto")) ? String(params.mode || "auto") : "auto";
  await waitForSettle(id, params.settle_ms);
  let frames = [];
  let readerError = null;
  let recovery = "";
  try {
    const top = await readViaContentScript(id, {max_chars: maxChars, selector, mode});
    frames = [{frameId: 0, result: top}];
    if (params.include_frames) {
      try {
        const frameInfo = await execute(id, countFrames, [], 3000);
        if (frameInfo && Number(frameInfo.count || 0) > 0 && Number(frameInfo.count || 0) <= 8) {
          const all = await executeAllFrames(id, extractReadableDocument, [maxChars, selector, mode], READ_FALLBACK_TIMEOUT_MS);
          if (Array.isArray(all) && all.length) frames = all;
        }
      } catch (_) {
        // Keep the already extracted top document when a frame is protected.
      }
    }
  } catch (contentError) {
    readerError = contentError;
    if (isReadTimeoutError(contentError)) {
      try {
        const recovered = await recoverReadFromDuplicate(id, {
          max_chars: maxChars,
          selector,
          mode
        });
        frames = recovered.frames;
        recovery = recovered.recovery || "";
      } catch (recoveryError) {
        readerError = new Error(
          readErrorMessage(contentError) + "; recovery: " + readErrorMessage(recoveryError)
        );
      }
    }
  }
  if (!frames.length) try {
    const top = await execute(id, extractReadableDocument, [maxChars, selector, mode], READ_SCRIPT_TIMEOUT_MS);
    frames = [{frameId: 0, result: top}];
    const frameInfo = await execute(id, countFrames, [], 3000);
    if (params.include_frames && frameInfo && Number(frameInfo.count || 0) > 0 && Number(frameInfo.count || 0) <= 8) {
      try {
        const all = await executeAllFrames(id, extractReadableDocument, [maxChars, selector, mode], READ_FALLBACK_TIMEOUT_MS);
        if (Array.isArray(all) && all.length) frames = all;
      } catch (_) {
        // Keep the already extracted top document when a frame is protected.
      }
    }
  } catch (firstError) {
    // A page that is not GitHub may still expose a useful main content root
    // even when the first visible-layout pass is slow. Keep this fallback
    // bounded; if both injections fail, return the original reason instead of
    // spending another 30-60 seconds retrying the same page.
    try {
      const fallback = await execute(id, extractReadableDocument, [maxChars, "main", "fast"], READ_FALLBACK_TIMEOUT_MS);
      frames = [{frameId: 0, result: fallback}];
    } catch (fallbackError) {
      const reasons = [readerError, firstError, fallbackError]
        .filter(Boolean)
        .map((error) => readErrorMessage(error));
      throw new Error(reasons.join("; "));
    }
  }
  const ordered = (Array.isArray(frames) ? frames : []).filter((item) => item && item.result).sort((a, b) => Number(a.frameId || 0) - Number(b.frameId || 0));
  let remaining = maxChars;
  let text = "";
  let truncated = false;
  const frameSummaries = [];
  for (const frame of ordered) {
    const data = frame.result || {};
    const frameText = String(data.text || "");
    if (data.truncated) truncated = true;
    if (!frameText || remaining <= 0) continue;
    const prefix = Number(frame.frameId || 0) === 0 ? "" : `\n[iframe ${frame.frameId} ${data.frame_url || ""}]\n`;
    const piece = (prefix + frameText).slice(0, remaining);
    text += (text ? "\n" : "") + piece;
    remaining -= piece.length;
    if (piece.length < prefix.length + frameText.length) truncated = true;
    frameSummaries.push({
      frame_id: Number(frame.frameId || 0),
      url: data.frame_url || "",
      chars: frameText.length,
      strategy: data.strategy || ""
    });
  }
  const tab = await getTab(id);
  const primary = ordered.find((frame) => Number(frame.frameId || 0) === 0)?.result || {};
  const response = {
    tab_id: String(id),
    title: tab.title || "",
    url: tab.url || "",
    page_status: tab.status || "unknown",
    text,
    truncated,
    extraction: primary.extraction || "dom-text-native",
    strategy: primary.strategy || "",
    frames: frameSummaries,
    interactive_elements: Array.isArray(primary.interactive_elements) ? primary.interactive_elements : []
  };
  if (recovery) {
    response.recovery = recovery;
    response.recovered_from_tab = String(id);
  }
  return response;
}

// Normalize the locator at the bridge boundary. Older callers often supplied
// selector without by, or supplied a text name while leaving selector empty.
// Treat unmistakable CSS as CSS and use name as the text query so those calls
// cannot silently target the first link on the page.
function locatorLooksLikeCss(value) {
  return /[#.\[\]>+~*:=,]/.test(String(value || ""));
}

function normalizeLocator(params) {
  const source = {...(params || {})};
  const selector = typeof source.selector === "string" ? source.selector.trim() : "";
  const name = typeof source.name === "string" ? source.name.trim() : "";
  let by = String(source.by || "").trim().toLowerCase();
  if (!by) by = selector ? (locatorLooksLikeCss(selector) ? "css" : "role") : (name ? "text" : "role");
  if (by === "role" && selector && !name && locatorLooksLikeCss(selector)) by = "css";
  if (!["role", "text", "label", "placeholder", "css"].includes(by)) {
    throw new Error("不支持的元素定位方式: " + by);
  }
  const normalized = {...source, by, selector};
  if (["text", "label", "placeholder"].includes(by) && !normalized.selector && name) normalized.selector = name;
  if (by === "css" && !normalized.selector) throw new Error("CSS 定位需要 selector");
  if (["text", "label", "placeholder"].includes(by) && !normalized.selector) {
    throw new Error(by + " 定位需要 selector 或 name");
  }
  return normalized;
}

function actionStateInPage(locator, allowMissing = false) {
  const p = locator || {};
  const visibleNow = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const match = (value, wanted, exact) => {
    const left = String(value || "").replace(/\s+/g, " ").trim();
    const right = String(wanted || "").replace(/\s+/g, " ").trim();
    return exact ? left === right : left.toLowerCase().includes(right.toLowerCase());
  };
  const resolveCss = (query) => {
    try {
      return Array.from(document.querySelectorAll(query)).find(visibleNow) || null;
    } catch (_) {
      return null;
    }
  };
  const resolve = () => {
    const by = p.by || "role";
    const selector = p.selector || "";
    const exact = Boolean(p.exact);
    if (by === "css") {
      return resolveCss(selector);
    }
    if (by === "label") {
      const label = Array.from(document.querySelectorAll("label")).find((item) => visibleNow(item) && match(item.innerText, selector, exact));
      if (!label) return null;
      const target = label.htmlFor ? document.getElementById(label.htmlFor) : label.querySelector("input,textarea,select,[contenteditable='true']");
      return visibleNow(target) ? target : null;
    }
    if (by === "placeholder") {
      return Array.from(document.querySelectorAll("input,textarea,[contenteditable='true']")).find((item) => visibleNow(item) && match(item.getAttribute("placeholder"), selector, exact)) || null;
    }
    if (by === "text") {
      // Radix/React menus frequently render the visible label inside a
      // span/div without giving that node a button role.  Resolve the exact
      // label first, then promote it to the nearest actionable ancestor so a
      // text click really activates the menu item instead of clicking an
      // unrelated control or doing nothing.
      const candidates = Array.from(document.querySelectorAll("button,a,[role='button'],[role='link'],[role='menuitem'],[data-radix-collection-item],[onclick],summary,li,div,span"));
      const raw = candidates.find((item) => visibleNow(item) && match(item.innerText || item.textContent, selector || p.name, exact)) || null;
      if (!raw) return null;
      const actionable = raw.closest("[role='menuitem'],[data-radix-collection-item],button,a,[role='button'],[role='link'],[onclick]");
      return visibleNow(actionable) ? actionable : raw;
    }
    const role = selector || "button";
    let css = "[role=\"" + CSS.escape(role) + "\"]";
    if (role === "button") css += ",button,input[type='button'],input[type='submit'],input[type='reset']";
    if (role === "link") css += ",a";
    if (role === "textbox") css += ",input:not([type]),input[type='text'],input[type='search'],input[type='email'],input[type='password'],textarea,[contenteditable='true']";
    if (role === "combobox" || role === "listbox") css += ",select";
    return Array.from(document.querySelectorAll(css)).find((item) => visibleNow(item) && (!p.name || match(item.getAttribute("aria-label") || item.getAttribute("title") || item.innerText || item.value || item.getAttribute("placeholder"), p.name, exact))) || null;
  };
  const pageText = String(document.body && (document.body.innerText || document.body.textContent) || "").replace(/\s+/g, " ").slice(0, 60000);
  let pageHash = 2166136261;
  for (let i = 0; i < pageText.length; i += 1) {
    pageHash ^= pageText.charCodeAt(i);
    pageHash = Math.imul(pageHash, 16777619);
  }
  const element = resolve();
  if (!element && !allowMissing) throw new Error("找不到可操作的可见目标");
  const value = element && "value" in element ? String(element.value || "") : "";
  const text = element ? String(element.innerText || element.getAttribute("aria-label") || element.getAttribute("title") || "").replace(/\s+/g, " ").trim().slice(0, 200) : "";
  return {
    page: {url: String(location.href || ""), title: String(document.title || ""), text_length: pageText.length, text_hash: pageHash >>> 0},
    target: element ? {
      present: true,
      tag: element.tagName.toLowerCase(),
      id: element.id || "",
      text,
      value_length: value.length,
      class_name: String(element.className || "").slice(0, 300),
      aria_pressed: element.getAttribute("aria-pressed"),
      aria_expanded: element.getAttribute("aria-expanded"),
      disabled: Boolean(element.disabled),
      checked: "checked" in element ? Boolean(element.checked) : null,
      selected_index: "selectedIndex" in element ? Number(element.selectedIndex) : null
    } : {present: false}
  };
}

function clickInPage(locator) {
  const p = locator || {};
  const by = p.by || "role";
  const selector = p.selector || "";
  const exact = Boolean(p.exact);
  const visibleNow = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el); const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const match = (value, wanted) => {
    const left = String(value || "").replace(/\s+/g, " ").trim();
    const right = String(wanted || "").replace(/\s+/g, " ").trim();
    return exact ? left === right : left.toLowerCase().includes(right.toLowerCase());
  };
  const resolveCss = (query) => {
    try {
      return Array.from(document.querySelectorAll(query)).find(visibleNow) || null;
    } catch (_) {
      return null;
    }
  };
  const nameOf = (el) => String(el.getAttribute("aria-label") || el.getAttribute("title") || el.innerText || el.value || "").replace(/\s+/g, " ").trim();
  let element = null;
  if (by === "css") element = resolveCss(selector);
  if (by === "text") {
    // Include non-semantic menu labels and promote the matched label to its
    // actionable ancestor.  This is required by the ChatGPT plugin picker,
    // whose Xiaoruo1 row is a React/Radix container rather than a native
    // button in some builds.
    const candidates = Array.from(document.querySelectorAll("button,a,[role='button'],[role='link'],[role='menuitem'],[data-radix-collection-item],[onclick],summary,li,div,span"));
    const raw = candidates.find((item) => visibleNow(item) && match(item.innerText || item.textContent, selector || p.name)) || null;
    const actionable = raw && raw.closest("[role='menuitem'],[data-radix-collection-item],button,a,[role='button'],[role='link'],[onclick]");
    element = visibleNow(actionable) ? actionable : raw;
  }
  if (by === "role") {
    let css = "[role=\"" + CSS.escape(selector || "button") + "\"]";
    if ((selector || "button") === "button") css += ",button,input[type='button'],input[type='submit'],input[type='reset']";
    if (selector === "link") css += ",a";
    if (selector === "combobox" || selector === "listbox") css += ",select";
    element = Array.from(document.querySelectorAll(css)).find((item) => visibleNow(item) && (!p.name || match(nameOf(item), p.name)));
  }
  if (by === "label") {
    const label = Array.from(document.querySelectorAll("label")).find((item) => visibleNow(item) && match(item.innerText, selector));
    element = label ? (label.htmlFor ? document.getElementById(label.htmlFor) : label.querySelector("input,textarea,select,[contenteditable='true']")) : null;
  }
  if (by === "placeholder") element = Array.from(document.querySelectorAll("input,textarea,[contenteditable='true']")).find((item) => visibleNow(item) && match(item.getAttribute("placeholder"), selector));
  if (!visibleNow(element)) throw new Error("找不到可点击目标");
  element.scrollIntoView({block: "center", inline: "center"});
  const target = {address: element.tagName.toLowerCase(), id: element.id || "", text: String(element.innerText || element.getAttribute("aria-label") || "").slice(0, 200)};
  // React/SPA controls can listen for the real pointer sequence rather than
  // the convenience `.click()` method.  Dispatch the trusted-looking DOM
  // sequence first, then keep `.click()` as a compatibility fallback for
  // native controls and older pages.
  try { element.focus({preventScroll: true}); } catch (_) {}
  const eventInit = {bubbles: true, cancelable: true, view: window, detail: 1};
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
    try {
      const event = type.startsWith("pointer")
        ? new PointerEvent(type, { ...eventInit, pointerId: 1, isPrimary: true, pointerType: "mouse", buttons: type.endsWith("down") ? 1 : 0 })
        : new MouseEvent(type, eventInit);
      element.dispatchEvent(event);
    } catch (_) {}
  }
  element.click();
  return target;
}

function selectOptionInPage(locator) {
  const p = locator || {};
  const by = p.by || "css";
  const selector = p.selector || "";
  const exact = Boolean(p.exact);
  const wanted = String(p.option ?? "");
  const optionBy = String(p.option_by || "label").toLowerCase();
  const visibleNow = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el); const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const match = (left, right) => {
    const a = String(left || "").replace(/\s+/g, " ").trim(); const b = String(right || "").replace(/\s+/g, " ").trim();
    return exact ? a === b : a.toLowerCase().includes(b.toLowerCase());
  };
  const resolveCss = (query) => {
    try { return Array.from(document.querySelectorAll(query)).find((item) => visibleNow(item)) || null; } catch (_) { return null; }
  };
  const nameOf = (el) => String(el.getAttribute("aria-label") || el.getAttribute("title") || el.name || el.id || el.innerText || "").replace(/\s+/g, " ").trim();
  let element = null;
  if (by === "css") element = resolveCss(selector);
  if (by === "label") {
    const label = Array.from(document.querySelectorAll("label")).find((item) => visibleNow(item) && match(item.innerText, selector));
    element = label ? (label.htmlFor ? document.getElementById(label.htmlFor) : label.querySelector("select,[role='combobox'],[role='listbox']")) : null;
  }
  if (by === "role") {
    const role = selector || "combobox";
    const css = role === "combobox" || role === "listbox" ? "select,[role=\"" + CSS.escape(role) + "\"]" : "select";
    element = Array.from(document.querySelectorAll(css)).find((item) => visibleNow(item) && (!p.name || match(nameOf(item), p.name)));
  }
  if (by === "text") {
    element = Array.from(document.querySelectorAll("select,[role='combobox'],[role='listbox']")).find((item) => visibleNow(item) && match(nameOf(item), selector || p.name));
  }
  if (!visibleNow(element) || !element || String(element.tagName || "").toLowerCase() !== "select") {
    throw new Error("找不到可操作的原生 select/combobox");
  }
  element.scrollIntoView({block: "center", inline: "center"});
  try { element.focus({preventScroll: true}); } catch (_) {}
  const options = Array.from(element.options || []);
  let index = -1;
  if (optionBy === "index") {
    index = Number.parseInt(wanted, 10);
  } else {
    index = options.findIndex((item) => {
      if (optionBy === "value") return exact ? String(item.value) === wanted : String(item.value).toLowerCase().includes(wanted.toLowerCase());
      return match(item.textContent, wanted);
    });
  }
  if (!Number.isInteger(index) || index < 0 || index >= options.length) {
    throw new Error("找不到下拉选项: " + wanted);
  }
  const option = options[index];
  element.selectedIndex = index;
  try { element.dispatchEvent(new InputEvent("input", {bubbles: true, composed: true, inputType: "insertReplacementText", data: String(option.value || "")})); } catch (_) { element.dispatchEvent(new Event("input", {bubbles: true})); }
  element.dispatchEvent(new Event("change", {bubbles: true}));
  const verified = Number(element.selectedIndex) === index;
  if (!verified) throw new Error("下拉选项已发送，但选择状态校验失败");
  return {
    address: "select",
    id: element.id || "",
    selected_index: index,
    selected_value: String(element.value || ""),
    selected_label: String(option.textContent || "").replace(/\s+/g, " ").trim(),
    option_count: options.length,
    verified: true,
  };
}

function typeInPage(locator) {
  const p = locator || {};
  const by = p.by || "role";
  const selector = p.selector || "";
  const exact = Boolean(p.exact);
  const value = String(p.text || "");
  const visibleNow = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el); const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const match = (left, right) => {
    const a = String(left || "").replace(/\s+/g, " ").trim(); const b = String(right || "").replace(/\s+/g, " ").trim();
    return exact ? a === b : a.toLowerCase().includes(b.toLowerCase());
  };
  const resolveCss = (query) => {
    try {
      return Array.from(document.querySelectorAll(query)).find(visibleNow) || null;
    } catch (_) {
      return null;
    }
  };
  const nameOf = (el) => String(el.getAttribute("aria-label") || el.getAttribute("title") || el.innerText || el.value || el.getAttribute("placeholder") || "").replace(/\s+/g, " ").trim();
  let element = null;
  if (by === "css") element = resolveCss(selector);
  if (by === "placeholder") element = Array.from(document.querySelectorAll("input,textarea,[contenteditable='true']")).find((item) => visibleNow(item) && match(item.getAttribute("placeholder"), selector));
  if (by === "label") {
    const label = Array.from(document.querySelectorAll("label")).find((item) => visibleNow(item) && match(item.innerText, selector));
    element = label ? (label.htmlFor ? document.getElementById(label.htmlFor) : label.querySelector("input,textarea,select,[contenteditable='true']")) : null;
  }
  if (by === "role") {
    let css = "[role=\"" + CSS.escape(selector || "textbox") + "\"]";
    if ((selector || "textbox") === "textbox") css += ",input:not([type]),input[type='text'],input[type='search'],input[type='email'],input[type='password'],textarea,[contenteditable='true']";
    element = Array.from(document.querySelectorAll(css)).find((item) => visibleNow(item) && (!p.name || match(nameOf(item), p.name)));
  }
  if (!visibleNow(element)) throw new Error("找不到可输入目标");
  if (String(element.tagName || "").toLowerCase() === "select") {
    // This function is serialized and executed in the page world, so it
    // cannot call the separately declared selectOptionInPage helper. Keep a
    // small self-contained native-select path here for the legacy
    // browser_type API used by already-open web-chat conversations.
    if (element.disabled) throw new Error("下拉框当前不可用");
    const options = Array.from(element.options || []);
    const wanted = String(value || "");
    const optionIndex = options.findIndex((item) => {
      const label = String(item.textContent || "").replace(/\\s+/g, " ").trim();
      const optionValue = String(item.value || "");
      return match(label, wanted) || (exact ? optionValue === wanted : optionValue.toLowerCase().includes(wanted.toLowerCase()));
    });
    if (optionIndex < 0 || optionIndex >= options.length) throw new Error("找不到下拉选项: " + wanted);
    element.selectedIndex = optionIndex;
    try {
      element.dispatchEvent(new InputEvent("input", {bubbles: true, composed: true, inputType: "insertReplacementText", data: String(options[optionIndex].value || "")}));
    } catch (_) {
      element.dispatchEvent(new Event("input", {bubbles: true}));
    }
    element.dispatchEvent(new Event("change", {bubbles: true}));
    const actualValue = String(element.value || "");
    return {
      address: "select",
      id: element.id || "",
      length: wanted.length,
      value_length: actualValue.length,
      expected_length: wanted.length,
      selected_index: optionIndex,
      selected_value: actualValue,
      selected_label: String(options[optionIndex].textContent || "").replace(/\\s+/g, " ").trim(),
      verified: Number(element.selectedIndex) === optionIndex,
    };
  }
  element.focus();
  const current = element.isContentEditable ? String(element.textContent || "") : String(element.value || "");
  const expected = p.clear ? value : current + value;
  const dispatch = (event) => {
    try { element.dispatchEvent(event); } catch (_) {}
  };
  try {
    dispatch(new InputEvent("beforeinput", {bubbles: true, cancelable: true, composed: true, inputType: p.clear ? "deleteContent" : "insertText", data: value}));
  } catch (_) {}
  if (element.isContentEditable) {
    element.textContent = expected;
  } else {
    if (p.clear && typeof element.select === "function") {
      try { element.select(); } catch (_) {}
    }
    const proto = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(element, expected); else element.value = expected;
  }
  try {
    dispatch(new InputEvent("input", {bubbles: true, cancelable: false, composed: true, inputType: "insertText", data: value}));
  } catch (_) {
    dispatch(new Event("input", {bubbles: true}));
  }
  dispatch(new Event("change", {bubbles: true}));
  dispatch(new Event("keyup", {bubbles: true}));
  const actual = element.isContentEditable ? String(element.textContent || "") : String(element.value || "");
  return {address: element.tagName.toLowerCase(), id: element.id || "", length: value.length, value_length: actual.length, verified: actual === expected, expected_length: expected.length, previous_length: current.length};
}

function pressKeyInPage(locator) {
  const p = locator || {};
  const requested = String(p.key || "");
  if (!requested) throw new Error("缺少要按下的键");
  const visibleNow = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el); const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const match = (value, wanted) => {
    const left = String(value || "").replace(/\s+/g, " ").trim();
    const right = String(wanted || "").replace(/\s+/g, " ").trim();
    return Boolean(p.exact) ? left === right : left.toLowerCase().includes(right.toLowerCase());
  };
  let element = null;
  const by = p.by || "";
  const selector = p.selector || "";
  if (by === "css" && selector) {
    try { element = Array.from(document.querySelectorAll(selector)).find(visibleNow) || null; } catch (_) {}
  } else if (by === "placeholder") {
    element = Array.from(document.querySelectorAll("input,textarea,[contenteditable='true']")).find((item) => visibleNow(item) && match(item.getAttribute("placeholder"), selector));
  } else if (by === "role") {
    const role = selector || "textbox";
    element = Array.from(document.querySelectorAll("[role=\"" + CSS.escape(role) + "\"],input,textarea,[contenteditable='true']")).find((item) => visibleNow(item) && (!p.name || match(item.getAttribute("aria-label") || item.getAttribute("placeholder") || item.innerText, p.name)));
  }
  if (!element) {
    const active = document.activeElement;
    element = active && active !== document.body && visibleNow(active) ? active : null;
  }
  if (!element) throw new Error("找不到可接收键盘事件的可见目标");
  try { element.focus({preventScroll: true}); } catch (_) { try { element.focus(); } catch (_) {} }
  const aliases = {"Esc": "Escape", "Return": "Enter", "Down": "ArrowDown", "Up": "ArrowUp", "Left": "ArrowLeft", "Right": "ArrowRight", "Space": " "};
  const key = aliases[requested] || requested;
  const codeMap = {" ": "Space", "Enter": "Enter", "Escape": "Escape", "Tab": "Tab", "ArrowDown": "ArrowDown", "ArrowUp": "ArrowUp", "ArrowLeft": "ArrowLeft", "ArrowRight": "ArrowRight", "Backspace": "Backspace", "Delete": "Delete", "Home": "Home", "End": "End"};
  const code = codeMap[key] || (key.length === 1 ? "Key" + key.toUpperCase() : key);
  const printable = key.length === 1;
  const init = {key, code, bubbles: true, cancelable: true, composed: true, view: window, detail: 0, repeat: false, isComposing: false, which: key.charCodeAt(0) || 0, keyCode: key.charCodeAt(0) || 0, charCode: printable ? key.charCodeAt(0) : 0};
  const down = new KeyboardEvent("keydown", init);
  const downAccepted = element.dispatchEvent(down);
  if (printable) {
    try { element.dispatchEvent(new KeyboardEvent("keypress", {...init, charCode: key.charCodeAt(0)})); } catch (_) {}
  }
  const up = new KeyboardEvent("keyup", init);
  element.dispatchEvent(up);
  return {pressed: true, key, code, target: element.tagName.toLowerCase(), id: element.id || "", default_prevented: !downAccepted};
}

function actionStateChanged(before, after) {
  if (!before || !after) return "";
  const beforePage = before.page || {}; const afterPage = after.page || {};
  if (beforePage.url !== afterPage.url) return "url_changed";
  if (beforePage.title !== afterPage.title) return "title_changed";
  if (beforePage.text_hash !== afterPage.text_hash || beforePage.text_length !== afterPage.text_length) return "page_content_changed";
  const beforeTarget = before.target || {}; const afterTarget = after.target || {};
  if (beforeTarget.present !== afterTarget.present) return "target_presence_changed";
  for (const key of ["class_name", "aria_pressed", "aria_expanded", "disabled", "checked", "selected_index", "value_length"]) {
    if (beforeTarget[key] !== afterTarget[key]) return "target_state_changed";
  }
  return "";
}

async function waitForActionChange(id, locator, before) {
  const deadline = Date.now() + Math.min(ACTION_VERIFY_TIMEOUT_MS, Math.max(500, Number(locator.timeout || ACTION_VERIFY_TIMEOUT_MS)));
  let after = before;
  let lastError = "";
  while (Date.now() <= deadline) {
    await new Promise((resolve) => setTimeout(resolve, ACTION_VERIFY_INTERVAL_MS));
    try {
      after = await execute(id, actionStateInPage, [locator, true], 5000);
      const reason = actionStateChanged(before, after);
      if (reason) return {verified: true, reason, after};
    } catch (error) {
      lastError = String(error && error.message || error);
    }
  }
  return {verified: false, reason: "no_observable_change", after, lastError};
}

async function clickElement(params) {
  const id = Number((await resolveTab(params.tab_id)).id);
  const locator = normalizeLocator(params);
  const before = await execute(id, actionStateInPage, [locator, false], 5000);
  const watchDownload = downloadLikely(locator, before);
  const previousDownloadIds = watchDownload ? await recentDownloadIds() : null;
  if (watchDownload) {
    try {
      await executeMain(id, installDownloadSafetyHooks, [], 3000);
    } catch (_) {
      // Download observation below remains useful even when the page refuses
      // a main-world hook (for example a restricted browser page).
    }
  }
  let clickData = null;
  let clickError = null;
  try {
    // Run the interaction in the page's main world so React/SPA delegated
    // handlers observe the same DOM event realm as a real page interaction.
    // The isolated world remains the read/verification path.
    clickData = await executeMain(id, clickInPage, [locator], locator.timeout / 1000 + 5);
  } catch (error) {
    clickError = error;
  }
  const verification = await waitForActionChange(id, locator, before);
  const download = watchDownload ? await waitForNewDownload(previousDownloadIds) : null;
  const tab = await getTab(id);
  if (!verification.verified && !download) {
    const detail = clickError ? "；执行层错误：" + String(clickError.message || clickError) : "";
    throw new Error("点击已发送，但 " + ACTION_VERIFY_TIMEOUT_MS + "ms 内未观察到页面状态变化" + detail);
  }
  if (download && download.state === "interrupted") {
    throw new Error("点击已发送，但浏览器下载中断: " + String(download.error || download.state));
  }
  return {
    tab_id: String(id),
    clicked: true,
    verified: true,
    verification: verification.reason || (download ? "download_started" : ""),
    url: tab.url || "",
    target: clickData,
    after: verification.after,
    download: download || null
  };
}

async function typeText(params) {
  const id = Number((await resolveTab(params.tab_id)).id);
  const locator = normalizeLocator(params);
  // Run the page operation in the main world.  Native <select> controls and
  // SPA form handlers often live in the page's main JS world; executing the
  // same operation in the isolated extension world can change the DOM value
  // without delivering the event to the application's handler.
  let data = null;
  let typeError = null;
  try {
    data = await executeMain(id, typeInPage, [locator], locator.timeout / 1000 + 5);
  } catch (error) {
    typeError = error;
  }
  // Keep the legacy browser_type contract useful even when the serialized
  // typing function cannot resolve a native select in a particular page
  // world. The dedicated selector implementation is self-contained and is
  // already used by browser_select_option, so use it as a bounded fallback.
  if (!data || data.verified !== true) {
    try {
      data = await executeMain(
        id,
        selectOptionInPage,
        [{...locator, option: String(locator.text || ""), option_by: "label"}],
        locator.timeout / 1000 + 5,
      );
    } catch (_) {
      if (typeError) throw typeError;
    }
  }
  const tab = await getTab(id);
  if (!data || data.verified !== true) {
    throw new Error("输入已发送，但输入框内容校验失败（期望长度 " + (data?.expected_length ?? "?") + "，实际长度 " + (data?.value_length ?? "?") + "）");
  }
  return {tab_id: String(id), typed: true, verified: true, url: tab.url || "", target: data};
}

async function pressKey(params) {
  const id = Number((await resolveTab(params.tab_id)).id);
  const locator = normalizeLocator({...params, by: params.by || "", selector: params.selector || "", name: params.name || ""});
  locator.key = String(params.key || "");
  let data = null;
  try {
    data = await executeMain(id, pressKeyInPage, [locator], locator.timeout / 1000 + 5);
  } catch (error) {
    throw error;
  }
  return {tab_id: String(id), pressed: true, verified: true, url: (await getTab(id)).url || "", target: data};
}

async function selectOption(params) {
  const id = Number((await resolveTab(params.tab_id)).id);
  const locator = normalizeLocator(params);
  const before = await execute(id, actionStateInPage, [locator, false], 5000);
  const data = await executeMain(id, selectOptionInPage, [locator], locator.timeout / 1000 + 5);
  const after = await execute(id, actionStateInPage, [locator, true], 5000);
  const changed = actionStateChanged(before, after);
  if (!data || data.verified !== true || !changed) {
    throw new Error("下拉选项已发送，但选择状态校验失败");
  }
  return {
    tab_id: String(id),
    selected: true,
    verified: true,
    verification: changed,
    url: (await getTab(id)).url || "",
    target: data,
    after,
  };
}

async function waitTab(params) {
  const milliseconds = Math.max(0, Math.min(120000, Number(params.milliseconds || 0)));
  await new Promise((resolve) => setTimeout(resolve, milliseconds));
  const tab = await getTab(params.tab_id);
  rememberTab(tab);
  return {tab_id: String(tab.id), waited_ms: milliseconds, url: tab.url || ""};
}

async function screenshot(params) {
  const id = Number((await resolveTab(params.tab_id)).id);
  const previous = (await chrome.tabs.query({active: true, lastFocusedWindow: true}))[0] || null;
  const target = await getTab(id);
  if (target.windowId !== undefined) await chrome.windows.update(target.windowId, {focused: true});
  await chrome.tabs.update(id, {active: true});
  await new Promise((resolve) => setTimeout(resolve, 120));
  const dataUrl = await chrome.tabs.captureVisibleTab(target.windowId, {format: "png"});
  if (previous && previous.id !== id) {
    try {
      await chrome.tabs.update(previous.id, {active: true});
      await chrome.windows.update(previous.windowId, {focused: true});
    } catch (_) {}
  }
  return {tab_id: String(id), url: target.url || "", data_url: dataUrl, full_page: false};
}

async function closeTab(params) {
  const id = Number((await resolveTab(params.tab_id)).id);
  const tab = await getTab(id);
  await chrome.tabs.remove(id);
  return {tab_id: String(id), closed: true, title: tab.title || "", url: tab.url || ""};
}

const COMMANDS = {list_tabs: listTabs, list_downloads: listDownloads, open_tab: openTab, navigate, read_page: readPage, click: clickElement, type_text: typeText, press_key: pressKey, select_option: selectOption, wait: waitTab, screenshot, close_tab: closeTab};

async function handleCommand(message) {
  if (!message || !message.id || !message.method) return;
  const command = COMMANDS[message.method];
  if (!command) return reply(message.id, null, new Error(`Unknown Edge bridge command: ${message.method}`));
  try {
    const result = await command(message.params || {});
    reply(message.id, result, null);
  } catch (error) {
    reply(message.id, null, error);
  }
}

function connect() {
  if (!TOKEN || (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING))) return;
  try {
    const current = new WebSocket(WS_URL);
    socket = current;
    current.onopen = () => {
      setBadge(true);
      send({type: "hello_ack", client: "xiaoruo1-edge-extension", version: EXTENSION_VERSION});
    };
    current.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === "hello") return send({type: "hello_ack", client: "xiaoruo1-edge-extension", version: EXTENSION_VERSION});
        handleCommand(message);
      } catch (_) {}
    };
    current.onerror = () => {};
    current.onclose = () => {
      if (socket === current) socket = null;
      setBadge(false);
      scheduleReconnect();
    };
  } catch (_) {
    setBadge(false);
    scheduleReconnect();
  }
}

chrome.alarms.create(CONNECT_ALARM, {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === CONNECT_ALARM) connect(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
// Keep the recovery index current even when the page changes between tool
// calls.  Do not delete the snapshot on onRemoved: that snapshot is exactly
// what lets a later call detect an Edge restart and safely rebind an old id.
chrome.tabs.onUpdated.addListener((_tabId, _changeInfo, tab) => rememberTab(tab));
chrome.tabs.onRemoved.addListener(() => {});
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.action === "status") {
    sendResponse({connected, bridge: "127.0.0.1:48272"});
    return false;
  }
  if (message && message.action === "reconnect") {
    if (socket) try { socket.close(); } catch (_) {}
    socket = null;
    connect();
    sendResponse({ok: true});
    return false;
  }
  return false;
});

connect();