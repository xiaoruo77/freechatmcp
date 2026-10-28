// Independent instance discovery, not an authentication credential.
let browserIdentityPromise = null;
const UNIFIED_ACTIONS = ["status","list_tabs","open_tab","close_tab","activate_tab","navigate","read","click","type","press","select","scroll","wait","screenshot","downloads","debug_status","debug_attach","debug_snapshot","debug_detach"];
function browserIdentity() {
  if (!browserIdentityPromise) browserIdentityPromise = (async () => {
    const key = "xiaoruo1_browser_instance_id";
    const stored = await new Promise((resolve,reject) => chrome.storage.local.get(key, data => {
      if (chrome.runtime.lastError) reject(new Error("browser_identity_storage_unavailable")); else resolve(data || {});
    }));
    let id = stored[key];
    if (typeof id !== "string" || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(id)) {
      id = crypto.randomUUID();
      await new Promise((resolve,reject) => chrome.storage.local.set({[key]:id}, () => {
        if (chrome.runtime.lastError) reject(new Error("browser_identity_storage_unavailable")); else resolve();
      }));
    }
    const kind = BROWSER_FAMILY === "edge" ? "edge" : globalThis.XIAORUO1_BROWSER_KIND === "xiaoruo_browser" ? "xiaoruo_browser" : "chromium_other";
    return {instance_id:id,browser_kind:kind,browser_family:BROWSER_FAMILY,browser_version:BROWSER_VERSION,
      version:EXTENSION_VERSION,supported_actions:[...UNIFIED_ACTIONS]};
  })();
  return browserIdentityPromise;
}