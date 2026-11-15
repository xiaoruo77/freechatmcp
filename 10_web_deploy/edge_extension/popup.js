const status = document.getElementById("status");
const button = document.getElementById("reconnect");
const activity = document.getElementById("activity");
const version = document.getElementById("version");

function render(value) {
  const online = Boolean(value && value.connected);
  status.className = online ? "on" : "off";
  status.textContent = online ? "桥接已连接" : "桥接未连接：请确认本机服务正在运行";
  version.textContent = value && value.version ? " · v" + value.version : "";
  activity.replaceChildren();
  const state = value && value.activity || {};
  const current = Array.isArray(state.current) ? state.current : [];
  const recent = Array.isArray(state.recent) ? state.recent : [];
  const heading = document.createElement("div");
  heading.className = "activity-title";
  heading.textContent = current.length ? "正在处理 " + current.length + " 项操作" : "当前没有操作";
  activity.appendChild(heading);
  const labels={running:"正在操作",queued:"等待执行",completed:"已完成",needs_check:"结果待核对",refused:"未执行"};
  for (const item of [...current,...recent].slice(0,5)) {
    const row=document.createElement("div");
    row.className="item " + (Object.hasOwn(labels,item.state) ? item.state : "needs_check");
    row.textContent=(labels[item.state] || "状态待核对") + " · " + String(item.action || "网页操作").slice(0,32);
    const detail=document.createElement("span");
    detail.className="detail";
    detail.textContent=String(item.tab_title || (item.tab_id ? "标签页 " + item.tab_id : "新标签页")).slice(0,100);
    row.appendChild(detail);
    activity.appendChild(row);
  }
}

function refresh() {
  chrome.runtime.sendMessage({action: "status"}, value=>{
    if (chrome.runtime.lastError) render(null);
    else render(value);
  });
}

button.addEventListener("click", () => {
  chrome.runtime.sendMessage({action: "reconnect"}, () => setTimeout(refresh, 300));
});
refresh();
const refreshTimer=setInterval(refresh,700);
window.addEventListener("unload",()=>clearInterval(refreshTimer));
