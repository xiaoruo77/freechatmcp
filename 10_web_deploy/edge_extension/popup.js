const status = document.getElementById("status");
const button = document.getElementById("reconnect");

function render(value) {
  const online = Boolean(value && value.connected);
  status.className = online ? "on" : "off";
  status.textContent = online ? "已连接：FreeChatMCP 可以控制当前 Edge 标签页" : "未连接：请确认本机桥接服务正在运行";
}

function refresh() {
  chrome.runtime.sendMessage({action: "status"}, render);
}

button.addEventListener("click", () => {
  chrome.runtime.sendMessage({action: "reconnect"}, () => setTimeout(refresh, 300));
});
refresh();