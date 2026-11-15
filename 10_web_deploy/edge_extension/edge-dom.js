// Explicit browser-only control. No Windows input or window focus APIs.
const EDGE_DOM_PROTOCOL = "xiaoruo1-edge-dom/1";
const EDGE_DOM_METHODS = new Set(["list_tabs","connection_status","open_tab","close_tab","activate_tab","navigate",
  "read_page","click","type_text","press_key","select_option","scroll","wait","screenshot","list_downloads"]);
let edgeDomQueue = Promise.resolve();

function edgeDomId(value) {
  const number = Number(value);
  if (value === null || value === undefined || value === "" || !Number.isSafeInteger(number) || number < 0) throw new Error("extension_dom_tab_required");
  return number;
}
function edgeDomSafeUrl(value) {
  try { const url=new URL(String(value || "")); return ["http:","https:"].includes(url.protocol) && Boolean(url.hostname) && !url.username && !url.password; }
  catch (_) { return false; }
}
async function edgeDomExpectedTarget(params,id) {
  if((params.cooperative_tab===true || params.independent_target_guard===true) && typeof params.expected_target_url==="string") {
    const target=await chrome.tabs.get(id);
    if(String(target.url || "")!==params.expected_target_url)throw new Error("extension_dom_target_changed");
  }
}
async function edgeDomWaitExact(id, timeout, requestedUrl, previousUrl = null) {
  const deadline=Date.now()+Math.max(1,Math.min(120000,Number(timeout || 20000)-500));
  let stable=0,lastUrl=null;
  while(true) {
    const tab=await chrome.tabs.get(id),url=String(tab.url || "");
    if ((edgeDomSafeUrl(url) || requestedUrl === "about:blank" && url === "about:blank")
        && (previousUrl === null || previousUrl !== url || requestedUrl === previousUrl)
        && ["loading","complete"].includes(tab.status) && !tab.pendingUrl) {
      stable=lastUrl===url?stable+1:1;lastUrl=url;
    } else {stable=0;lastUrl=null;}
    if(stable>=2)return tab;
    if(Date.now()>=deadline)throw new Error("extension_dom_navigation_unsettled");
    await new Promise(resolve=>setTimeout(resolve,100));
  }
}
function edgeDomFailure(error, phase) {
  const message=String(error && error.message || "");
  if (/^browser_(?:target_in_use|activation_conflicts_with_user)$/.test(message))return {code:message,no_effect:true};
  if(["preflight","script_dispatch"].includes(phase) && /^extension_(?:dom|background)_target_changed$/.test(message))
    return {code:message.replace("extension_background_","extension_dom_"),no_effect:true};
  if (/^extension_(?:dom|background)_[a-z_]+$/.test(message)) return {code:message.replace("extension_background_","extension_dom_"),no_effect:false};
  if (phase === "script_dispatch") {
    if (/^Frame with ID \d+ is not ready\.?$/i.test(message)) return {code:"extension_dom_frame_not_ready",no_effect:true};
    if (/^No (?:tab|frame) with id: ?\d+\.?$/i.test(message)) return {code:"extension_dom_target_missing",no_effect:true};
    if (/^Cannot access (?:contents of (?:the )?url|a chrome:\/\/|a chrome-extension:\/\/|an edge:\/\/)/i.test(message)
        || /^The extensions gallery cannot be scripted\.?$/i.test(message)) return {code:"extension_dom_permission_denied",no_effect:true};
    if (/^(?:Extension context invalidated\.?|Execution context was destroyed(?:,.*)?\.?|The frame was removed\.?|Frame with ID \d+ was removed\.?)$/i.test(message)) return {code:"extension_dom_context_changed",no_effect:false};
  }
  return {code:"extension_dom_failed",no_effect:false};
}
async function edgeDomRun(method, params) {
  let targetId=null,mayHaveRun=false,created=false,phase="preflight";
  const evidence={dom_protocol:EDGE_DOM_PROTOCOL,input_scope:"browser_dom",foreground_input_used:false,
    cooperative_tab:Boolean(params && params.cooperative_tab===true)};
  try {
    if(BROWSER_FAMILY!=="edge" || !params || params.browser_dom_only!==true || params.background_only===true) throw new Error("extension_dom_explicit_mode_required");
    const cooperative=params.cooperative_tab===true;
    if(!EDGE_DOM_METHODS.has(method))throw new Error("extension_dom_action_unsupported");
    if(["list_tabs","connection_status","list_downloads"].includes(method))return {...await COMMANDS[method](params),...evidence};
    if(method!=="open_tab")targetId=edgeDomId(params.tab_id);
    let value;
    if(method==="open_tab") {
      const url=String(params.url || "about:blank");
      if(url!=="about:blank" && !edgeDomSafeUrl(url))throw new Error("extension_dom_url_denied");
      const options={url,active:false};
      // An explicitly selected background window may change its own tab. The
      // focused window is never switched merely because this mode was chosen.
      if(params.activate===true) {
        const windowId=edgeDomId(params.window_id),window=await chrome.windows.get(windowId);
        if(window.focused!==false)throw new Error("browser_activation_conflicts_with_user");
        options.windowId=windowId;options.active=true;
      }
      mayHaveRun=true;
      let tab=await chrome.tabs.create(options);
      targetId=edgeDomId(tab.id);created=true;
      phase="navigation_wait";
      tab=await edgeDomWaitExact(targetId,params.timeout,url);
      value={tab_id:String(targetId),created:true,requested_url:url,final_url:String(tab.url || ""),url:String(tab.url || ""),
        active:tab.active===true,navigation_verified:true,navigation_status:tab.status,verified:true};
    } else {
      const tab=await chrome.tabs.get(targetId);
      if((params.independent_target_guard===true || cooperative) && typeof params.expected_target_url==="string"
          && String(tab.url || "")!==params.expected_target_url)throw new Error("extension_dom_target_changed");
      if(method==="activate_tab") {
        if(tab.active===true)return {tab_id:String(targetId),active:true,url:String(tab.url || ""),verified:true,already_active:true,...evidence};
        const window=await chrome.windows.get(tab.windowId);
        if(window.focused!==false)throw new Error("browser_activation_conflicts_with_user");
        mayHaveRun=true;
        await chrome.tabs.update(targetId,{active:true});
        const after=await chrome.tabs.get(targetId);
        value={tab_id:String(targetId),active:after.active===true,url:String(after.url || ""),verified:after.active===true};
      } else if(method==="close_tab") {
        if(tab.active===true && !cooperative)throw new Error("browser_target_in_use");
        await edgeDomExpectedTarget(params,targetId);
        mayHaveRun=true;
        await chrome.tabs.remove(targetId);
        value={tab_id:String(targetId),closed:true,verified:true};
      } else if(method==="navigate") {
        if(tab.active===true && !cooperative)throw new Error("browser_target_in_use");
        const url=String(params.url || "");
        if(!edgeDomSafeUrl(url))throw new Error("extension_dom_url_denied");
        await edgeDomExpectedTarget(params,targetId);
        mayHaveRun=true;
        await chrome.tabs.update(targetId,{url});
        phase="navigation_wait";
        const after=await edgeDomWaitExact(targetId,params.timeout,url,String(tab.url || ""));
        value={tab_id:String(targetId),requested_url:url,final_url:String(after.url || ""),url:String(after.url || ""),
          navigation_verified:true,navigation_status:after.status,verified:true};
      } else if(method==="read_page") {
        if(!edgeDomSafeUrl(tab.url))throw new Error("extension_dom_page_denied");
        if(typeof edgeReadPageExact!=="function")throw new Error("extension_dom_reader_unavailable");
        value=await edgeReadPageExact(targetId,params);
      } else if(method==="wait") {
        await new Promise(resolve=>setTimeout(resolve,Math.max(0,Math.min(120000,Number(params.milliseconds || 0)))));
        value={tab_id:String(targetId),waited_ms:Number(params.milliseconds || 0),verified:true};
      } else if(method==="screenshot") {
        if(!edgeDomSafeUrl(tab.url))throw new Error("extension_dom_page_denied");
        if(tab.active!==true)throw new Error("extension_dom_capture_requires_active_tab");
        // Check immediately before capture. Never activate a target or restore
        // focus to make capture possible. Chrome captures only the window's
        // already active tab, so verify the selected tab again afterwards.
        const before=await chrome.tabs.get(targetId);
        if(before.active!==true)throw new Error("extension_dom_capture_requires_active_tab");
        const data=await chrome.tabs.captureVisibleTab(before.windowId,{format:"png"});
        const after=await chrome.tabs.get(targetId);
        if(after.active!==true || after.windowId!==before.windowId || after.url!==before.url)throw new Error("extension_dom_capture_target_changed");
        if(typeof data!=="string" || !data.startsWith("data:image/png;base64,") || data.length>24*1024*1024)throw new Error("extension_dom_capture_invalid");
        value={tab_id:String(targetId),url:String(after.url || ""),data_url:data,full_page:false,verified:true};
      } else {
        if(!edgeDomSafeUrl(tab.url))throw new Error("extension_dom_page_denied");
        if(tab.active===true && !cooperative)throw new Error("browser_target_in_use");
        const before=await chrome.tabs.get(targetId);
        if(before.active===true && !cooperative)throw new Error("browser_target_in_use");
        if((params.independent_target_guard===true || cooperative) && typeof params.expected_target_url==="string"
            && String(before.url || "")!==params.expected_target_url)throw new Error("extension_dom_target_changed");
        if(typeof edgeBackgroundDomAction!=="function")throw new Error("extension_dom_helper_unavailable");
        const target={tabId:targetId,frameIds:[params.frame_id===undefined?0:params.frame_id]};
        phase="script_dispatch";mayHaveRun=true;
        const guardedParams={...params,browser_dom_only:true,cooperative_tab:cooperative,require_inactive:!cooperative,
          ...(cooperative && (params.frame_id===undefined || params.frame_id===0) && typeof params.expected_target_url==="string"
            ? {expected_document_url:params.expected_target_url} : {})};
        const guard=async id=>{
          const exact=await chrome.tabs.get(id);
          if(exact.active===true && !cooperative)throw new Error("browser_target_in_use");
        };
        const response=method==="click"
          ? [{result:await edgePerformObservedClick(tab,guardedParams,guard)}]
          : await chrome.scripting.executeScript({target,func:edgeBackgroundDomAction,args:[guardedParams,method],injectImmediately:true});
        value=edgeScriptResult(response,"extension_dom",mayHaveRun);
        phase="script_result";
        if(value.action_may_have_run===false)mayHaveRun=false;
        value={tab_id:String(targetId),url:String(tab.url || ""),...value};
      }
    }
    return {...value,...evidence};
  } catch(error) {
    const failure=edgeDomFailure(error,phase);
    if(failure.no_effect)mayHaveRun=false;
    return {ok:false,verified:false,action_may_have_run:mayHaveRun,error:{code:failure.code,phase},...evidence,
      ...(targetId===null?{}:{tab_id:String(targetId)}),...(created?{created:true,navigation_verified:false}:{})};
  }
}
function edgeDomCommand(method, params, onExecute) {
  const pending=edgeDomQueue.then(async()=>{
    if(typeof onExecute==="function")try{await onExecute();}catch(_){}
    return edgeDomRun(method,params);
  });
  edgeDomQueue=pending.catch(()=>{});
  return pending;
}
