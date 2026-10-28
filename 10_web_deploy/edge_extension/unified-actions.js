// Fixed scoped actions for explicit frames and open Shadow DOM; no eval.
async function scopedPageAction(params,action) {
  function unique(root,selector) {
    if(typeof selector!=="string" || !selector || selector.length>2000)throw new Error("scoped_selector_required");
    const matches=root.querySelectorAll(selector);
    if(matches.length!==1)throw new Error("scoped_target_ambiguous");
    return matches[0];
  }
  let root=document;
  for(const selector of params.shadow_path||[]) {const host=unique(root,selector);if(!host.shadowRoot)throw new Error("open_shadow_root_unavailable");root=host.shadowRoot;}
  if(action==="read") {
    const area=params.selector?unique(root,params.selector):root;
    const controls=Array.from(area.querySelectorAll("input,textarea,button,select,a,[contenteditable=true]")).slice(0,100).map(el=>({tag:el.tagName.toLowerCase(),id:el.id||"",type:el.type||"",selector:el.id?"#"+CSS.escape(el.id):el.tagName.toLowerCase(),disabled:Boolean(el.disabled),text:String(el.innerText||el.textContent||"").slice(0,100)}));
    return {url:String(location.href),text:String(area.innerText||area.textContent||"").slice(0,Math.max(100,Math.min(100000,params.max_chars||20000))),interactive_elements:controls,scope:"explicit_frame_open_shadow",verified:true};
  }
  const el=unique(root,params.selector);
  if(el.disabled)throw new Error("scoped_target_disabled");
  if(el.type==="password" || el.type==="file")throw new Error("scoped_sensitive_or_file_input_denied");
  const before={scopeText:String(root.textContent||""),url:String(location.href),body:String(document.body&&document.body.innerText||""),checked:el.checked,expanded:el.getAttribute("aria-expanded"),pressed:el.getAttribute("aria-pressed"),value:el.value};
  if(action==="click")el.click();
  else if(action==="type") {
    if(!["INPUT","TEXTAREA"].includes(el.tagName) && !el.isContentEditable)throw new Error("scoped_editable_target_required");
    const text=String(params.text||"");const value=(params.clear===false?String(el.value||el.textContent||""):"")+text;
    if(el.isContentEditable)el.textContent=value;
    else {const proto=el.tagName==="TEXTAREA"?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,"value").set.call(el,value);}
    el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));
    return {verified:(el.isContentEditable?el.textContent:el.value)===value,typed:true,value_length:value.length,url:String(location.href)};
  } else if(action==="select") {
    if(el.tagName!=="SELECT")throw new Error("scoped_select_required");
    const options=Array.from(el.options);const by=params.option_by||"label";
    const match=options.filter((option,index)=>by==="index"?index===Number(params.option):by==="value"?option.value===params.option:String(option.textContent||"").trim()===params.option);
    if(match.length!==1)throw new Error("scoped_option_ambiguous");
    el.value=match[0].value;el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));
    return {verified:el.value===match[0].value,selected_index:el.selectedIndex,url:String(location.href)};
  } else if(action==="press") {
    const key=String(params.key||"");if(!["Enter","Escape","Tab","ArrowDown","ArrowUp","ArrowLeft","ArrowRight","Home","End"," "].includes(key))throw new Error("scoped_key_unsupported");
    el.focus();el.dispatchEvent(new KeyboardEvent("keydown",{key,bubbles:true,cancelable:true}));el.dispatchEvent(new KeyboardEvent("keyup",{key,bubbles:true}));
  } else throw new Error("scoped_action_unsupported");
  await new Promise(resolve=>setTimeout(resolve,100));
  const changed=before.scopeText!==String(root.textContent||"")||before.url!==String(location.href)||before.body!==String(document.body&&document.body.innerText||"")||before.checked!==el.checked||before.expanded!==el.getAttribute("aria-expanded")||before.pressed!==el.getAttribute("aria-pressed")||before.value!==el.value;
  return {verified:changed,dispatched:true,url:String(location.href),verification:changed?"observed_page_or_control_change":"no_observable_change"};
}
async function scopedAction(params,action) {
  const id=tabId(params.tab_id);const tab=await chrome.tabs.get(id);
  if(!/^https?:\/\//i.test(String(tab.url||"")))throw new Error("scoped_page_not_allowed");
  if(params.by && params.by!=="css")throw new Error("scoped_action_requires_exact_css");
  const response=await chrome.scripting.executeScript({target:{tabId:id,frameIds:[params.frame_id===undefined?0:params.frame_id]},func:scopedPageAction,args:[params,action]});
  const value=response&&response[0]&&response[0].result;
  if(!value)throw new Error("scoped_response_unavailable");
  return {tab_id:String(id),frame_id:params.frame_id||0,...value};
}
function scopedRequested(params){return params.frame_id!==undefined || (Array.isArray(params.shadow_path)&&params.shadow_path.length>0);}
async function activateTab(params) {
  const id=tabId(params.tab_id);const tab=await chrome.tabs.get(id);
  await chrome.tabs.update(id,{active:true});await chrome.windows.update(tab.windowId,{focused:true});
  const after=await chrome.tabs.get(id);
  return {tab_id:String(id),url:String(after.url||""),active:after.active===true,verified:after.active===true};
}
function scrollInPage(params) {
  const matches=params.selector?document.querySelectorAll(params.selector):[document.scrollingElement];
  if(matches.length!==1 || !matches[0])throw new Error("scroll_target_ambiguous");
  const target=matches[0];const before={x:target.scrollLeft,y:target.scrollTop};
  const amount=Math.max(1,Math.min(100000,Math.abs(Number(params.pixels||600))));
  const direction=params.direction||"down";if(!["down","up","left","right"].includes(direction))throw new Error("scroll_direction_invalid");
  target.scrollBy({left:direction==="left"?-amount:direction==="right"?amount:0,top:direction==="up"?-amount:direction==="down"?amount:0,behavior:"instant"});
  const after={x:target.scrollLeft,y:target.scrollTop};const changed=before.x!==after.x||before.y!==after.y;
  return {verified:changed,scrolled:changed,before,after,url:String(location.href),verification:changed?"scroll_position_changed":"scroll_boundary_or_no_change"};
}
async function scrollTab(params) {
  const id=tabId(params.tab_id);const tab=await chrome.tabs.get(id);
  if(!/^https?:\/\//i.test(String(tab.url||"")))throw new Error("scroll_page_not_allowed");
  const response=await chrome.scripting.executeScript({target:{tabId:id,frameIds:[params.frame_id===undefined?0:params.frame_id]},func:scrollInPage,args:[params]});
  const value=response&&response[0]&&response[0].result;if(!value)throw new Error("scroll_response_unavailable");
  return {tab_id:String(id),...value};
}