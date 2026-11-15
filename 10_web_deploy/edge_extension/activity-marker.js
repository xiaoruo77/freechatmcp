// Serialized only into the addressed tab. No page input or focus changes.
function edgeActivityMarker(update) {
  const key = "__xiaoruo1ActivityMarkerV1";
  const revisionKey = "__xiaoruo1ActivityMarkerRevisionV1";
  const valid = new Set(["queued", "running", "completed", "needs_check", "refused", "clear"]);
  if (!update || !valid.has(update.state)) return;
  const now=Date.now(),revision=Number(update.revision);
  const issued=Number(update.issued_at),expires=Number(update.expires_at);
  if (!Number.isSafeInteger(revision) || revision <= Number(globalThis[revisionKey] || 0)
      || !Number.isFinite(issued) || issued > now+1000 || issued < now-150000
      || !Number.isFinite(expires) || expires <= now || expires > issued+150000) return;
  // Keep a tiny revision tombstone after visual removal: a delayed running
  // injection cannot resurrect a notice after the completed update expired.
  globalThis[revisionKey]=revision;
  const previous = globalThis[key];
  if (previous && previous.timer) clearTimeout(previous.timer);
  if (update.state === "clear") {
    if (previous && previous.host) previous.host.remove();
    delete globalThis[key];
    return;
  }
  const labels = {queued:"Xiaoruo1 等待操作此标签页",running:"Xiaoruo1 正在操作此标签页",
    completed:"Xiaoruo1 本次操作已完成",needs_check:"Xiaoruo1 操作结果待核对",refused:"Xiaoruo1 本次操作未执行"};
  const colors = {queued:"#475569",running:"#2563eb",completed:"#15803d",needs_check:"#b45309",refused:"#64748b"};
  let host = previous && previous.host;
  let root = previous && previous.root;
  if (!host || !host.isConnected) {
    host = document.createElement("div");
    host.setAttribute("data-xiaoruo1-activity", "true");
    host.setAttribute("aria-hidden", "true");
    // Keep the notice outside body so page body-text verification and readers
    // never mistake this UI for the result of a website action.
    host.style.cssText = "all:initial!important;position:fixed!important;top:12px!important;right:12px!important;z-index:2147483647!important;pointer-events:none!important;user-select:none!important;display:block!important;max-width:360px!important;contain:content!important;";
    root = host.attachShadow({mode:"closed"});
    const style = document.createElement("style");
    style.textContent = ":host{pointer-events:none!important}section{box-sizing:border-box;max-width:360px;padding:10px 14px;border:1px solid #ffffff66;border-radius:12px;color:white;font:13px/1.4 system-ui,'Microsoft YaHei',sans-serif;box-shadow:0 4px 18px #0003;pointer-events:none;user-select:none}strong{display:block;font-weight:650}span{display:block;font-size:12px;opacity:.94;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}";
    root.appendChild(style);
    const panel = document.createElement("section");
    panel.appendChild(document.createElement("strong"));
    panel.appendChild(document.createElement("span"));
    root.appendChild(panel);
    document.documentElement.appendChild(host);
  }
  const panel = root.querySelector("section");
  panel.style.backgroundColor = colors[update.state];
  panel.querySelector("strong").textContent = labels[update.state];
  panel.querySelector("span").textContent = String(update.action || "网页操作").slice(0, 32);
  const marker = {host, root, revision, timer:null};
  const delay = Math.min(expires-now, update.state === "running" || update.state === "queued" ? 150000 : update.state === "needs_check" ? 8000 : 5000);
  marker.timer = setTimeout(() => {
    if (globalThis[key] !== marker) return;
    host.remove();
    delete globalThis[key];
  }, delay);
  globalThis[key] = marker;
}
