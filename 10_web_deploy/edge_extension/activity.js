// Ephemeral command notices. No selectors, page contents, form data or URLs.
const EDGE_ACTIVITY_LABELS = Object.freeze({open_tab:"打开后台标签",navigate:"打开网页",read_page:"读取网页",
  click:"点击网页元素",type_text:"填写网页内容",press_key:"操作网页按键",select_option:"选择网页选项",
  scroll:"滚动网页",wait:"等待页面",screenshot:"读取页面截图",close_tab:"关闭标签页",
  upload_file:"选择授权文件",activate_tab:"切换标签页",debug_attach:"连接网页调试",debug_detach:"结束网页调试",
  debug_snapshot:"读取网页结构",debug_screenshot:"读取调试截图"});
const edgeActivityItems = new Map();
const edgeActivityRecent = [];
const edgeActivityTabs = new Set();
let edgeActivityOnline = false;
let edgeActivityRevision = 0;
const EDGE_ACTIVITY_UI_DEADLINE_MS = 120;

function edgeActivityNextRevision() {
  // Wall-clock scale remains ordered across normal service-worker restarts;
  // the sequence also orders updates issued within the same millisecond.
  edgeActivityRevision=Math.max(edgeActivityRevision+1,Date.now()*1000);
  return edgeActivityRevision;
}

function edgeActivityTabId(value) {
  const number = Number(value);
  return value !== null && value !== undefined && value !== "" && Number.isSafeInteger(number) && number >= 0 ? number : null;
}
function edgeActivityPublic(item) {
  return {command_id:item.id,tab_id:item.tabId === null ? null : String(item.tabId),
    tab_title:item.title,action:item.label,state:item.state,started_at:item.started,ended_at:item.ended || null};
}
function edgeActivitySnapshot() {
  const now = Date.now();
  return {running:Array.from(edgeActivityItems.values()).filter(item=>item.state === "running").length,
    queued:Array.from(edgeActivityItems.values()).filter(item=>item.state === "queued").length,
    current:Array.from(edgeActivityItems.values()).slice(-8).map(edgeActivityPublic),
    recent:edgeActivityRecent.filter(item=>now-item.ended<60000).slice(0,5).map(edgeActivityPublic)};
}
async function edgeActivitySafe(call, timeout = 200) {
  let timer=null;
  try {
    const operation=Promise.resolve().then(call);
    const deadline=new Promise(resolve=>{timer=setTimeout(()=>resolve(null),timeout);});
    return await Promise.race([operation,deadline]);
  } catch (_) { return null; }
  finally { if(timer!==null)clearTimeout(timer); }
}
function edgeActivityLatest(tabId) {
  const current = Array.from(edgeActivityItems.values()).filter(item=>tabId === undefined || item.tabId === tabId);
  return current.reverse().find(item=>item.state === "running") || current[0]
    || edgeActivityRecent.find(item=>(tabId === undefined || item.tabId === tabId) && Date.now()-item.ended<12000) || null;
}
async function edgeActivityPaint(item, tabId, otherTab = false) {
  const badges={running:"运行",queued:"等待",completed:"完成",needs_check:"核对",refused:"未执行"};
  const colors={running:"#2563eb",queued:"#475569",completed:"#15803d",needs_check:"#b45309",refused:"#64748b"};
  const labels={running:"正在操作",queued:"等待执行",completed:"本次操作已完成",needs_check:"操作结果待核对",refused:"本次操作未执行"};
  const target = tabId === undefined ? {} : {tabId};
  const tooltip = item ? "Xiaoruo1：" + labels[item.state] + " · " + item.label + "\n" + (otherTab ? "其他标签页：" : "标签页：") + (item.title || (item.tabId === null ? "新标签" : String(item.tabId)))
    : "Xiaoruo1：" + (edgeActivityOnline ? "已连接，当前没有操作" : "未连接");
  await Promise.all([
    edgeActivitySafe(()=>chrome.action.setBadgeText({...target,text:item ? badges[item.state] : edgeActivityOnline ? "ON" : "OFF"}),EDGE_ACTIVITY_UI_DEADLINE_MS),
    edgeActivitySafe(()=>chrome.action.setBadgeBackgroundColor({...target,color:item ? colors[item.state] : edgeActivityOnline ? "#15803d" : "#6b7280"}),EDGE_ACTIVITY_UI_DEADLINE_MS),
    edgeActivitySafe(()=>chrome.action.setTitle({...target,title:tooltip}),EDGE_ACTIVITY_UI_DEADLINE_MS),
  ]);
}
async function edgeActivityRefresh() {
  const global = edgeActivityLatest();
  await Promise.all([edgeActivityPaint(global),...Array.from(edgeActivityTabs).map(async id=>{
    const own = edgeActivityLatest(id);
    await edgeActivityPaint(own || global, id, !own && Boolean(global));
  })]);
}
async function edgeActivityNotice(item, state = item.state) {
  if (item.tabId === null || !chrome.scripting || typeof edgeActivityMarker !== "function") return;
  await edgeActivitySafe(async()=>{
    const tab = await chrome.tabs.get(item.tabId);
    if (!/^https?:\/\//i.test(String(tab.url || ""))) return;
    await chrome.scripting.executeScript({target:{tabId:item.tabId},func:edgeActivityMarker,
      args:[{state,action:item.label,revision:item.revision,issued_at:item.issued,
        expires_at:item.issued+(state === "running" || state === "queued" ? 150000 : state === "needs_check" ? 8000 : 5000)}]});
  },EDGE_ACTIVITY_UI_DEADLINE_MS);
}
async function edgeActivityTitle(item) {
  if (item.tabId === null) return;
  const tab = await edgeActivitySafe(()=>chrome.tabs.get(item.tabId),EDGE_ACTIVITY_UI_DEADLINE_MS);
  const title = tab && typeof tab.title === "string" ? tab.title.replace(/[\u0000-\u001f\u007f]/g," ").trim() : "";
  item.title = /^\w+:\/\//.test(title) ? "标签页 " + item.tabId : title.slice(0,100);
  edgeActivityTabs.add(item.tabId);
  if (edgeActivityTabs.size>100) edgeActivityTabs.delete(edgeActivityTabs.values().next().value);
}
function edgeActivityPublish(item) {
  const update={...item,revision:edgeActivityNextRevision(),issued:Date.now()};
  // Activity UI is strictly best effort. A suspended renderer or unresolved
  // Chrome action promise must never hold the real browser command queue.
  void edgeActivitySafe(async()=>{
    await edgeActivityTitle(item);
    await Promise.all([edgeActivityRefresh(),edgeActivityNotice(update)]);
  },250);
}
async function edgeActivityBegin(id, method, params, queued = false) {
  if (!Object.hasOwn(EDGE_ACTIVITY_LABELS,method) || typeof id !== "string" || !id || edgeActivityItems.has(id)) return null;
  const item={id:id.slice(0,160),method,label:EDGE_ACTIVITY_LABELS[method],tabId:edgeActivityTabId(params && params.tab_id),
    title:"",state:queued ? "queued" : "running",started:Date.now(),timer:null};
  edgeActivityItems.set(item.id,item);
  item.timer=setTimeout(()=>edgeActivityFinish(item.id,{verified:false,action_may_have_run:item.state === "running"}),132000);
  edgeActivityPublish(item);
  return item.id;
}
async function edgeActivityExecuting(id) {
  const item=edgeActivityItems.get(id);
  if (!item || item.state === "running") return;
  item.state="running";
  edgeActivityPublish(item);
}
async function edgeActivityFinish(id, value, failure = null) {
  const item=edgeActivityItems.get(id);
  if (!item) return;
  if (item.timer) clearTimeout(item.timer);
  const target=edgeActivityTabId(value && value.tab_id);
  if (item.tabId === null && target !== null) item.tabId=target;
  const refused=value && value.action_may_have_run === false;
  const failed=Boolean(failure || value && (value.ok === false || value.error));
  item.state=refused ? "refused" : failed || value && value.verified === false ? "needs_check" : "completed";
  item.ended=Date.now();
  edgeActivityItems.delete(id);
  edgeActivityRecent.unshift(item);
  edgeActivityRecent.splice(8);
  const own=edgeActivityLatest(item.tabId);
  edgeActivityPublish(own || item);
  setTimeout(()=>edgeActivityRefresh(),12050);
}
function edgeActivityConnection(online) {
  edgeActivityOnline=online === true;
  void edgeActivityRefresh();
}
function edgeActivityRemoved(tabId) {
  edgeActivityTabs.delete(tabId);
}
