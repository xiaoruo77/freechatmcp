# FreeChatMCP

把 ChatGPT 等支持 MCP 的聊天客户端，接到你自己的 Windows 电脑。在对话里布置任务，由本机执行器操作浏览器、文件和应用，再把执行结果带回对话。模型由聊天客户端提供，FreeChatMCP 本身无需另配模型 API Key；使用额度和 MCP 接入资格随客户端套餐。

**最新版本：[v0.3.0 源码与发布说明](https://github.com/xiaoruo77/freechatmcp/releases/tag/v0.3.0)** · [安装说明](https://github.com/xiaoruo77/freechatmcp/blob/v0.3.0/README.md) · [Windows 后台设置](https://github.com/xiaoruo77/freechatmcp/blob/v0.3.0/docs/BACKGROUND_WINDOWS.md)

## v0.3.0 能做什么

| 你想做的事 | FreeChatMCP 怎么做 |
| --- | --- |
| 查网页、填表、上传或下载 | 浏览器会记录操作回执，区分已发出、待确认和已观察到结果。遇到超时，可以先查回执再决定是否重试。 |
| 一边用电脑，一边让它处理任务 | 可选的 Windows 后台工作区支持独立 Edge 实例和部分应用控件。WPS 文字可在独立窗口编辑和另存，并回查保存结果；需要相应应用与后台助手。 |
| 运行课设脚本或 Jupyter Notebook | 按项目指定 Python 解释器，Notebook 用独立内核运行，原始笔记本保留。 |
| 接着做多步骤任务 | 保留目标、检查点和本机输出，方便看清做到了哪一步、用什么结果验收。 |

[v0.1.0](https://github.com/xiaoruo77/freechatmcp/releases/tag/v0.1.0) 建立了 MCP 连接与本机部署基础；[v0.2.0](https://github.com/xiaoruo77/freechatmcp/releases/tag/v0.2.0) 加入统一浏览器控制、Windows 原生 IPC 和上传；v0.3.0 把后台操作、结果核对和任务续做串了起来。

## 从这里开始

建议用 **Windows、Python 3.11+ 和 Node.js/npm**。从发布页下载源码，或直接检出 **`v0.3.0` 标签**；首页默认分支目前只更新了说明。

```powershell
git clone --branch v0.3.0 --single-branch https://github.com/xiaoruo77/freechatmcp.git
cd freechatmcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Set-Location 01_web_frontend
npm ci
npm run build
Set-Location ..
```

在项目目录的两个终端分别运行 `.\.venv\Scripts\python.exe run_server.py` 和 `.\.venv\Scripts\python.exe run_worker.py`，再打开 <http://127.0.0.1:48271>。先用 `workspace/` 里的测试文件体验，接入客户端和启用后台功能时，按上面的安装说明配置。

默认只监听本机。桌面和自定义命令权限按需开启，使用独立工作目录；连接外部客户端前先读[安全说明](https://github.com/xiaoruo77/freechatmcp/blob/v0.3.0/docs/SECURITY.md)。

[MIT 许可证](LICENSE)
