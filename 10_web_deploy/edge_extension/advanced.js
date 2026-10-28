// Opt-in public Chromium debugger commands. No arbitrary caller CDP methods.
const ownedDebugTabs = new Set();
const DEBUG_PROTOCOL = "1.3";
async function debugAvailable() {
  if (!chrome.debugger || typeof chrome.debugger.attach !== "function" || !chrome.permissions || typeof chrome.permissions.contains !== "function")
    return {available:false,reason:"debugger_api_unavailable"};
  const granted = await chrome.permissions.contains({permissions:["debugger"]});
  return {available:granted === true,reason:granted ? null : "debugger_permission_not_granted"};
}
function debugSafeUrl(value) {
  try { const url=new URL(value); return ["http:","https:"].includes(url.protocol) && !url.username && !url.password; } catch (_) {return false;}
}
function sanitizeDebugTree(root) {
  let remaining=800;
  function clean(node,depth) {
    if (!node || depth>12 || --remaining<0) return null;
    const value={node_id:Number(node.nodeId||0),node_name:String(node.nodeName||"").slice(0,80)};
    const attributes=Array.isArray(node.attributes)?node.attributes:[];
    for(let i=0;i<attributes.length;i+=2) if(attributes[i]==="id" || attributes[i]==="class") value[attributes[i]]=String(attributes[i+1]||"").slice(0,160);
    for(const key of ["children","shadowRoots"]) {
      const children=Array.isArray(node[key])?node[key]:[];
      const output=children.slice(0,100).map(child=>clean(child,depth+1)).filter(Boolean);
      if(output.length)value[key]=output;
    }
    if(node.contentDocument)value.frame=clean(node.contentDocument,depth+1);
    return value;
  }
  return {tree:clean(root,0),bounded:true,truncated:remaining<0,excluded:["text","input_values","cookies","network","scripts"]};
}
async function debugStatus(params) {
  const capability=await debugAvailable();
  const id=tabId(params.tab_id); await chrome.tabs.get(id);
  return {tab_id:String(id),...capability,attached:ownedDebugTabs.has(id),owner:"xiaoruo1",verified:true};
}
async function debugAttach(params) {
  if(params.confirm!==true)throw new Error("debug_confirmation_required");
  const id=tabId(params.tab_id);const tab=await chrome.tabs.get(id);
  if(!debugSafeUrl(tab.url))throw new Error("debug_page_not_allowed");
  const capability=await debugAvailable();
  if(!capability.available)return {ok:false,error:{code:capability.reason},available:false,attached:false};
  if(!ownedDebugTabs.has(id)) {await chrome.debugger.attach({tabId:id},DEBUG_PROTOCOL);ownedDebugTabs.add(id);}
  return {tab_id:String(id),attached:true,verified:true,user_can_cancel:"Use Chromium debugging banner Cancel or debug_detach"};
}
async function debugSnapshot(params) {
  const id=tabId(params.tab_id);const tab=await chrome.tabs.get(id);
  if(!debugSafeUrl(tab.url) || !ownedDebugTabs.has(id))throw new Error("debug_target_not_owned");
  const capability=await debugAvailable();if(!capability.available)throw new Error(capability.reason);
  const value=await chrome.debugger.sendCommand({tabId:id},"DOM.getDocument",{depth:3,pierce:true});
  return {tab_id:String(id),url:String(tab.url),verified:true,...sanitizeDebugTree(value.root)};
}
async function debugDetach(params) {
  if(params.confirm!==true)throw new Error("debug_confirmation_required");
  const id=tabId(params.tab_id);
  if(ownedDebugTabs.has(id)){await chrome.debugger.detach({tabId:id});ownedDebugTabs.delete(id);}
  return {tab_id:String(id),attached:false,verified:true};
}
if(chrome.debugger && chrome.debugger.onDetach)chrome.debugger.onDetach.addListener(source=>{if(source && source.tabId!==undefined)ownedDebugTabs.delete(Number(source.tabId));});
if(chrome.tabs.onRemoved)chrome.tabs.onRemoved.addListener(id=>ownedDebugTabs.delete(Number(id)));