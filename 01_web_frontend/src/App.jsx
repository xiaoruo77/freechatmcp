import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, clearToken, getToken, setToken } from './api.js'

const PRODUCT_NAME = 'Xiaoruo1'
const pretty = (value) => JSON.stringify(value, null, 2)
const DEFAULT_MD_WORKFLOW = {
  script_path: '',
  cwd: '',
  timeout: 3600,
  args: [],
}

function Login({ onLogin }) {
  const [value, setValue] = useState('')
  const [error, setError] = useState('')
  async function submit(event) {
    event.preventDefault()
    if (!value.trim()) return
    setToken(value)
    try {
      await api('/api/config')
      onLogin()
    } catch {
      clearToken()
      setError('Token 无效，请读取 08_web_data/web_agent.token 后重试。')
    }
  }
  return <main className="login-shell">
    <form className="login-card" onSubmit={submit}>
      <div className="eyebrow">SELF-HOSTED AI WORKSPACE</div>
      <h1>{PRODUCT_NAME}</h1>
      <p>输入本机生成的访问 Token，进入个人 Agent 控制台。</p>
      <input autoFocus type="password" value={value} onChange={(e) => setValue(e.target.value)} placeholder="Bearer token" />
      <button className="primary" type="submit">连接本地服务</button>
      {error && <div className="error-box">{error}</div>}
    </form>
  </main>
}

function Sidebar({ conversations, activeId, onSelect, onNew, onLogout }) {
  return <aside className="sidebar">
    <div className="brand"><span className="brand-dot" />{PRODUCT_NAME}</div>
    <button className="new-chat" onClick={onNew}>＋ 新建对话</button>
    <div className="section-label">对话记录</div>
    <div className="conversation-list">
      {conversations.map((item) => <button key={item.id} className={`conversation-item ${item.id === activeId ? 'active' : ''}`} onClick={() => onSelect(item.id)}>
        <span>{item.title || 'New conversation'}</span><small>{new Date(item.updated_at).toLocaleDateString()}</small>
      </button>)}
      {!conversations.length && <div className="muted">还没有对话</div>}
    </div>
    <div className="sidebar-bottom"><div className="status-pill"><span />本地服务在线</div><button className="ghost-button" onClick={onLogout}>退出</button></div>
  </aside>
}

function ChatPanel({ conversation, tools, onSend, busy }) {
  const [message, setMessage] = useState('')
  const [toolName, setToolName] = useState('')
  const [toolArgs, setToolArgs] = useState('{}')
  const canSend = message.trim() && !busy
  function submit(event) {
    event.preventDefault()
    if (!canSend) return
    let parsed = {}
    if (toolName) {
      try { parsed = JSON.parse(toolArgs || '{}') } catch { return window.alert('工具参数必须是合法 JSON') }
    }
    onSend(message, toolName || null, parsed)
    setMessage('')
  }
  return <section className="chat-panel">
    <div className="panel-heading"><div><div className="eyebrow">WEB CHAT</div><h2>{conversation?.title || '新对话'}</h2></div><div className="connection-label">MCP · Streamable HTTP</div></div>
    <div className="messages">
      {!conversation?.messages?.length && <div className="empty-state"><div className="empty-icon">✦</div><h3>你的本地 Agent 工作台</h3><p>可以直接聊天，也可以选择工具调用文件、项目、任务与 Canvas。</p><code>/tool list_directory {`{"path":"."}`}</code></div>}
      {conversation?.messages?.map((item) => <div key={item.id} className={`message-row ${item.role}`}><div className="message-avatar">{item.role === 'user' ? '你' : item.role === 'tool' ? '⚙' : '✦'}</div><div className="message-bubble"><div className="message-role">{item.role === 'user' ? '你' : item.role === 'tool' ? item.tool_name || '工具' : PRODUCT_NAME}</div><div className="message-content">{item.content}</div></div></div>)}
      {busy && <div className="message-row assistant"><div className="message-avatar">✦</div><div className="message-bubble"><div className="typing"><i /><i /><i /></div></div></div>}
    </div>
    <form className="composer" onSubmit={submit}>
      <div className="tool-row"><select value={toolName} onChange={(e) => setToolName(e.target.value)}><option value="">普通消息（或输入 /tool）</option>{tools.map((tool) => <option key={tool.name} value={tool.name}>{tool.name}</option>)}</select>{toolName && <input value={toolArgs} onChange={(e) => setToolArgs(e.target.value)} aria-label="工具参数 JSON" placeholder="工具参数 JSON" />}</div>
      <div className="composer-row"><textarea value={message} onChange={(e) => setMessage(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(e) } }} placeholder="输入消息，Enter 发送，Shift+Enter 换行" rows={3} /><button className="send-button" disabled={!canSend} type="submit">发送 ↑</button></div>
      <div className="composer-hint">工具执行会进入任务队列；长期任务请在右侧 Tasks 面板查看。</div>
    </form>
  </section>
}

function TasksPanel({ tasks, onRefresh, onCancel }) {
  return <section className="side-panel"><div className="panel-title"><span>Tasks</span><button className="icon-button" onClick={onRefresh}>↻</button></div><div className="task-list">{tasks.map((task) => <div className="task-card" key={task.id}><div className="task-card-top"><strong>{task.profile}</strong><span className={`status status-${task.status}`}>{task.status}</span></div><div className="task-id">{task.id}</div><div className="task-time">{new Date(task.created_at).toLocaleString()}</div>{['queued', 'running'].includes(task.status) && <button className="tiny-button" onClick={() => onCancel(task.id)}>取消任务</button>}{task.error && <div className="task-error">{task.error}</div>}</div>)}{!tasks.length && <div className="muted">暂无任务</div>}</div></section>
}

function MdWorkflowPanel({ tasks, onStart, onCancel }) {
  const latest = [...tasks]
    .filter((task) => task.profile === 'md_simulation')
    .sort((left, right) => new Date(right.created_at) - new Date(left.created_at))[0]
  const active = latest && ['queued', 'running'].includes(latest.status)
  const stages = ['准备', 'EM', 'NVT', 'NPT']
  const stageIndex = latest?.status === 'success' ? 4 : latest?.status === 'running' ? 1 : 0
  return <section className="side-panel md-panel">
    <div className="panel-title"><span>独立 MD 流程</span><span className="count-badge">EM → NVT → NPT</span></div>
    <p className="md-description">网页直接提交本机维护入口，Worker 在 WSL 中执行；结果以真实任务终态和运行目录日志为准。</p>
    <div className="md-stage-row">{stages.map((stage, index) => <span key={stage} className={`md-stage ${index < stageIndex ? 'done' : ''} ${index === stageIndex && active ? 'active' : ''}`}>{stage}</span>)}</div>
    <div className="md-path"><span>入口</span><code>AUTO_file/main.py</code></div>
    <div className="md-path"><span>输入</span><code>glycam5_input_20260824_1455</code></div>
    {latest && <div className="md-current"><div><span>最近任务</span><strong className={`status status-${latest.status}`}>{latest.status}</strong></div><code>{latest.id}</code>{latest.error && <div className="task-error">{latest.error}</div>}</div>}
    <button className="primary small md-start" disabled={active} onClick={() => onStart(DEFAULT_MD_WORKFLOW)}>{active ? '流程执行中…' : '启动独立 EM → NVT → NPT'}</button>
    {active && <button className="tiny-button md-cancel" onClick={() => onCancel(latest.id)}>取消当前流程</button>}
  </section>
}

function CanvasPanel({ canvases, active, onSelect, onCreate, onSave, onRestore }) {
  const [draft, setDraft] = useState('')
  useEffect(() => setDraft(active?.revision?.content || ''), [active?.id, active?.current_revision])
  return <section className="side-panel canvas-panel"><div className="panel-title"><span>Canvas</span><button className="tiny-button" onClick={onCreate}>新建</button></div><div className="canvas-select-list">{canvases.map((canvas) => <button key={canvas.id} className={`canvas-list-item ${active?.id === canvas.id ? 'active' : ''}`} onClick={() => onSelect(canvas.id)}><span>{canvas.title}</span><small>Rev {canvas.current_revision}</small></button>)}{!canvases.length && <div className="muted">暂无 Canvas</div>}</div>{active && <><div className="canvas-meta"><strong>{active.title}</strong><span>Revision {active.current_revision}</span></div><textarea className="canvas-editor" value={draft} onChange={(e) => setDraft(e.target.value)} /><div className="canvas-actions"><button className="primary small" onClick={() => onSave(active.id, draft)}>保存新版本</button><select defaultValue="" onChange={(e) => { if (e.target.value) onRestore(active.id, Number(e.target.value)); e.target.value = '' }}><option value="">历史 / 回滚</option>{(active.revisions || []).map((rev) => <option key={rev.revision_number} value={rev.revision_number}>恢复 Rev {rev.revision_number}</option>)}</select></div><div className="revision-list">{(active.revisions || []).map((rev) => <div key={rev.revision_number}><span>Rev {rev.revision_number}</span><small>{new Date(rev.created_at).toLocaleString()}</small></div>)}</div></>}</section>
}

function ToolsPanel({ tools, system }) {
  return <section className="side-panel"><div className="panel-title"><span>Tools</span><span className="count-badge">{tools.length}</span></div><div className="tool-list">{tools.map((tool) => <div className="tool-card" key={tool.name}><strong>{tool.name}</strong><span>{tool.description}</span><small>{tool.scope} · {tool.permission}</small></div>)}</div>{system && <div className="system-card"><div className="section-label">运行环境</div><div>{system.system} {system.release}</div><div>Python {system.python?.split(' ')[1]}</div><div className="muted">{system.workspace_root}</div></div>}</section>
}

export default function App() {
  const [authenticated, setAuthenticated] = useState(Boolean(getToken()))
  const [config, setConfig] = useState(null)
  const [conversations, setConversations] = useState([])
  const [conversation, setConversation] = useState(null)
  const [tools, setTools] = useState([])
  const [tasks, setTasks] = useState([])
  const [canvases, setCanvases] = useState([])
  const [activeCanvas, setActiveCanvas] = useState(null)
  const [system, setSystem] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const loadData = useCallback(async () => {
    const [cfg, conv, toolData, taskData, canvasData, sys] = await Promise.all([api('/api/config'), api('/api/conversations'), api('/api/tools'), api('/api/tasks'), api('/api/canvas'), api('/api/system')])
    setConfig(cfg); setConversations(conv.conversations); setTools(toolData.tools); setTasks(taskData.tasks); setCanvases(canvasData.canvases); setSystem(sys)
    if (conv.conversations[0]) setConversation(await api(`/api/conversations/${conv.conversations[0].id}`))
  }, [])

  useEffect(() => { if (authenticated) loadData().catch((err) => { if (err.status === 401) { clearToken(); setAuthenticated(false) } else setError(err.message) }) }, [authenticated, loadData])
  useEffect(() => { if (!authenticated) return undefined; const timer = setInterval(() => api('/api/tasks').then((data) => setTasks(data.tasks)).catch(() => {}), 2000); return () => clearInterval(timer) }, [authenticated])

  if (!authenticated) return <Login onLogin={() => setAuthenticated(true)} />

  async function newConversation() { const item = await api('/api/conversations', { method: 'POST' }); setConversations((old) => [item, ...old]); setConversation({ ...item, messages: [] }) }
  async function selectConversation(id) { setConversation(await api(`/api/conversations/${id}`)) }
  async function sendMessage(message, toolName, toolInput) { setBusy(true); setError(''); try { const result = await api('/api/chat', { method: 'POST', body: JSON.stringify({ conversation_id: conversation?.id, message, tool_name: toolName, tool_input: toolName ? toolInput : null }) }); setConversation(await api(`/api/conversations/${result.conversation_id}`)); const updated = await api('/api/conversations'); setConversations(updated.conversations); if (result.tool_result?.task_id) refreshTasks() } catch (err) { setError(err.message) } finally { setBusy(false) } }
  async function startMdWorkflow(payload) { setError(''); try { await api('/api/tasks', { method: 'POST', body: JSON.stringify({ profile: 'md_simulation', payload }) }); await refreshTasks() } catch (err) { setError(err.message) } }
  async function refreshTasks() { const data = await api('/api/tasks'); setTasks(data.tasks) }
  async function cancelTask(id) { await api(`/api/tasks/${id}/cancel`, { method: 'POST' }); refreshTasks() }
  async function createCanvas() { const item = await api('/api/canvas', { method: 'POST', body: JSON.stringify({ title: 'Untitled Canvas', content: '# New Canvas\n\n', kind: 'markdown', status: 'draft' }) }); setCanvases((old) => [item, ...old.filter((x) => x.id !== item.id)]); setActiveCanvas(item) }
  async function selectCanvas(id) { setActiveCanvas(await api(`/api/canvas/${id}`)) }
  async function saveCanvas(id, content) { setActiveCanvas(await api(`/api/canvas/${id}`, { method: 'PUT', body: JSON.stringify({ content }) })); const data = await api('/api/canvas'); setCanvases(data.canvases) }
  async function restoreCanvas(id, revision) { setActiveCanvas(await api(`/api/canvas/${id}/restore/${revision}`, { method: 'POST' })); const data = await api('/api/canvas'); setCanvases(data.canvases) }
  function logout() { clearToken(); setAuthenticated(false) }

  return <div className="app-shell">
    <Sidebar conversations={conversations} activeId={conversation?.id} onSelect={selectConversation} onNew={newConversation} onLogout={logout} />
    <ChatPanel conversation={conversation} tools={tools} onSend={sendMessage} busy={busy} />
    <aside className="right-rail"><div className="rail-tabs"><span className="active">Workspace</span><span>{config?.admin_mode ? 'ADMIN' : 'USER'}</span></div><MdWorkflowPanel tasks={tasks} onStart={startMdWorkflow} onCancel={cancelTask} /><TasksPanel tasks={tasks} onRefresh={refreshTasks} onCancel={cancelTask} /><CanvasPanel canvases={canvases} active={activeCanvas} onSelect={selectCanvas} onCreate={createCanvas} onSave={saveCanvas} onRestore={restoreCanvas} /><ToolsPanel tools={tools} system={system} /></aside>
    {error && <div className="toast error-box">{error}<button onClick={() => setError('')}>×</button></div>}
  </div>
}