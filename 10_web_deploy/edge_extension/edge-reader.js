// Fixed, read-only renderer inspection. No focus, activation or page mutations.
function edgeReadDocument(params) {
  const max = Math.max(100, Math.min(100000, Number(params.max_chars || 20000)));
  const mode = ["auto", "visible", "fast"].includes(params.mode) ? params.mode : "auto";
  const selector = typeof params.selector === "string" ? params.selector.slice(0, 2000) : "";
  const compact = (value, limit = 160) => String(value || "").replace(/\s+/g, " ").trim().slice(0, limit);
  const escaped = value => CSS.escape(String(value));
  const safeHttpHref = element => {
    try {
      const raw = String(element.getAttribute("href") || "").trim();
      if (!raw || raw.length > 4000) return "";
      const url = new URL(raw, String(document.baseURI || location.href || ""));
      return ["http:", "https:"].includes(url.protocol) && Boolean(url.hostname)
        && !url.username && !url.password && url.href.length <= 4000 ? url.href : "";
    } catch (_) { return ""; }
  };
  const structurallyShown = element => {
    if (String(element.getAttribute && element.getAttribute("type") || "").toLowerCase() === "hidden") return false;
    let node = element;
    for (let depth = 0; node && depth < 20; depth++, node = node.parentElement) {
      if (node.hidden === true) return false;
      if (typeof node.getAttribute !== "function") continue;
      if (node.getAttribute("hidden") !== null || node.getAttribute("aria-hidden") === "true") return false;
      // Inline declaration inspection reads attributes only, never style/layout.
      if (/(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)/i.test(node.getAttribute("style") || "")) return false;
    }
    return true;
  };
  const shown = element => {
    if (mode === "fast") return structurallyShown(element);
    try {
      const style = getComputedStyle(element), rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    } catch (_) { return false; }
  };
  function exactSelector(element, root) {
    const unique = value => {
      try { const matches = root.querySelectorAll(value); return matches.length === 1 && matches[0] === element; } catch (_) { return false; }
    };
    if (element.id) { const value = "#" + escaped(element.id); if (unique(value)) return value; }
    const tag = String(element.tagName || "").toLowerCase();
    const name = element.getAttribute("name");
    if (name) { const value = tag + "[name=" + JSON.stringify(String(name)) + "]"; if (unique(value)) return value; }
    const parts = [];
    let node = element;
    for (let depth = 0; node && node.nodeType === 1 && depth < 12; depth++, node = node.parentElement) {
      let part = String(node.tagName || "").toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(item => item.tagName === node.tagName);
        if (siblings.length > 1) part += ":nth-of-type(" + (siblings.indexOf(node) + 1) + ")";
      }
      parts.unshift(part);
      const value = parts.join(" > ");
      if (unique(value)) return value;
    }
    return ""; // An ambiguous selector must not be advertised as actionable.
  }
  let area = document, shadowPath = [];
  for (const hostSelector of params.shadow_path || []) {
    const matches = area.querySelectorAll(hostSelector);
    if (matches.length !== 1 || !matches[0].shadowRoot) return {text:"",interactive_elements:[],file_inputs:[],read_diagnostics:{code:"browser_shadow_root_unavailable"}};
    area = matches[0].shadowRoot;
    shadowPath.push(hostSelector);
  }
  let root = null, strategy = "body", selectorMissing = false;
  if (selector) {
    try { const found = area.querySelectorAll(selector); if (found.length === 1) { root = found[0]; strategy = selector; } else selectorMissing = true; }
    catch (_) { selectorMissing = true; }
  } else if (area !== document) { root = area; strategy = "open_shadow_root"; }
  if (!root && !selectorMissing) {
    for (const value of ["main", "[role=main]", "article"]) {
      const candidate = document.querySelector(value);
      if (candidate && (mode === "fast" || String(candidate.textContent || "").trim().length >= 80)) { root = candidate; strategy = value; break; }
    }
    if (!root) { root = document.body || document.documentElement; strategy = document.body ? "body" : "documentElement"; }
  }
  const textOf = element => {
    if (!element) return "";
    let raw = "";
    if (mode !== "fast") {
      try { raw = String(element.innerText || ""); } catch (_) {}
    }
    if (!raw.trim()) {
      // textContent on <html> includes inline scripts. Walk only content nodes,
      // with a strict node/character budget and without altering the document.
      let count = 0;
      const pieces = [];
      function walk(node, depth) {
        if (!node || ++count > 8000 || depth > 64 || raw.length > max * 8) return;
        if (node.nodeType === 3) { const value = String(node.nodeValue || ""); pieces.push(value); raw += value; return; }
        if (node.nodeType !== 1 && node.nodeType !== 11) return;
        if (node.nodeType === 1) {
          if (["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "IFRAME", "FRAME"].includes(node.tagName)) return;
          if (mode === "fast") { if (!structurallyShown(node)) return; }
          else try { const style = getComputedStyle(node); if (style.display === "none" || style.visibility === "hidden") return; } catch (_) {}
        }
        for (const child of Array.from(node.childNodes || [])) walk(child, depth + 1);
      }
      walk(element, 0);
      raw = pieces.join(" ");
    }
    return raw.slice(0, max * 8).replace(/\u00a0/g, " ").replace(/\r\n?/g, "\n").replace(/[ \t\f\v]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
  };
  const controls = [], files = [];
  let rootsVisited = 0;
  function collect(scope, path, depth) {
    if (!scope || depth > 8 || ++rootsVisited > 32 || controls.length >= 100) return;
    const controlSelector = "input,textarea,button,select,a,summary,[role=button],[role=link],[role=textbox],[contenteditable=true]";
    const nodes = Array.from(scope.querySelectorAll(controlSelector)).slice(0, 1200);
    if (typeof scope.matches === "function" && scope.matches(controlSelector)) nodes.unshift(scope);
    for (const element of nodes) {
      const tag = String(element.tagName || "").toLowerCase(), type = String(element.getAttribute("type") || "").toLowerCase();
      const selectorScope = element === scope ? (typeof scope.getRootNode === "function" ? scope.getRootNode() : document) : scope;
      const exact = exactSelector(element, selectorScope);
      const common = {selector:exact,shadow_path:path.slice(),id:String(element.id || ""),name:String(element.getAttribute("name") || ""),disabled:Boolean(element.disabled)};
      if (tag === "input" && type === "file") {
        if (files.length < 32) files.push({...common,accept:String(element.accept || "").slice(0,500),multiple:Boolean(element.multiple),selected_files:Array.from(element.files || []).slice(0,20).map(file=>({name:String(file.name).slice(0,200),size:file.size,type:String(file.type).slice(0,100)}))});
      }
      if (!shown(element) || controls.length >= 100) continue;
      const role = element.getAttribute("role") || (tag === "a" ? "link" : tag === "button" || ["button","submit"].includes(type) ? "button" : tag === "select" ? "combobox" : ["input","textarea"].includes(tag) || element.isContentEditable ? "textbox" : "");
      const controlText = mode === "fast" ? element.textContent : element.innerText || element.textContent;
      const value = {...common,index:controls.length,tag,type,role,aria_label:compact(element.getAttribute("aria-label")),placeholder:compact(element.getAttribute("placeholder")),text:compact(controlText),
        visibility_hint:mode === "fast" ? "structural_only" : "layout_observed",visibility_verified:mode !== "fast",locator_unique:Boolean(exact)};
      if (tag === "a" || role === "link") {
        // These are observed navigation facts, not authority to follow a link.
        // No script/data/file URLs, embedded credentials or handler code leave
        // the renderer; consumers still validate the exact target before use.
        value.href = safeHttpHref(element);
        value.href_http_safe = Boolean(value.href);
        value.target = compact(element.getAttribute("target"),128);
        value.opens_new_tab = value.target.toLowerCase() === "_blank";
        value.download = element.getAttribute("download") !== null;
        value.has_inline_handler = ["onclick","onauxclick","onmousedown","onmouseup","onpointerdown","onpointerup","onkeydown"]
          .some(attribute=>element.getAttribute(attribute) !== null);
      }
      if (tag === "input" && ["checkbox","radio"].includes(type)) {
        value.checked = element.checked === true;
        value.indeterminate = element.indeterminate === true;
      }
      if (["input","textarea"].includes(tag)) value.read_only = element.readOnly === true;
      for (const attribute of ["aria-expanded","aria-pressed","aria-selected"]) {
        const state = element.getAttribute(attribute);
        if (state !== null) value[attribute.replace(/-/g,"_")] = compact(state,16);
      }
      if (tag === "select") {
        value.options = Array.from(element.options || []).slice(0,100).map(option=>({label:compact(option.textContent),value:String(option.value).slice(0,160),selected:Boolean(option.selected)}));
        value.selected_index = Number.isInteger(element.selectedIndex) ? element.selectedIndex : -1;
        value.multiple = element.multiple === true;
      }
      controls.push(value);
    }
    // Open shadow controls retain their observed host path. Closed roots remain
    // inaccessible, and are never treated as successful observations.
    for (const host of Array.from(scope.querySelectorAll("*")).slice(0,2000)) {
      if (!host.shadowRoot) continue;
      const hostSelector = exactSelector(host, scope);
      if (hostSelector) collect(host.shadowRoot,path.concat(hostSelector),depth+1);
    }
  }
  if (root) collect(root,shadowPath,0);
  const text = textOf(root), iframeCount = document.querySelectorAll("iframe,frame").length;
  return {text:text.slice(0,max),truncated:text.length>max,extraction:mode === "fast" ? "dom-text-fast" : "dom-text-native",strategy,
    frame_url:String(location.href || ""),frame_title:String(document.title || "").slice(0,500),
    page_status:String(document.readyState || "unknown"),interactive_elements:controls,file_inputs:files,
    read_diagnostics:{ready_state:String(document.readyState || "unknown"),body_present:Boolean(document.body),body_chars:mode === "fast" ? text.length : String(document.body && document.body.textContent || "").length,
      body_chars_bounded:mode === "fast",
      iframe_count:iframeCount,interactive_count:controls.length,file_input_count:files.length,selector_missing:selectorMissing,
      reader_mode:mode,layout_observation_used:mode !== "fast"}};
}

function edgeReadHasContent(value) {
  return Boolean(value && (String(value.text || "").trim() || (value.interactive_elements || []).length
    || (value.file_inputs || []).some(input=>!input.disabled && input.selector)));
}

function edgeReadMerge(frames, params, diagnostics) {
  const max = Math.max(100,Math.min(100000,Number(params.max_chars || 20000)));
  let text = "", truncated = false;
  const controls = [], files = [], summaries = [];
  for (const frame of frames.slice(0,32).sort((a,b)=>a.frameId-b.frameId)) {
    const value = frame.result;
    if (!value || typeof value !== "object") continue;
    const frameId = frame.frameId, frameUrl = String(value.frame_url || "");
    const extra = {frame_id:frameId,frame_url:frameUrl};
    const content = String(value.text || "").trim();
    if (content) {
      const piece = (text ? "\n\n" : "") + (frameId === 0 ? "" : "[iframe " + frameId + "]\n") + content;
      const left = Math.max(0,max-text.length);
      text += piece.slice(0,left);
      if (piece.length>left) truncated=true;
    }
    truncated = truncated || value.truncated === true;
    for (const control of (value.interactive_elements || []).slice(0,100-controls.length)) controls.push({...control,...extra});
    for (const input of (value.file_inputs || []).slice(0,32-files.length)) files.push({...input,...extra});
    summaries.push({frame_id:frameId,url:frameUrl,chars:content.length,useful:edgeReadHasContent(value),...(value.read_diagnostics || {})});
  }
  const main = frames.find(frame=>frame.frameId===0)?.result || frames[0]?.result || {};
  const expectedFrames = Number(main.read_diagnostics && main.read_diagnostics.iframe_count || 0);
  const missingFrames = diagnostics.all_frames_attempted === true && expectedFrames > Math.max(0,summaries.length-1);
  const useful = Boolean(text.trim() || controls.length || files.some(input=>!input.disabled && input.selector));
  return {ok:useful,verified:useful,text,truncated,interactive_elements:controls,file_inputs:files,frames:summaries,
    extraction:main.extraction || "dom-text-native",strategy:main.strategy || "frame-document",
    frame_url:String(main.frame_url || ""),frame_title:String(main.frame_title || "").slice(0,500),
    page_status:String(main.page_status || main.read_diagnostics && main.read_diagnostics.ready_state || "unknown"),
    read_diagnostics:{...diagnostics,partial:diagnostics.partial || missingFrames,frame_count:summaries.length,
      inaccessible_or_missing_frames:missingFrames,meaningful_content:useful},
    ...(!useful ? {action_may_have_run:false,error:{code:"browser_page_empty",message:"The exact page and accessible frames returned no readable text or controls; inspect frame diagnostics or let the page finish loading."}} : {})};
}

async function edgeReadPageExact(id, params) {
  const explicitFrame = Number.isInteger(params.frame_id);
  const requestedFrame = explicitFrame ? params.frame_id : 0;
  const started = Date.now();
  const requestedBudget=Number(params.timeout || 4000);
  const budget = Math.max(100,Math.min(3000,(Number.isFinite(requestedBudget) ? requestedBudget : 4000)-500));
  const deadline = started+budget;
  let frames = [], attempts = 0, partial = false, failure = null, allAttempted = false, tabObservation = null;
  const stages = [];
  const failureCode = error => {
    if (error && error.read_code) return error.read_code;
    const message=String(error && error.message || "");
    if (/Frame with ID .*not ready|No frame with id|No frame with ID/i.test(message)) return "browser_frame_not_ready";
    if (/Receiving end does not exist|Could not establish connection/i.test(message)) return "browser_content_listener_unavailable";
    if (/Cannot access|Missing host permission|not allowed|extensions gallery/i.test(message)) return "browser_frame_permission_denied";
    return "browser_frame_read_failed";
  };
  const argumentsFor = mode => ({max_chars:params.max_chars || 20000,selector:params.selector || null,mode,shadow_path:params.shadow_path || []});
  async function bounded(operation, limit, stage, code) {
    const remaining = deadline-Date.now();
    if (remaining <= 0) throw {read_code:code,read_stage:stage};
    let timer;
    const timeout = new Promise((_,reject)=>{
      timer=setTimeout(()=>reject({read_code:code,read_stage:stage}),Math.max(1,Math.min(limit,remaining)));
    });
    try {return await Promise.race([operation(),timeout]);} finally {clearTimeout(timer);}
  }
  function acceptedValues(values, allFrames) {
    const accepted = [];
    for (const value of Array.isArray(values) ? values.slice(0,32) : []) {
      if (value && value.error) {partial=true;failure="browser_frame_script_error";continue;}
      const frameId = Number.isInteger(value && value.frameId) ? value.frameId : allFrames ? null : requestedFrame;
      if (frameId === null || !value || !value.result || typeof value.result !== "object") {partial=true;failure="browser_frame_script_error";continue;}
      accepted.push({frameId,result:value.result});
    }
    return accepted;
  }
  async function inject(allFrames, mode, limit) {
    // The exact tab is never rebound, activated, duplicated or navigated.
    const passStarted=Date.now(), passDeadline=Math.min(deadline,passStarted+limit);
    const event={route:"scripting",scope:allFrames ? "all_frames" : "exact_frame",mode,stage:"tabs_get",state:"running"};
    stages.push(event);
    try {
      const tab = await bounded(()=>chrome.tabs.get(id),Math.min(200,limit),"tabs_get","browser_tab_lookup_timeout");
      tabObservation = {url:String(tab.url || ""),title:String(tab.title || "").slice(0,500),tab_status:String(tab.status || "unknown")};
      event.stage="script_injection";
      const target = allFrames ? {tabId:id,allFrames:true} : {tabId:id,frameIds:[requestedFrame]};
      // document_idle can wait indefinitely on a portal's pending subresources
      // even while its DOM is already readable. Fixed read code can inspect the
      // current document immediately and report ready_state honestly.
      const values=await bounded(()=>chrome.scripting.executeScript({target,func:edgeReadDocument,args:[argumentsFor(mode)],injectImmediately:true}),
        Math.max(1,passDeadline-Date.now()),"script_injection","browser_frame_read_timeout");
      const accepted=acceptedValues(values,allFrames);
      event.state="completed";event.frame_count=accepted.length;
      return accepted;
    } catch(error) {
      event.state=error && error.read_code ? "timeout" : "failed";
      event.error=failureCode(error);
      throw error;
    } finally {event.elapsed_ms=Date.now()-passStarted;}
  }
  async function messageRead() {
    const event={route:"content_message",scope:"exact_frame",mode:"fast",stage:"content_message",state:"running"},passStarted=Date.now();
    stages.push(event);
    try {
      const value=await bounded(()=>chrome.tabs.sendMessage(id,{type:"xiaoruo1_read_exact_v2",params:argumentsFor("fast")},{frameId:requestedFrame}),
        600,"content_message","browser_content_read_timeout");
      if (!value || value.ok !== true || !value.result || typeof value.result !== "object") throw {read_code:"browser_content_read_failed"};
      event.state="completed";event.frame_count=1;
      return [{frameId:requestedFrame,result:value.result}];
    } catch(error) {event.state=error && error.read_code ? "timeout" : "failed";event.error=failureCode(error);throw error;}
    finally {event.elapsed_ms=Date.now()-passStarted;}
  }
  const mergeFrames = values => {
    const ids=new Set(values.map(frame=>frame.frameId));
    frames=values.concat(frames.filter(frame=>!ids.has(frame.frameId)));
  };
  async function pass(mode,limit) {
    attempts++;
    try {mergeFrames(await inject(false,mode,limit));}
    catch(error) {failure=failureCode(error);}
  }
  function report() {
    return {...edgeReadMerge(frames,params,{attempts,all_frames_attempted:allAttempted,partial,frame_error:failure,
      requested_frame_id:explicitFrame ? requestedFrame : null,elapsed_ms:Date.now()-started,budget_ms:budget,
      stages:stages.slice(0,12),recovered_after_read_failure:Boolean(failure && frames.some(frame=>edgeReadHasContent(frame.result))),
      reader_mode:frames.find(frame=>frame.frameId===requestedFrame)?.result?.read_diagnostics?.reader_mode || null}),
      ...(tabObservation || {}),tab_id:String(id)};
  }
  const requestedMode=params.mode === "fast" ? "fast" : params.mode === "visible" ? "visible" : "auto";
  await pass(requestedMode,requestedMode === "fast" ? 1000 : 600);
  const useful = () => frames.some(frame=>edgeReadHasContent(frame.result));
  if (!useful() && requestedMode !== "fast" && Date.now()<deadline && failure !== "browser_frame_permission_denied") {
    // A second read uses no layout APIs at all. It neither reloads nor changes
    // the tab; this route is also available explicitly as mode=fast.
    await pass("fast",1100);
  }
  if (!explicitFrame && (params.include_frames === true || !useful()) && Date.now()<deadline) {
    allAttempted=true;
    try {mergeFrames(await inject(true,"fast",700));}
    catch(error) {partial=true;failure=failureCode(error);}
  }
  if (!useful() && failure === "browser_frame_not_ready" && Date.now()+100<deadline) {
    await new Promise(resolve=>setTimeout(resolve,100));
    await pass("fast",500);
  }
  if (!useful() && typeof chrome.tabs.sendMessage === "function" && Date.now()<deadline) {
    try {mergeFrames(await messageRead());}
    catch(error) {failure=failureCode(error);}
  }
  return report();
}

// This dedicated content-script route only calls the fixed read function. It
// has no keyboard-marker parser, mutations, caller code or external listener.
if (typeof document !== "undefined" && typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage
    && !globalThis.__xiaoruo1ExactReaderV2) {
  globalThis.__xiaoruo1ExactReaderV2=true;
  chrome.runtime.onMessage.addListener((message,_sender,sendResponse)=>{
    if (!message || message.type !== "xiaoruo1_read_exact_v2") return false;
    const input=message.params && typeof message.params === "object" ? message.params : {};
    const safe={max_chars:Math.max(100,Math.min(100000,Number(input.max_chars || 20000))),mode:"fast",
      selector:typeof input.selector === "string" ? input.selector.slice(0,2000) : null,
      shadow_path:Array.isArray(input.shadow_path) ? input.shadow_path.slice(0,8).filter(value=>typeof value === "string" && value.length<=1000) : []};
    try {sendResponse({ok:true,result:edgeReadDocument(safe)});}
    catch (_) {sendResponse({ok:false,error:{code:"browser_content_reader_error"}});}
    return false;
  });
}
