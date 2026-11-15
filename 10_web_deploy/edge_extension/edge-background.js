// Fixed Edge DOM actions. No OS input, window focus, tab activation or fallback.
const EDGE_BACKGROUND_PROTOCOL = "xiaoruo1-edge-dom-background/1";
const EDGE_BACKGROUND_METHODS = new Set(["list_tabs", "connection_status", "open_tab", "close_tab", "navigate", "read_page", "click", "type_text", "press_key", "select_option", "scroll", "wait", "list_downloads"]);
let edgeBackgroundQueue = Promise.resolve();

function edgeBackgroundSafeUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return ["http:", "https:"].includes(url.protocol) && Boolean(url.hostname) && !url.username && !url.password;
  } catch (_) { return false; }
}

async function edgeBackgroundWaitExact(id, timeout, complete, requestedUrl = null, previousUrl = null) {
  const deadline = Date.now() + Math.max(1, Math.min(120000, Number(timeout || 20000)));
  let stable = 0, lastUrl = null;
  while (true) {
    // The legacy getTab/wait helpers may rebind stale IDs after a restart.
    // This guarded path must observe only the exact ID that was dispatched.
    const tab = await chrome.tabs.get(id);
    const url = String(tab.url || "");
    // Legitimate server redirects need not keep the requested URL. Observe a
    // stable committed destination in this exact tab, never a browser error or
    // credential-bearing URL. For navigation do not accept the stale old page.
    const committed = edgeBackgroundSafeUrl(url) || (requestedUrl === "about:blank" && url === "about:blank");
    const transitioned = previousUrl === null || url !== previousUrl || requestedUrl === previousUrl;
    // A committed document may retain slow subresources indefinitely. This
    // proves the requested browser navigation, not readable page contents;
    // page readiness is checked by the separate read action.
    if (committed && transitioned && (!complete || ["loading","complete"].includes(tab.status)) && !tab.pendingUrl) {
      stable = lastUrl === url ? stable + 1 : 1;
      lastUrl = url;
    } else { stable = 0; lastUrl = null; }
    // tabs.create may report a requested URL before the actual tab briefly
    // returns newtab/about:blank. Require two committed observations of the same
    // destination; tabs.create/update is never replayed while waiting.
    if (stable >= 2) return tab;
    if (Date.now() >= deadline) throw new Error("extension_background_navigation_unsettled");
    await new Promise(resolve => setTimeout(resolve, 100));
  }
}

async function edgeBackgroundState(tabs) {
  const items = tabs || await chrome.tabs.query({});
  const windows = await chrome.windows.getAll({populate: false});
  const active = items.filter(tab => tab.active === true);
  const activeTabs = {};
  for (const tab of active.sort((a, b) => a.windowId - b.windowId)) {
    const key = String(tab.windowId);
    if (activeTabs[key] !== undefined) throw new Error("extension_background_state_unverified");
    activeTabs[key] = String(tab.id);
  }
  const focused = windows.filter(window => window.focused === true);
  if (!Object.keys(activeTabs).length || focused.length > 1) throw new Error("extension_background_state_unverified");
  return {active_tabs: activeTabs, focused_window_id: focused.length ? String(focused[0].id) : null};
}

function edgeBackgroundSameState(left, right) {
  if (!left || !right || left.focused_window_id !== right.focused_window_id) return false;
  const a = left.active_tabs, b = right.active_tabs;
  if (!a || !b || Array.isArray(a) || Array.isArray(b)) return false;
  const keys = Object.keys(a).sort();
  return keys.length === Object.keys(b).length && keys.every(key => typeof a[key] === "string" && a[key] === b[key]);
}

async function edgeBackgroundGuard(params, expected, targetId, targetMayBeAbsent = false) {
  const state = await edgeBackgroundState();
  if (params.independent_target_guard !== true && !edgeBackgroundSameState(state, expected)) throw new Error("extension_background_active_changed");
  if (targetId !== null) {
    let tab;
    try { tab = await chrome.tabs.get(targetId); } catch (error) {
      if (targetMayBeAbsent) return state;
      throw new Error("extension_background_target_missing");
    }
    if (targetMayBeAbsent) throw new Error("extension_background_target_still_present");
    if (tab.active !== false || state.active_tabs[String(tab.windowId)] === String(targetId)) throw new Error("foreground_required");
    if (params.independent_target_guard === true && params.expected_target_window_id !== undefined
        && params.expected_target_window_id !== null && String(tab.windowId)!==String(params.expected_target_window_id))
      throw new Error("extension_background_target_changed");
  }
  return state;
}

async function edgeBackgroundExpectedTarget(params, id) {
  if (params.independent_target_guard !== true || typeof params.expected_target_url !== "string") return;
  const tab=await chrome.tabs.get(id);
  if (String(tab.url || "")!==params.expected_target_url)throw new Error("extension_background_target_changed");
}

// This function is serialized into one isolated page world. It accepts only
// fixed DOM action names; caller JavaScript is never evaluated.
async function edgeBackgroundDomAction(params, action) {
  let actionMayHaveRun = false;
  try {
  const allowed = ["inspect_click", "click_javascript_anchor", "click", "type_text", "press_key", "select_option", "scroll"];
  if (!allowed.includes(action)) throw new Error("extension_background_action_unsupported");
  // Recheck in the renderer as well: a tab can become active between the
  // service-worker guard and this injection. Never interact with a visible tab.
  const cooperative = params.browser_dom_only===true && params.cooperative_tab===true && params.require_inactive!==true;
  if (!cooperative && document.visibilityState !== "hidden") throw new Error("foreground_required");
  if (cooperative && typeof params.expected_document_url==="string" && String(location.href)!==params.expected_document_url)
    return {ok:false,verified:false,action_may_have_run:false,
      error:{code:"extension_dom_target_changed",phase:"renderer_preflight"}};
  let root = document;
  const visible = element => {
    if (!element || element.nodeType !== 1) return false;
    const style = getComputedStyle(element), rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const unique = items => {
    const candidates = Array.from(items);
    const matches = candidates.filter(visible);
    if (candidates.length === 1 && matches.length === 0) throw new Error("extension_background_target_hidden");
    if (matches.length !== 1) throw new Error("extension_background_target_ambiguous");
    return matches[0];
  };
  for (const selector of params.shadow_path || []) {
    const host = unique(root.querySelectorAll(selector));
    if (!host.shadowRoot) throw new Error("extension_background_shadow_root_unavailable");
    root = host.shadowRoot;
  }
  const compact = text => String(text || "").replace(/\s+/g, " ").trim();
  const match = (text, wanted) => params.exact === true ? compact(text) === compact(wanted) : compact(text).toLowerCase().includes(compact(wanted).toLowerCase());
  const name = element => element.getAttribute("aria-label") || element.getAttribute("title") || element.innerText || element.getAttribute("placeholder") || "";
  const by = params.by || (params.selector && /[#.\[\]>+~*:=,]/.test(params.selector) ? "css" : params.name ? "text" : "role");
  const selector = params.selector || params.name || "";
  let element;
  if (action === "scroll" && !selector) element = document.scrollingElement;
  else if (by === "css") element = unique(root.querySelectorAll(selector));
  else if (by === "placeholder") element = unique(Array.from(root.querySelectorAll("input,textarea,[contenteditable=true]")).filter(item => match(item.getAttribute("placeholder"), selector)));
  else if (by === "label") {
    const label = unique(Array.from(root.querySelectorAll("label")).filter(item => match(item.innerText, selector)));
    element = label.htmlFor ? document.getElementById(label.htmlFor) : label.querySelector("input,textarea,select,[contenteditable=true]");
  } else if (by === "text") {
    const raw = Array.from(root.querySelectorAll("button,a,[role=button],[role=link],[role=menuitem],summary,label,div,span")).filter(item => visible(item) && match(item.innerText || item.textContent, selector));
    // Avoid treating a containing paragraph and its child label as two targets.
    element = unique(raw.filter(item => !raw.some(child => child !== item && item.contains(child))));
  } else if (by === "role") {
    const role = selector || (action === "type_text" || action === "press_key" ? "textbox" : "button");
    const native = {button:"button,input[type=button],input[type=submit]",link:"a",textbox:"input:not([type]),input[type=text],input[type=search],input[type=email],textarea,[contenteditable=true]",combobox:"select",listbox:"select"}[role];
    const query = "[role=\"" + CSS.escape(role) + "\"]" + (native ? "," + native : "");
    element = unique(Array.from(root.querySelectorAll(query)).filter(item => !params.name || match(name(item), params.name)));
  } else throw new Error("extension_background_locator_unsupported");
  if (!element || element.disabled) throw new Error("extension_background_target_disabled");
  if (action === "type_text" && element.readOnly) throw new Error("extension_background_target_readonly");
  const inputType = String(element.type || "").toLowerCase();
  if (["password", "file"].includes(inputType)) throw new Error("extension_background_sensitive_target_denied");
  function javascriptAnchorIdentity() {
    if (element.tagName !== "A" || element.hasAttribute("download")) return null;
    const href = element.getAttribute("href");
    if (!href || href.length > 4000) return null;
    const label = compact(element.getAttribute("aria-label") || element.textContent || element.getAttribute("title"));
    if (!/^(?:下一页|下页|上一页|上页|next(?: page)?|prev(?:ious)?(?: page)?)$/i.test(label)) return null;
    const baseTarget = document.querySelector("base[target]");
    const target = (element.getAttribute("target") ?? baseTarget?.getAttribute("target") ?? "").toLowerCase() || "_self";
    if (target !== "_self") return null;
    try {
      if (new URL(href, document.baseURI || location.href).protocol !== "javascript:") return null;
    } catch (_) {return null;}
    return {href, id:element.getAttribute("id") || "", label};
  }
  if (action === "click_javascript_anchor") {
    const expected = params.observed_javascript_anchor;
    const anchor = javascriptAnchorIdentity();
    if (!expected || !anchor || String(location.href)!==params.observed_document_url
        || anchor.href!==expected.href || anchor.id!==expected.id || anchor.label!==expected.label) {
      return {javascript_anchor_dispatched:false};
    }
    // Let the page's native anchor activation run under its own CSP. This
    // fixed MAIN-world function never interprets the href or attests an effect.
    actionMayHaveRun = true;
    HTMLElement.prototype.click.call(element);
    return {javascript_anchor_dispatched:true};
  }
  if (action === "inspect_click") {
    const link = element.closest && element.closest("a");
    const value = {source_url:String(location.href),frame_id:Number.isInteger(params.frame_id)?params.frame_id:0,
      anchor:false,plain_anchor:false,expected_url:null,target:null};
    if (!link) {
      const form = element.form;
      if (value.frame_id !== 0 || inputType !== "submit" || !["BUTTON", "INPUT"].includes(element.tagName) || form?.tagName !== "FORM") return value;
      const method = (element.getAttribute("formmethod") ?? form.getAttribute("method") ?? "get").toLowerCase();
      if (!["get", "post", ""].includes(method)) return value;
      const baseTarget = document.querySelector && document.querySelector("base[target]");
      const target = (element.getAttribute("formtarget") ?? form.getAttribute("target") ?? baseTarget?.getAttribute("target") ?? "").toLowerCase() || "_self";
      if (!["_self", "_top"].includes(target)) return value;
      const action = element.getAttribute("formaction") ?? form.getAttribute("action") ?? "";
      try {
        const parsed = new URL(action || String(location.href), document.baseURI || String(location.href));
        if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password || parsed.href.length > 4000) return value;
        // Observe the endpoint only. The browser handles native submission,
        // including validation, events, and encoding any form data.
        return {...value,native_submit:true,expected_url:parsed.href,target};
      } catch (_) {return value;}
    }
    const raw = String(link.getAttribute && link.getAttribute("href") || "");
    const baseTarget = document.querySelector && document.querySelector("base[target]");
    const target = String(link.getAttribute && link.getAttribute("target") || link.target || baseTarget && baseTarget.getAttribute("target") || "_self").toLowerCase();
    let destination=null;
    try {
      const parsed=new URL(String(link.href || raw),String(location.href));
      if (["http:","https:"].includes(parsed.protocol) && parsed.hostname && !parsed.username && !parsed.password && parsed.href.length<=4000) destination=parsed.href;
    } catch (_) {}
    const has = name => Boolean(link.hasAttribute && link.hasAttribute(name));
    const eventAttribute = String(link.getAttribute && link.getAttribute("onclick") || "");
    const role = String(link.getAttribute && link.getAttribute("role") || "").toLowerCase();
    // A literal ordinary hyperlink has well-defined navigation semantics.
    // Preserve inline/known framework handlers by dispatching their DOM click
    // once instead. No caller JavaScript or javascript: URL is interpreted.
    const framework = ["data-action","data-toggle","data-bs-toggle","data-router-link","routerlink","ng-click","v-on:click","@click"].some(has);
    const javascriptAnchor = element===link ? javascriptAnchorIdentity() : null;
    const pager = javascriptAnchor ? element.closest("nav,[role='navigation']") : null;
    const currentPages = pager?.querySelectorAll("[aria-current='page']");
    const currentPage = currentPages?.length===1 && visible(currentPages[0]) ? currentPages[0] : null;
    return {...value,anchor:true,expected_url:destination,target,
      javascript_anchor:javascriptAnchor,
      javascript_pager:currentPage && pager.getAttribute("aria-busy")!=="true" ? {
        id:pager.getAttribute("id") || "",label:pager.getAttribute("aria-label") || "",
        page:compact(currentPage.getAttribute("aria-label") || currentPage.textContent).slice(0,160)
      } : null,
      plain_anchor:Boolean(destination && raw && !has("download") && !eventAttribute && typeof link.onclick!=="function"
        && !framework && !["button","menuitem","tab"].includes(role) && ["_self","_blank","_top"].includes(target)),
      standard_download:Boolean(destination && raw && has("download") && !eventAttribute && typeof link.onclick!=="function"
        && !framework && !["button","menuitem","tab"].includes(role) && ["_self","_parent","_top"].includes(target)),
      has_inline_handler:Boolean(eventAttribute || typeof link.onclick==="function"),download:has("download")};
  }
  const state = () => ({url:String(location.href), text: String(document.body && document.body.innerText || "").slice(0, 100000), value: "value" in element ? String(element.value) : null, checked: typeof element.checked === "boolean" ? element.checked : null, expanded: element.getAttribute("aria-expanded"), pressed: element.getAttribute("aria-pressed"), selected_index: typeof element.selectedIndex === "number" ? element.selectedIndex : null, scroll_x: element.scrollLeft, scroll_y: element.scrollTop});
  const before = state();
  if (action === "click") {
    const link = element.closest && element.closest("a");
    if (link && link.target && !["_self", "_parent", "_top"].includes(link.target.toLowerCase())) throw new Error("extension_background_new_window_denied");
    actionMayHaveRun = true;
    element.click();
  } else if (action === "type_text") {
    if (!["INPUT", "TEXTAREA"].includes(element.tagName) && !element.isContentEditable) throw new Error("extension_background_editable_required");
    if (element.tagName === "INPUT" && !["", "text", "search", "email", "url", "tel", "number", "date", "time", "datetime-local", "month", "week"].includes(inputType)) throw new Error("extension_background_editable_required");
    const current = element.isContentEditable ? String(element.textContent || "") : String(element.value || "");
    const expected = (params.clear === false ? current : "") + String(params.text || "");
    let setter = null;
    if (!element.isContentEditable) {
      const proto = element.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (!descriptor || typeof descriptor.set !== "function") throw new Error("extension_background_native_setter_unavailable");
      setter = descriptor.set;
    }
    actionMayHaveRun = true;
    if (element.isContentEditable) element.textContent = expected;
    else setter.call(element, expected);
    element.dispatchEvent(new Event("input", {bubbles:true, composed:true}));
    element.dispatchEvent(new Event("change", {bubbles:true, composed:true}));
    // Framework handlers can normalize or replace a value during their update
    // microtask. Read the addressed element again without repeating the write.
    await Promise.resolve();
    const verified = element.isConnected !== false && (element.isContentEditable ? element.textContent : element.value) === expected;
    return {ok:verified,verified,typed:true,action_may_have_run:true,
      input_semantics:"synthetic_dom_events_untrusted",value_length:expected.length,url:String(location.href),
      ...(!verified ? {error:{code:"extension_background_value_not_retained",phase:"control_verification",
        message:"The page did not retain the requested value. Inspect the current field before another write."}} : {})};
  } else if (action === "select_option") {
    if (element.tagName !== "SELECT") throw new Error("extension_background_select_required");
    const wanted = String(params.option || ""), optionBy = params.option_by || "label";
    const options = Array.from(element.options);
    const matches = options.filter((option, index) => optionBy === "index" ? index === Number(wanted) : optionBy === "value" ? String(option.value) === wanted : compact(option.textContent) === compact(wanted));
    if (matches.length !== 1 || matches[0].disabled
        || (matches[0].parentElement?.tagName === "OPTGROUP" && matches[0].parentElement.disabled))
      throw new Error("extension_background_option_ambiguous");
    const selectedOption = matches[0], expectedIndex = options.indexOf(selectedOption), expectedValue = String(selectedOption.value);
    actionMayHaveRun = true;
    // Values are not unique option identities: two labels can legitimately
    // share a value (including an empty value). Select the addressed index.
    element.selectedIndex = expectedIndex;
    element.dispatchEvent(new Event("input", {bubbles:true}));
    element.dispatchEvent(new Event("change", {bubbles:true}));
    const verified = element.selectedIndex === expectedIndex && String(element.value) === expectedValue
      && element.options[expectedIndex] === selectedOption;
    return {ok:verified,verified,action_may_have_run:true,selected_index:element.selectedIndex,
      input_semantics:"synthetic_dom_events_untrusted",url:String(location.href),
      ...(!verified ? {error:{code:"extension_background_selection_not_retained",phase:"control_verification",
        message:"The page did not retain the requested option. Inspect the current selection before another write."}} : {})};
  } else if (action === "press_key") {
    const key = String(params.key || "");
    if (!["Enter","Escape","Tab","ArrowDown","ArrowUp","ArrowLeft","ArrowRight","Home","End","Backspace","Delete"," "].includes(key)) throw new Error("extension_background_key_unsupported");
    actionMayHaveRun = true;
    element.dispatchEvent(new KeyboardEvent("keydown", {key,bubbles:true,cancelable:true,composed:true}));
    element.dispatchEvent(new KeyboardEvent("keyup", {key,bubbles:true,cancelable:true,composed:true}));
  } else if (action === "scroll") {
    const pixels = Math.max(1, Math.min(100000, Math.abs(Number(params.pixels || 600))));
    const direction = params.direction || "down";
    if (!["down","up","left","right"].includes(direction)) throw new Error("extension_background_scroll_direction_invalid");
    actionMayHaveRun = true;
    element.scrollBy({left:direction === "left" ? -pixels : direction === "right" ? pixels : 0, top:direction === "up" ? -pixels : direction === "down" ? pixels : 0, behavior:"instant"});
  }
  const verificationStarted = Date.now();
  const verificationBudget = params.download_observation_only===true ? 0 : Math.min(1500, Math.max(0, Number(params.timeout || 2000) - 500));
  let after = state(), changed = JSON.stringify(before) !== JSON.stringify(after);
  // Observe delayed handlers without dispatching the action again. Background
  // timers can be throttled by Edge, so the returned elapsed time is evidence,
  // not a promise that every asynchronous website action has settled.
  while (!changed && Date.now() - verificationStarted < verificationBudget) {
    await new Promise(resolve => setTimeout(resolve, Math.min(100, Math.max(1, verificationBudget - (Date.now() - verificationStarted)))));
    if (!cooperative && document.visibilityState !== "hidden") throw new Error("foreground_required");
    after = state();
    changed = JSON.stringify(before) !== JSON.stringify(after);
  }
  let transition = {};
  try {
    const initial = new URL(before.url), final = new URL(after.url);
    const safe = url => ["http:", "https:"].includes(url.protocol) && !url.username && !url.password && url.href.length <= 4000;
    if (safe(initial) && safe(final)) transition = {before_url:initial.href, after_url:final.href,
      url_transition:initial.href !== final.href && initial.origin === final.origin};
  } catch (_) {}
  // Return only bounded change metadata, never duplicate page or input contents.
  return {verified:changed, dispatched:true, action, input_semantics:"synthetic_dom_events_untrusted", url:String(location.href),
    ...transition, verification_wait_ms:Date.now() - verificationStarted,
    verification:changed ? "observed_page_or_control_change" : "no_observable_change"};
  } catch (error) {
    const requestedCode = String(error && error.message || "");
    const fixedMessages = {
      foreground_required:"The selected control requires an inactive tab for this execution mode.",
      extension_background_action_unsupported:"The requested control action is unavailable.",
      extension_background_target_hidden:"The addressed control is hidden. Open its panel and inspect it before editing.",
      extension_background_target_ambiguous:"The locator does not identify exactly one visible control.",
      extension_background_target_disabled:"The addressed control is disabled.",
      extension_background_target_readonly:"The addressed control is read-only.",
      extension_background_sensitive_target_denied:"This control requires its dedicated authorized input flow.",
      extension_background_editable_required:"The addressed control does not support text editing.",
      extension_background_native_setter_unavailable:"The native text value setter is unavailable for this control.",
      extension_background_shadow_root_unavailable:"The observed open shadow root is no longer available.",
      extension_background_locator_unsupported:"The requested locator strategy is unavailable.",
      extension_background_select_required:"The addressed control is not a native select.",
      extension_background_option_ambiguous:"The requested native option is absent, ambiguous or disabled.",
      extension_background_key_unsupported:"The requested synthetic key is unavailable.",
      extension_background_scroll_direction_invalid:"The requested scroll direction is unavailable.",
      extension_background_new_window_denied:"This click requires the dedicated observed-navigation flow."
    };
    const known = Object.prototype.hasOwnProperty.call(fixedMessages, requestedCode);
    const code = known ? requestedCode : actionMayHaveRun
      ? "extension_background_control_exception" : "extension_background_validation_failed";
    return {ok:false,verified:false,action_may_have_run:actionMayHaveRun,
      error:{code,phase:actionMayHaveRun ? "control_mutation" : "target_validation",
        message:known ? fixedMessages[code] : actionMayHaveRun
          ? "The page raised an error after the control action began. Inspect its current state without repeating the action."
          : "The control could not be validated. Inspect the current page before editing."}};
  }
}

function edgeScriptResult(response, prefix, actionMayHaveRun) {
  const first = Array.isArray(response) ? response[0] : null;
  if (first && first.error) {
    return {ok:false,verified:false,action_may_have_run:actionMayHaveRun,
      error:{code:prefix+"_script_execution_error",phase:"script_result",
        message:"The browser returned a page-script error without a verifiable control result. Inspect the current field before retrying."}};
  }
  if (!first || !first.result || typeof first.result !== "object" || Array.isArray(first.result)) {
    return {ok:false,verified:false,action_may_have_run:actionMayHaveRun,
      error:{code:prefix+"_script_result_missing",phase:"script_result",
        message:"The browser returned no verifiable page-script result. Inspect the current field before retrying."}};
  }
  return first.result;
}

const EDGE_NAVIGATION_PROTOCOL = "xiaoruo1-edge-navigation/1";
const EDGE_DOWNLOAD_PROTOCOL = "xiaoruo1-edge-download/1";

async function edgeClickBounded(operation, milliseconds, code) {
  let timer;
  try {
    return await Promise.race([Promise.resolve().then(operation),new Promise((_,reject)=>{
      timer=setTimeout(()=>reject(new Error(code)),Math.max(1,milliseconds));
    })]);
  } finally {if(timer!==undefined)clearTimeout(timer);}
}

function edgeClickNavigationEvidence(intent, sourceTab, destination, created, semantics, verified=false) {
  return {navigation_protocol:EDGE_NAVIGATION_PROTOCOL,operation_no_replay:true,dispatched:true,
    dispatch_semantics:semantics,
    navigation_source:{source_tab_id:String(sourceTab.id),source_url:String(sourceTab.url || ""),
      expected_url:intent.expected_url,target:intent.target,frame_id:intent.frame_id},
    navigation_target:{tab_id:String(destination.id),url:String(destination.url || ""),created:created===true,
      status:String(destination.status || "unknown"),navigation_verified:verified===true,
      complete:destination.status==="complete" && !destination.pendingUrl}};
}

async function edgeClickObserveNavigation(id, intent, sourceTab, created, semantics, guard, budget) {
  const deadline=Date.now()+Math.max(1,Math.min(2500,budget));
  let last=null,stable=0,tab=null;
  while(Date.now()<=deadline) {
    await edgeClickBounded(()=>guard(id),Math.min(500,Math.max(1,deadline-Date.now())),"browser_click_guard_timeout");
    tab=await edgeClickBounded(()=>chrome.tabs.get(id),Math.min(500,Math.max(1,deadline-Date.now())),"extension_background_navigation_observation_timeout");
    const url=String(tab.url || "");
    const sourceUrl=String(sourceTab.url || "");
    const transitioned=created || url!==sourceUrl || (intent.native_submit!==true && intent.expected_url===sourceUrl);
    if(edgeBackgroundSafeUrl(url) && transitioned && ["complete","loading"].includes(tab.status) && !tab.pendingUrl) {
      stable=last===url?stable+1:1;last=url;
      if(stable>=2) {
        let expected=false;
        try {
          const wanted=new URL(intent.expected_url),actual=new URL(url);
          if (intent.native_submit===true) {
            expected=actual.origin===wanted.origin && actual.pathname===wanted.pathname;
          } else {
            expected=actual.href===wanted.href || actual.origin===wanted.origin
              || wanted.protocol==="http:" && actual.protocol==="https:" && wanted.hostname===actual.hostname
                && wanted.pathname===actual.pathname && wanted.search===actual.search;
          }
        } catch (_) {}
        if(expected)return {ok:true,verified:true,tab_id:String(sourceTab.id),target_tab_id:String(tab.id),source_tab_id:String(sourceTab.id),
          url,created:created===true,action_may_have_run:true,navigation_verified:true,
          ...edgeClickNavigationEvidence(intent,sourceTab,tab,created,semantics,true)};
      }
    }
    await new Promise(resolve=>setTimeout(resolve,Math.min(100,Math.max(1,deadline-Date.now()))));
  }
  const evidence=tab?edgeClickNavigationEvidence(intent,sourceTab,tab,created,semantics):{};
  return {ok:false,verified:false,action_may_have_run:true,tab_id:String(sourceTab.id),target_tab_id:String(id),source_tab_id:String(sourceTab.id),
    created:created===true,error:{code:"browser_navigation_pending",phase:"navigation_observation"},...evidence};
}

function edgeClickDownloadSource(intent, sourceTab) {
  return {source_tab_id:String(sourceTab.id),source_url:String(sourceTab.url || ""),expected_url:intent.expected_url};
}

async function edgeClickDownloadBaseline(budget) {
  if(!chrome.downloads || typeof chrome.downloads.search!=="function")return {available:false,ids:new Set(),reason:"download_api_unavailable"};
  try {
    const values=await edgeClickBounded(()=>chrome.downloads.search({orderBy:["-startTime"],limit:100}),Math.min(300,budget),"browser_download_query_timeout");
    if(!Array.isArray(values))return {available:false,ids:new Set(),reason:"download_response_unavailable"};
    return {available:true,ids:new Set(values.filter(item=>Number.isSafeInteger(item&&item.id)).map(item=>item.id))};
  } catch (_) {return {available:false,ids:new Set(),reason:"download_query_unavailable"};}
}

function edgeClickDownloadRecord(item) {
  const number=value=>Number.isFinite(Number(value))?Math.max(0,Math.floor(Number(value))):0;
  return {id:item.id,url:String(item.url || ""),final_url:String(item.finalUrl || ""),
    state:["in_progress","complete","interrupted"].includes(item.state)?item.state:"unknown",
    filename:String(item.filename || "").replace(/[\u0000-\u001f\u007f]/g," ").slice(0,2048),
    exists:typeof item.exists==="boolean"?item.exists:null,
    bytes_received:number(item.bytesReceived),total_bytes:number(item.totalBytes)};
}

async function edgeClickObserveDownload(intent, sourceTab, baseline, dispatchedAt, guard, budget) {
  const receipt={download_protocol:EDGE_DOWNLOAD_PROTOCOL,download_source:edgeClickDownloadSource(intent,sourceTab),
    tab_id:String(sourceTab.id),dispatched:true,operation_no_replay:true,action_may_have_run:true};
  if(baseline.available) {
    const deadline=Date.now()+Math.max(1,Math.min(2000,budget));
    while(Date.now()<=deadline) {
      await edgeClickBounded(()=>guard(Number(sourceTab.id)),Math.min(300,Math.max(1,deadline-Date.now())),"browser_download_target_guard_timeout");
      let values=[];
      try {values=await edgeClickBounded(()=>chrome.downloads.search({orderBy:["-startTime"],limit:100}),
        Math.min(300,Math.max(1,deadline-Date.now())),"browser_download_query_timeout");}catch(_){}
      const matches=(Array.isArray(values)?values:[]).filter(item=>item && Number.isSafeInteger(item.id)
        && !baseline.ids.has(item.id) && Number.isFinite(Date.parse(item.startTime)) && Date.parse(item.startTime)>=dispatchedAt
        && (String(item.url || "")===intent.expected_url || String(item.finalUrl || "")===intent.expected_url));
      if(matches.length===1) {
        const download=edgeClickDownloadRecord(matches[0]);
        return {...receipt,ok:true,verified:true,verification:"matching_download_record",download_started:true,
          download,download_complete:download.state==="complete",goal_download_complete:download.state==="complete" && download.exists===true};
      }
      if(matches.length>1)return {...receipt,ok:false,verified:false,download_started:null,
        error:{code:"browser_download_result_ambiguous",phase:"download_observation"},
        recovery:{next_tool:"browser_list_downloads",repeat_write_allowed:false}};
      await new Promise(resolve=>setTimeout(resolve,Math.min(100,Math.max(1,deadline-Date.now()))));
    }
  }
  return {...receipt,ok:false,verified:false,download_started:null,
    download_observation:{available:baseline.available,reason:baseline.reason || "matching_download_not_observed"},
    error:{code:"browser_download_result_unconfirmed",phase:"download_observation"},
    recovery:{next_tool:"browser_list_downloads",repeat_write_allowed:false}};
}

async function edgePerformObservedClick(sourceTab, params, guard) {
  const sourceId=Number(sourceTab.id),target={tabId:sourceId,frameIds:[params.frame_id===undefined?0:params.frame_id]};
  let intent=null,dispatched=false,destinationId=sourceId,createdTarget=false;
  let observedDocumentId=null;
  const observedInTab=()=>intent?.expected_url && intent.frame_id===0;
  const observedSemantics=()=>intent?.native_submit===true ? "dom_submit_observed_navigation" : "dom_click_observed_navigation";
  const budget=Math.max(500,Math.min(6000,Number(params.timeout || 5000)-250));
  const started=Date.now(),remaining=()=>Math.max(1,budget-(Date.now()-started));
  async function inspect() {
    await edgeClickBounded(()=>guard(sourceId),Math.min(500,remaining()),"browser_click_guard_timeout");
    const results=await edgeClickBounded(()=>chrome.scripting.executeScript({target,func:edgeBackgroundDomAction,
      args:[params,"inspect_click"],injectImmediately:true}),Math.min(1000,remaining()),"browser_click_preflight_timeout");
    const first=results&&results[0];
    const observed=edgeScriptResult(results,params.browser_dom_only===true ? "extension_dom" : "extension_background",false);
    if(observed.ok===false) {
      const failure=new Error("browser_click_preflight_unavailable");
      failure.control_result=observed;
      throw failure;
    }
    observedDocumentId=typeof first.documentId==="string" && first.documentId ? first.documentId : null;
    return observed;
  }
  try {
    intent=await inspect();
    if(intent.javascript_anchor) {
      const clickTarget=observedDocumentId ? {tabId:sourceId,documentIds:[observedDocumentId]} : target;
      const sameFrame=result=>result && result.frameId===target.frameIds[0]
        && (!observedDocumentId || result.documentId===observedDocumentId);
      async function observe() {
        await edgeClickBounded(()=>guard(sourceId),Math.min(300,remaining()),"browser_click_guard_timeout");
        const [result]=await edgeClickBounded(()=>chrome.scripting.executeScript({target:clickTarget,world:"ISOLATED",
          func:edgeBackgroundDomAction,args:[params,"inspect_click"],injectImmediately:true}),Math.min(400,remaining()),"browser_click_observation_timeout");
        return sameFrame(result) && !result.error && result.result?.source_url===intent.source_url ? result.result : null;
      }
      const before=await observe();
      if(!before?.javascript_anchor || before.javascript_anchor.href!==intent.javascript_anchor.href
          || before.javascript_anchor.id!==intent.javascript_anchor.id || before.javascript_anchor.label!==intent.javascript_anchor.label
          || (await chrome.tabs.get(sourceId)).url!==sourceTab.url)throw new Error("browser_click_target_changed");
      await guard(sourceId);
      dispatched=true;
      const [result]=await edgeClickBounded(()=>chrome.scripting.executeScript({target:clickTarget,world:"MAIN",
        func:edgeBackgroundDomAction,args:[{...params,observed_javascript_anchor:intent.javascript_anchor,
          observed_document_url:intent.source_url},"click_javascript_anchor"],injectImmediately:true}),
        Math.min(700,remaining()),"browser_click_result_unknown");
      let verified=false;
      const beforePager=before.javascript_pager;
      if(beforePager?.page && sameFrame(result) && !result.error && result.result?.javascript_anchor_dispatched===true) {
        const deadline=Date.now()+Math.min(1500,remaining());
        let changedPage=null;
        do {
          const after=await observe();
          if(!after)break;
          const pager=after.javascript_pager;
          if(after.javascript_anchor?.id===intent.javascript_anchor.id && after.javascript_anchor.label===intent.javascript_anchor.label
              && pager?.page && pager.id===beforePager.id && pager.label===beforePager.label && pager.page!==beforePager.page) {
            if(changedPage===pager.page){verified=true;break;}
            changedPage=pager.page;
          } else {changedPage=null;}
          await new Promise(resolve=>setTimeout(resolve,Math.min(100,Math.max(1,deadline-Date.now()))));
        } while(Date.now()<deadline);
      }
      return {verified,dispatched:true,action_may_have_run:true,action:"click",operation_no_replay:true,
        input_semantics:"synthetic_dom_events_untrusted",dispatch_semantics:"native_javascript_anchor_click",
        verification:verified?"observed_pagination_indicator_change":"no_observable_change"};
    }
    if(intent.download===true) {
      if(intent.standard_download!==true || !intent.expected_url)throw new Error("browser_click_download_handler_required");
      const again=await inspect();
      if(again.source_url!==intent.source_url || again.expected_url!==intent.expected_url || again.target!==intent.target || again.standard_download!==true)
        throw new Error("browser_click_target_changed");
      const current=await chrome.tabs.get(sourceId);
      if(current.url!==sourceTab.url)throw new Error("browser_click_target_changed");
      const baseline=await edgeClickDownloadBaseline(remaining());
      await edgeClickBounded(()=>guard(sourceId),Math.min(300,remaining()),"browser_click_guard_timeout");
      dispatched=true;
      const dispatchedAt=Date.now();
      // The page action happens once. Its result may have no DOM change because
      // Chromium starts a download outside the document. Observe downloads as
      // evidence instead of inferring failure and clicking a second time.
      try {
        const dispatchedResult=await edgeClickBounded(()=>chrome.scripting.executeScript({target,func:edgeBackgroundDomAction,
          args:[{...params,download_observation_only:true},"click"],injectImmediately:true}),Math.min(700,remaining()),"browser_download_click_result_unavailable");
        const observed=dispatchedResult && dispatchedResult[0] && dispatchedResult[0].result;
        if(observed && observed.ok===false && observed.action_may_have_run===false)
          return {...observed,tab_id:String(sourceId),operation_no_replay:true,
            download_protocol:EDGE_DOWNLOAD_PROTOCOL,download_source:edgeClickDownloadSource(intent,sourceTab)};
      }catch(error){
        const failure=edgeBackgroundFailure(error,"script_dispatch");
        if(failure.no_effect)return {ok:false,verified:false,action_may_have_run:false,tab_id:String(sourceId),
          operation_no_replay:true,download_protocol:EDGE_DOWNLOAD_PROTOCOL,download_source:edgeClickDownloadSource(intent,sourceTab),
          error:{code:failure.code,phase:"download_dispatch"}};
      }
      return await edgeClickObserveDownload(intent,sourceTab,baseline,dispatchedAt,guard,remaining());
    }
    if(intent.anchor===true && intent.target && !["_self","_parent","_top"].includes(intent.target) && intent.plain_anchor!==true)
      throw new Error("browser_click_requires_page_handler");
    if(intent.plain_anchor===true && intent.expected_url && (intent.target==="_blank" || (intent.frame_id===0 && ["_self","_top"].includes(intent.target)))) {
      const again=await inspect();
      if(again.source_url!==intent.source_url || again.expected_url!==intent.expected_url || again.target!==intent.target || again.plain_anchor!==true)
        throw new Error("browser_click_target_changed");
      const current=await chrome.tabs.get(sourceId);
      if(current.url!==sourceTab.url)throw new Error("browser_click_target_changed");
      await guard(sourceId);
      dispatched=true;
      if(intent.target==="_blank") {
        const created=await chrome.tabs.create({url:intent.expected_url,active:false,windowId:sourceTab.windowId});
        destinationId=Number(created.id);createdTarget=true;
        return await edgeClickObserveNavigation(destinationId,intent,sourceTab,true,"observed_anchor_navigation",guard,remaining());
      }
      await chrome.tabs.update(sourceId,{url:intent.expected_url});
      return await edgeClickObserveNavigation(sourceId,intent,sourceTab,false,"observed_anchor_navigation",guard,remaining());
    }
    await guard(sourceId);
    dispatched=true;
    const results=await edgeClickBounded(()=>chrome.scripting.executeScript({target,func:edgeBackgroundDomAction,
      args:[params,"click"],injectImmediately:true}),Math.min(2500,remaining()),"extension_background_context_changed");
    const value=edgeScriptResult(results,params.browser_dom_only===true ? "extension_dom" : "extension_background",true);
    if(value.ok===false) {
      if(value.action_may_have_run===false)return {...value,tab_id:String(sourceId),operation_no_replay:true};
      if(observedInTab())throw new Error("extension_background_context_changed");
      return {...value,tab_id:String(sourceId),operation_no_replay:true};
    }
    // A framework handler may update the same page without navigating. Preserve
    // that exact observed effect rather than navigating to an anchor href.
    if(value.verified===true && (!intent.expected_url || value.url===intent.source_url))return value;
    if(observedInTab() && value.url!==intent.source_url)
      return await edgeClickObserveNavigation(sourceId,intent,sourceTab,false,observedSemantics(),guard,remaining());
    return value;
  } catch(error) {
    if(!dispatched && error && error.control_result) {
      return {...error.control_result,tab_id:String(sourceId),target_tab_id:String(sourceId),
        created:false,operation_no_replay:true,action_may_have_run:false};
    }
    const text=String(error&&error.message || "");
    const contextChanged=/^(?:extension_background_context_changed|Execution context was destroyed(?:,.*)?\.?|The frame was removed\.?|Frame with ID \d+ was removed\.?)$/.test(text);
    if(dispatched && contextChanged && observedInTab()) {
      try {return await edgeClickObserveNavigation(sourceId,intent,sourceTab,false,observedSemantics(),guard,remaining());}
      catch (_) {}
    }
    let code="browser_click_result_unknown";
    const classified=edgeBackgroundFailure(error,"script_dispatch");
    const classifiedCode=params.browser_dom_only===true
      ? classified.code.replace("extension_background_","extension_dom_") : classified.code;
    if(classified.code!=="extension_background_failed")code=classifiedCode;
    else if(!dispatched)code=/^browser_click_[a-z_]+$/.test(text)?text:"browser_click_preflight_unavailable";
    const value={ok:false,verified:false,action_may_have_run:dispatched,error:{code,phase:dispatched?"click_result":"click_preflight"},
      tab_id:String(sourceId),target_tab_id:String(destinationId),created:createdTarget,operation_no_replay:true};
    if(intent&&intent.expected_url)value.navigation_intent={source_tab_id:String(sourceId),source_url:String(sourceTab.url || ""),
      expected_url:intent.expected_url,target:intent.target,frame_id:intent.frame_id};
    if(dispatched && intent && intent.download===true) {
      value.download_protocol=EDGE_DOWNLOAD_PROTOCOL;
      value.download_source=edgeClickDownloadSource(intent,sourceTab);
      value.recovery={next_tool:"browser_list_downloads",repeat_write_allowed:false};
    }
    if(dispatched && intent && intent.expected_url) {
      let observed={id:destinationId,url:"",status:"unknown"};
      try {observed=await edgeClickBounded(()=>chrome.tabs.get(destinationId),200,"browser_click_target_observation_timeout");}catch(_){}
      Object.assign(value,edgeClickNavigationEvidence(intent,sourceTab,observed,createdTarget,
        intent.plain_anchor===true?"observed_anchor_navigation":observedSemantics(),false));
    }
    return value;
  }
}

function edgeBackgroundFailure(error, phase) {
  const message = String(error && error.message || "");
  if (/^(?:foreground_required|background_screenshot_unsupported|extension_background_[a-z_]+)$/.test(message)) return {code:message, no_effect:false};
  // Only fixed browser-owned rejection forms prove an injection was rejected
  // before entering the renderer. Never return raw errors or page arguments.
  if (phase === "script_dispatch") {
    if (/^Frame with ID \d+ is not ready\.?$/i.test(message)) return {code:"extension_background_frame_not_ready", no_effect:true};
    if (/^No (?:tab|frame) with id: ?\d+\.?$/i.test(message) || /^No frame with id \d+ in tab \d+\.?$/i.test(message)) return {code:"extension_background_target_missing", no_effect:true};
    if (/^Cannot access (?:contents of (?:the )?url|a chrome:\/\/|a chrome-extension:\/\/|an edge:\/\/)/i.test(message)
        || /^The extensions gallery cannot be scripted\.?$/i.test(message)) return {code:"extension_background_permission_denied", no_effect:true};
    if (/^(?:Extension context invalidated\.?|Execution context was destroyed(?:,.*)?\.?|The frame was removed\.?|Frame with ID \d+ was removed\.?)$/i.test(message)) return {code:"extension_background_context_changed", no_effect:false};
  }
  return {code:"extension_background_failed", no_effect:false};
}

async function edgeBackgroundRun(method, params) {
  let actionMayHaveRun = false, before = null, after = null, targetId = null, phase = "preflight", created = false, returnedValue = null;
  try {
    if (BROWSER_FAMILY !== "edge") throw new Error("extension_background_edge_required");
    if (method === "screenshot") throw new Error("background_screenshot_unsupported");
    if (!EDGE_BACKGROUND_METHODS.has(method)) throw new Error("extension_background_action_unsupported");
    if (["list_tabs", "connection_status", "list_downloads"].includes(method)) return {background_protocol:EDGE_BACKGROUND_PROTOCOL,...await COMMANDS[method](params)};
    const expected = {active_tabs:params.expected_active_tabs, focused_window_id:params.expected_focused_window_id};
    if (!params.expected_active_tabs || !Object.prototype.hasOwnProperty.call(params, "expected_focused_window_id")) throw new Error("extension_background_observation_required");
    if (method !== "open_tab") targetId = tabId(params.tab_id);
    before = await edgeBackgroundGuard(params, expected, targetId);
    if(targetId!==null)await edgeBackgroundExpectedTarget(params,targetId);
    let value;
    if (method === "open_tab") {
      const url = String(params.url || "about:blank");
      if (url !== "about:blank" && !edgeBackgroundSafeUrl(url)) throw new Error("extension_background_url_denied");
      const windowId = Number(expected.focused_window_id && Object.hasOwn(expected.active_tabs, expected.focused_window_id) ? expected.focused_window_id : Object.keys(expected.active_tabs)[0]);
      await edgeBackgroundGuard(params, expected, null);
      actionMayHaveRun = true;
      let tab = await chrome.tabs.create({url, active:false, windowId});
      targetId = tabId(tab.id);
      created = true;
      rememberTab(tab);
      phase = "navigation_wait";
      tab = await edgeBackgroundWaitExact(targetId, Math.max(1, Math.min(Number(params.timeout || 20000) - 500, 20000)), true, url);
      value = {tab_id:String(targetId), requested_url:url, final_url:String(tab.url || ""), url:String(tab.url || ""),
        created:true, navigation_verified:true, navigation_status:tab.status, active:tab.active === true, verified:tab.active === false};
    } else if (method === "close_tab") {
      await edgeBackgroundGuard(params, expected, targetId);
      actionMayHaveRun = true;
      await chrome.tabs.remove(targetId);
      value = {tab_id:String(targetId), closed:true, verified:true};
    } else if (method === "navigate") {
      const url = String(params.url || "");
      if (!edgeBackgroundSafeUrl(url)) throw new Error("extension_background_url_denied");
      await edgeBackgroundGuard(params, expected, targetId);
      const previous = await chrome.tabs.get(targetId);
      actionMayHaveRun = true;
      await chrome.tabs.update(targetId, {url});
      phase = "navigation_wait";
      const tab = await edgeBackgroundWaitExact(targetId, Math.max(1, Math.min(Number(params.timeout || 30000) - 500, 120000)), true, url, String(previous.url || ""));
      value = {tab_id:String(targetId), requested_url:url, final_url:String(tab.url || ""), url:String(tab.url || ""),
        navigation_verified:true, navigation_status:tab.status, verified:true};
    } else if (method === "wait") {
      await new Promise(resolve => setTimeout(resolve, Math.max(0,Math.min(120000,Number(params.milliseconds || 0)))));
      value = {tab_id:String(targetId), verified:true};
    } else {
      const tab = await chrome.tabs.get(targetId);
      if (!edgeBackgroundSafeUrl(tab.url)) throw new Error("extension_background_page_denied");
      await edgeBackgroundGuard(params, expected, targetId);
      const target = {tabId:targetId, frameIds:[params.frame_id === undefined ? 0 : params.frame_id]};
      let response;
      phase = "script_dispatch";
      if (method === "read_page") {
        // Never call readPage/recoverReadFromDuplicate: a background observation
        // belongs to this exact existing tab, including failures.
        response = [{result:await edgeReadPageExact(targetId,params)}];
      } else if (method === "click") {
        actionMayHaveRun = true;
        response=[{result:await edgePerformObservedClick(tab,params,
          id=>edgeBackgroundGuard(params,expected,id))}];
      } else {
        await edgeBackgroundExpectedTarget(params,targetId);
        actionMayHaveRun = true;
        response = await chrome.scripting.executeScript({target,func:edgeBackgroundDomAction,args:[params,method],injectImmediately:true});
      }
      value = edgeScriptResult(response,"extension_background",actionMayHaveRun);
      phase = "script_result";
      if(value.action_may_have_run===false)actionMayHaveRun=false;
      value = {tab_id:String(targetId), url:String(tab.url || ""), ...value};
    }
    returnedValue=value;
    phase = "postflight";
    after = await edgeBackgroundGuard(params, expected, targetId, method === "close_tab");
    return {...value, background_protocol:EDGE_BACKGROUND_PROTOCOL, background_execution:{protocol:EDGE_BACKGROUND_PROTOCOL,before,after,target_inactive:true,no_system_input:true}};
  } catch (error) {
    const failure = edgeBackgroundFailure(error, phase);
    if (failure.no_effect) actionMayHaveRun = false;
    const preserved={};
    if(returnedValue && typeof returnedValue==="object") {
      for(const key of ["navigation_protocol","navigation_source","navigation_target","navigation_intent","target_tab_id","source_tab_id","operation_no_replay","dispatch_semantics","dispatched","created",
        "download_protocol","download_source","download","download_complete","goal_download_complete","download_started","download_observation","verification","recovery"])
        if(Object.hasOwn(returnedValue,key))preserved[key]=returnedValue[key];
      if(returnedValue.action_may_have_run===false)actionMayHaveRun=false;
      if(preserved.navigation_target)preserved.navigation_target={...preserved.navigation_target,navigation_verified:false};
    }
    return {...preserved,ok:false,verified:false,action_may_have_run:actionMayHaveRun,error:{code:failure.code,phase},background_protocol:EDGE_BACKGROUND_PROTOCOL,
      ...(created ? {created:true,navigation_verified:false} : {}),
      ...(targetId === null ? {} : {tab_id:String(targetId)}),
      background_execution:{protocol:EDGE_BACKGROUND_PROTOCOL,before,after,target_inactive:false,no_system_input:true}};
  }
}

function edgeBackgroundCommand(method, params, onExecute) {
  // Serialize background commands, including reads, so each baseline is checked
  // against the actual current browser state immediately before dispatch.
  const pending = edgeBackgroundQueue.then(async () => {
    if (typeof onExecute === "function") {
      try { await onExecute(); } catch (_) {} // An indicator cannot change an operation's result.
    }
    return edgeBackgroundRun(method, params);
  });
  edgeBackgroundQueue = pending.catch(() => {});
  return pending;
}
