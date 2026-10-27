const TOKEN_KEY = 'web_agent_token'

export function getToken() {
  const fromUrl = new URLSearchParams(window.location.search).get('token')
  if (fromUrl) {
    localStorage.setItem(TOKEN_KEY, fromUrl)
    const url = new URL(window.location.href)
    url.searchParams.delete('token')
    window.history.replaceState({}, '', url)
    return fromUrl
  }
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token.trim())
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {})
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(path, { ...options, headers })
  const text = await response.text()
  let data
  try { data = text ? JSON.parse(text) : null } catch { data = { detail: text } }
  if (!response.ok) {
    const error = new Error(data?.detail || `Request failed: ${response.status}`)
    error.status = response.status
    throw error
  }
  return data
}
