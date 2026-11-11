const DEFAULT_API = {
  label: 'Python',
  baseUrl: import.meta.env.VITE_PYTHON_API_URL || '/api/python'
}

export function createInitialSettings() {
  const saved = readSettings()
  return {
    userId: saved.userId || createAnonymousUserId(),
    conversationId: saved.conversationId || '',
    sessionToken: saved.sessionToken || '',
    endpoint: saved.endpoint || saved.endpoints?.python || DEFAULT_API.baseUrl
  }
}

export function saveSettings(settings) {
  localStorage.setItem('hellohebut.frontend.settings', JSON.stringify(settings))
}

export function apiMeta(settings) {
  return {
    ...DEFAULT_API,
    baseUrl: normalizeBaseUrl(settings.endpoint || DEFAULT_API.baseUrl)
  }
}

export async function requestHealth(settings) {
  return requestJson(apiMeta(settings).baseUrl, '/health')
}

export async function requestMonitor(settings) {
  return requestJson(apiMeta(settings).baseUrl, '/monitor')
}

export async function requestKnowledgeStats(settings) {
  return requestJson(apiMeta(settings).baseUrl, '/knowledge/stats')
}

export async function requestSearch(settings, query, topK = 5) {
  const params = new URLSearchParams({ query, top_k: String(topK) })
  return requestJson(apiMeta(settings).baseUrl, `/search?${params}`, { method: 'POST' })
}

export async function requestChat(settings, message) {
  await ensureSession(settings)
  // Keep the ID for an explicit retry of the same message after a network error.
  const pending = settings.pendingRequest
  const requestId = pending?.message === message && pending?.convId === settings.conversationId
    ? pending.id : (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`)
  settings.pendingRequest = { id: requestId, message, convId: settings.conversationId }
  let raw
  try {
    raw = await requestJson(apiMeta(settings).baseUrl, '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${settings.sessionToken}` },
      body: JSON.stringify({
        message,
        user_id: settings.userId || 'anonymous',
        conv_id: settings.conversationId || undefined,
        client_request_id: requestId
      })
    })
  } catch (error) {
    if (error.status === 401) {
      settings.sessionToken = ''
      settings.userId = ''
      settings.conversationId = ''
      delete settings.pendingRequest
      saveSettings(settings)
    }
    throw error
  }
  delete settings.pendingRequest
  return normalizeChatResponse(raw)
}

const sessionRequests = new WeakMap()

export async function ensureSession(settings) {
  if (settings.sessionToken) return
  if (!sessionRequests.has(settings)) {
    const pending = requestJson(apiMeta(settings).baseUrl, '/session', { method: 'POST' }).then(session => {
      settings.sessionToken = session.session_token
      settings.userId = session.user_id
      settings.conversationId = ''
      saveSettings(settings)
    }).finally(() => sessionRequests.delete(settings))
    sessionRequests.set(settings, pending)
  }
  await sessionRequests.get(settings)
}

export async function requestMemories(settings, { method = 'GET', path = '', body, history = false } = {}) {
  await ensureSession(settings)
  try {
    return await requestJson(apiMeta(settings).baseUrl, `/memories${path}${method === 'GET' && history ? '?history=true' : ''}`, {
      method,
      headers: { Authorization: `Bearer ${settings.sessionToken}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {})
    })
  } catch (error) {
    if (error.status === 401) {
      settings.sessionToken = ''
      settings.userId = ''
      settings.conversationId = ''
      delete settings.pendingRequest
      saveSettings(settings)
    }
    throw error
  }
}

export async function addKnowledge(settings, documents, adminToken = '') {
  return requestJson(apiMeta(settings).baseUrl, '/knowledge/add', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(adminToken ? { 'X-Admin-Token': adminToken } : {})
    },
    body: JSON.stringify({ documents })
  })
}

export async function uploadKnowledge(settings, file, sourceUrl, adminToken = '') {
  const form = new FormData()
  form.append('file', file)
  form.append('source_url', sourceUrl)
  return requestJson(apiMeta(settings).baseUrl, '/knowledge/upload', {
    method: 'POST',
    headers: adminToken ? { 'X-Admin-Token': adminToken } : {},
    body: form
  })
}

function normalizeChatResponse(raw) {
  return {
    conversationId: raw.conv_id || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    agentType: raw.agent_type || '',
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used),
    admissionDataUsed: Boolean(raw.admission_data_used),
    verified: raw.verified,
    grounded: raw.grounded,
    citations: Array.isArray(raw.citations) ? raw.citations : [],
    entities: raw.entities || {},
    raw
  }
}

async function requestJson(baseUrl, path, options = {}) {
  const url = `${normalizeBaseUrl(baseUrl)}${path}`
  const response = await fetch(url, options)
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    const error = new Error(`${response.status} ${response.statusText}: ${detail}`)
    error.status = response.status
    throw error
  }
  return data
}

function normalizeBaseUrl(value) {
  return String(value || '').replace(/\/+$/, '')
}

function readSettings() {
  try {
    return JSON.parse(localStorage.getItem('hellohebut.frontend.settings') || '{}')
  } catch {
    return {}
  }
}

function createAnonymousUserId() {
  const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`
  return `anon-${suffix}`
}
