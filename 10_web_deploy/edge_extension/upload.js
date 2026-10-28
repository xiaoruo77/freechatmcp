// File selection over the existing authenticated bridge. No debugger, cookies,
// local browser profile access, or native client is involved.
const uploadOperations = new Map();
const uploadTargets = new Map();
const UPLOAD_MAX_BYTES = 32 * 1024 * 1024;

async function uploadWithinBudget(operation, deadline) {
  let timer = null;
  try {
    return await Promise.race([operation, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("upload_timeout_before_selection")), Math.max(1, deadline - Date.now()));
    })]);
  } finally { if (timer !== null) clearTimeout(timer); }
}

function uploadAllowedUrl(value) {
  try {
    const url = new URL(value);
    return (url.protocol === "https:" && url.hostname === "member.bilibili.com" &&
      url.pathname.startsWith("/platform/upload/") && (!url.port || url.port === "443") && !url.username && !url.password) ||
      (["http://127.0.0.1:48272", "https://mcp.example.com"].includes(url.origin) && url.pathname === "/upload-test/fixture.html" && !url.search && !url.hash);
  } catch (_) { return false; }
}

// Self-contained: chrome.scripting serializes this function into the isolated
// world. The observations are owned by our reader, not by page JS.
function inspectUploadTargetInPage(selector) {
  const match = /^input\[type="file"\]\[data-xiaoruo1-upload-id="([a-f0-9-]{36})"\]$/.exec(String(selector || ""));
  if (!match) return {matches: 0, error: "upload_selector_not_observed"};
  const observed = globalThis.__xiaoruo1UploadObservation;
  const entry = observed && observed.entries.get(match[1]);
  if (!entry) return {matches: 0, error: "upload_selector_not_observed"};
  const nodes = document.querySelectorAll(selector);
  if (nodes.length !== 1 || nodes[0] !== entry.element) return {matches: nodes.length, error: "upload_target_ambiguous"};
  const input = nodes[0];
  if (observed.url !== String(location.href) || !input.isConnected || Date.now() - entry.observed_at > 900000)
    return {matches: 1, error: "upload_observation_stale"};
  if (input.disabled) return {matches: 1, error: "upload_target_disabled"};
  if (input.files && input.files.length) return {matches: 1, error: "upload_target_already_selected"};
  return {matches: 1, document_id: observed.document_id, frame_url: String(location.href), accept: String(input.accept || "")};
}

// A File created from authenticated bytes is a real browser File, unlike a
// typed filesystem path. Only the observed control receives the new FileList.
async function setUploadFileInPage(payload) {
  let assignmentAttempted = false;
  try {
  const tokenMatch = /^input\[type="file"\]\[data-xiaoruo1-upload-id="([a-f0-9-]{36})"\]$/.exec(String(payload.selector || ""));
  if (!tokenMatch) throw new Error("upload_selector_not_observed");
  const observed = globalThis.__xiaoruo1UploadObservation;
  const entry = observed && observed.entries.get(tokenMatch[1]);
  const nodes = document.querySelectorAll(payload.selector);
  if (!entry || nodes.length !== 1 || nodes[0] !== entry.element) throw new Error("upload_target_ambiguous");
  const input = nodes[0];
  if (observed.document_id !== payload.document_id || observed.url !== String(location.href) ||
      !input.isConnected || Date.now() - entry.observed_at > 900000) throw new Error("upload_observation_stale");
  if (input.disabled || input.type !== "file") throw new Error("upload_target_disabled");
  if (input.files && input.files.length) throw new Error("upload_target_already_selected");
  if (Date.now() >= payload.deadline) throw new Error("upload_timeout_before_selection");
  const decoded = atob(payload.data_base64);
  const bytes = new Uint8Array(decoded.length);
  for (let index = 0; index < decoded.length; index++) bytes[index] = decoded.charCodeAt(index);
  if (bytes.byteLength !== payload.size || bytes.byteLength > 32 * 1024 * 1024) throw new Error("upload_size_mismatch");
  const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), (byte) => byte.toString(16).padStart(2, "0")).join("");
  if (sha256 !== payload.sha256) throw new Error("upload_hash_mismatch");
  if (Date.now() >= payload.deadline) throw new Error("upload_timeout_before_selection");
  // Digest awaits may let a SPA replace the input. Recheck immediately before
  // this operation's only side effect; do not retry after an uncertain result.
  const current = document.querySelectorAll(payload.selector);
  if (!input.isConnected || String(location.href) !== observed.url || current.length !== 1 || current[0] !== input)
    throw new Error("upload_observation_stale");
  if (input.disabled || (input.files && input.files.length)) throw new Error("upload_target_changed_before_selection");
  const file = new File([bytes], payload.name, {type: payload.mime_type, lastModified: 0});
  const transfer = new DataTransfer();
  transfer.items.add(file);
  assignmentAttempted = true;
  input.files = transfer.files;
  const selected = input.files && input.files[0];
  if (!selected || input.files.length !== 1 || selected.name !== payload.name || selected.size !== payload.size)
    throw new Error("upload_selection_verification_failed");
  input.dispatchEvent(new Event("input", {bubbles: true, composed: true}));
  input.dispatchEvent(new Event("change", {bubbles: true, composed: true}));
  return {
    selected_file: {name: selected.name, size: selected.size, type: selected.type, sha256},
    selection_observed_before_change: true,
    file_selected: true,
    stages: {file_selected: true, upload_started: null, server_received: null, platform_processed: null},
    verification: "FileList name/size and SHA256 verified before input/change; platform status requires a subsequent page read",
    document_id: observed.document_id,
    frame_url: observed.url
  };
  } catch (error) {
    const code = /^upload_[a-z_]+$/.test(String(error && error.message)) ? String(error.message) : "upload_selection_failed";
    return {file_selected:false, action_may_have_run:assignmentAttempted, error:{code},
      stages:{file_selected:assignmentAttempted ? null : false, upload_started:null, server_received:null, platform_processed:null}};
  }
}

async function uploadFile(params) {
  let dispatched = false;
  try {
  if (BROWSER_FAMILY !== "edge") throw new Error("upload_requires_edge");
  if (!params || params.confirm !== true) throw new Error("upload_confirmation_required");
  if (!Number.isSafeInteger(params.timeout) || params.timeout < 1000 || params.timeout > 120000) throw new Error("upload_timeout_invalid");
  const deadline = Date.now() + params.timeout;
  if (!params || !/^[A-Za-z0-9_.:-]{8,128}$/.test(String(params.idempotency_key || ""))) throw new Error("upload_idempotency_key_required");
  const tab = await uploadWithinBudget(chrome.tabs.get(tabId(params.tab_id)), deadline);
  if (!tab || String(tab.url) !== String(params.expected_url) || !uploadAllowedUrl(tab.url)) throw new Error("upload_page_not_allowed_or_changed");
  if (!Number.isSafeInteger(params.size) || params.size < 1 || params.size > UPLOAD_MAX_BYTES) throw new Error("upload_size_not_allowed");
  if (!/^[a-f0-9]{64}$/.test(String(params.sha256 || ""))) throw new Error("upload_hash_required");
  if (!/^[^\\/:\x00-\x1f]+\.(mp4|jpg|jpeg|png|webm)$/i.test(String(params.name || ""))) throw new Error("upload_filename_not_allowed");
  const expectedMime = {mp4:"video/mp4",webm:"video/webm",jpg:"image/jpeg",jpeg:"image/jpeg",png:"image/png"}[params.name.split(".").pop().toLowerCase()];
  if (params.mime_type !== expectedMime) throw new Error("upload_mime_mismatch");
  if (typeof params.data_base64 !== "string" || params.data_base64.length > Math.ceil(UPLOAD_MAX_BYTES / 3) * 4 ||
      !/^[A-Za-z0-9+/]*={0,2}$/.test(params.data_base64)) throw new Error("upload_bytes_invalid");
  if (!/^input\[type="file"\]\[data-xiaoruo1-upload-id="[a-f0-9-]{36}"\]$/.test(String(params.selector || ""))) throw new Error("upload_selector_not_observed");
  const key = String(params.idempotency_key);
  const fingerprint = JSON.stringify([String(tab.id), params.expected_url, params.selector, params.name, params.size, params.mime_type, params.sha256]);
  const previous = uploadOperations.get(key);
  if (previous) {
    if (previous.fingerprint !== fingerprint) throw new Error("upload_idempotency_conflict");
    if (previous.result) return {...previous.result, duplicate: true};
    throw new Error("upload_outcome_unknown_do_not_repeat");
  }
  // Never evict a previous operation and permit an old request to repeat.
  // The server also durably records dispatches across extension restarts.
  if (uploadOperations.size >= 500) throw new Error("upload_operation_limit_refresh_session_required");
  const inspected = await uploadWithinBudget(executeAllFrames(tab.id, inspectUploadTargetInPage, [params.selector], Math.min(params.timeout, 10000)), deadline);
  const targets = (inspected || []).filter((item) => item.result && item.result.matches > 0);
  if (targets.length !== 1) throw new Error(targets.length > 1 ? "upload_target_ambiguous" : "upload_selector_not_observed");
  const target = targets[0];
  if (target.result.error) throw new Error(target.result.error);
  if (new URL(target.result.frame_url).origin !== new URL(tab.url).origin) throw new Error("upload_cross_origin_frame_denied");
  const accepted = String(target.result.accept || "").split(",").map((value) => value.trim().toLowerCase()).filter(Boolean);
  const extension = "." + params.name.split(".").pop().toLowerCase();
  const mime = String(params.mime_type || "").toLowerCase();
  if (accepted.length && !accepted.some((value) => value === extension || value === mime || value === mime.split("/")[0] + "/*"))
    throw new Error("upload_input_accept_mismatch");
  const latest = await uploadWithinBudget(chrome.tabs.get(tab.id), deadline);
  if (String(latest.url) !== String(params.expected_url)) throw new Error("upload_page_changed");
  const targetKey = JSON.stringify([tab.id, Number(target.frameId || 0), target.result.document_id, params.selector]);
  if (uploadTargets.has(targetKey)) throw new Error("upload_target_busy_or_outcome_unknown");
  uploadTargets.set(targetKey, key);
  const record = {fingerprint, result: null};
  uploadOperations.set(key, record);
  let timer = null;
  try {
    if (Date.now() >= deadline) throw new Error("upload_timeout_before_selection");
    dispatched = true;
    const operation = chrome.scripting.executeScript({
      target: {tabId: tab.id, frameIds: [Number(target.frameId || 0)]},
      func: setUploadFileInPage,
      args: [{...params, document_id: target.result.document_id, deadline}]
    });
    const guard = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("upload_timeout_outcome_unknown_do_not_repeat")), Math.max(1, deadline - Date.now())); });
    const selected = await Promise.race([operation, guard]);
    const value = selected && selected[0] && selected[0].result;
    if (!value || value.file_selected !== true) {
      const error = new Error(value && value.error && value.error.code || "upload_selection_not_verified");
      error.action_may_have_run = !(value && value.action_may_have_run === false);
      if (error.action_may_have_run === false) {
        record.result = value;
        uploadTargets.delete(targetKey);
      }
      throw error;
    }
    record.result = {tab_id: String(tab.id), url: String(tab.url), browser_family: BROWSER_FAMILY, selected: true, verified: true, ...value};
    uploadTargets.delete(targetKey);
    return record.result;
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
  } catch (error) {
    if (!dispatched && error.action_may_have_run === undefined) error.action_may_have_run = false;
    throw error;
  }
}