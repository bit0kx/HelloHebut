const DEFAULT_API = {
  label: 'Python',
  baseUrl: import.meta.env.VITE_PYTHON_API_URL || '/api/python'
}

export function createInitialSettings() {
  const saved = readSettings()
  const endpoint = saved.endpoint || saved.endpoints?.python || DEFAULT_API.baseUrl
  const settings = {
    userId: saved.userId || createAnonymousUserId(),
    conversationId: saved.conversationId || '',
    sessionToken: saved.sessionToken || '',
    // Older releases stored the token and its endpoint in the same record.
    // Bind that persisted pair once; future endpoint changes cannot reuse it.
    sessionEndpoint: saved.sessionEndpoint || (saved.sessionToken ? endpointIdentity(endpoint) : ''),
    pendingRequest: saved.pendingRequest || null,
    lastConditions: saved.lastConditions || {},
    endpoint
  }
  enforceSessionEndpoint(settings)
  if (!settings.sessionToken) clearSession(settings)
  return settings
}

export function saveSettings(settings) {
  enforceSessionEndpoint(settings)
  localStorage.setItem('hellohebut.frontend.settings', JSON.stringify(settings))
}

function endpointIdentity(endpoint) {
  try {
    return normalizeBaseUrl(new URL(normalizeBaseUrl(endpoint), globalThis.location?.origin || 'http://localhost').href)
  } catch {
    return 'invalid:' + String(endpoint)
  }
}

function clearSession(settings) {
  settings.sessionToken = ''
  settings.sessionEndpoint = ''
  settings.userId = ''
  settings.conversationId = ''
  settings.lastConditions = {}
  delete settings.pendingRequest
}

function enforceSessionEndpoint(settings) {
  if (settings.sessionToken && settings.sessionEndpoint !== endpointIdentity(apiMeta(settings).baseUrl)) {
    clearSession(settings)
  }
}

function clearUnauthorizedSession(settings, token) {
  // An old request must not clear a newer session established in the meantime.
  if (settings.sessionToken === token) {
    clearSession(settings)
    saveSettings(settings)
  }
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

export async function requestChat(settings, message, onProgress = () => {}) {
  await ensureSession(settings)
  // Keep the ID for an explicit retry of the same message after a network error.
  const pending = settings.pendingRequest
  if (pending && (pending.message !== message || pending.convId !== settings.conversationId)) {
    throw new Error('还有未完成的请求，请先查询原任务，或点击“新对话”。')
  }
  if (pending?.runId) return resumeChat(settings, onProgress)
  const requestId = pending?.message === message && pending?.convId === settings.conversationId
    ? pending.id : (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`)
  settings.pendingRequest = pending?.id === requestId ? pending : { id: requestId, message, convId: settings.conversationId }
  saveSettings(settings)
  const token = settings.sessionToken
  const activePending = settings.pendingRequest
  const endpoint = apiMeta(settings).baseUrl
  try {
    const task = await requestJson(endpoint, '/chat/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${settings.sessionToken}` },
      body: JSON.stringify({
        message,
        conv_id: settings.conversationId || undefined,
        client_request_id: requestId
      })
    })
    if (settings.pendingRequest !== activePending || settings.sessionToken !== token ||
        endpointIdentity(apiMeta(settings).baseUrl) !== endpointIdentity(endpoint)) {
      throw new Error('会话已变更，原任务不会写入当前会话。')
    }
    activePending.runId = task.run_id
    saveSettings(settings)
    return await resumeChat(settings, onProgress)
  } catch (error) {
    if (error.status === 401) clearUnauthorizedSession(settings, token)
    throw error
  }
}

export async function resumeChat(settings, onProgress = () => {}) {
  enforceSessionEndpoint(settings)
  const token = settings.sessionToken
  if (!token) {
    saveSettings(settings)
    throw new Error('会话凭证已失效或后端已变更，请重新发送消息。')
  }
  const pending = settings.pendingRequest
  // A lost POST response may leave only the stable client ID. Reuse that ID.
  if (pending?.id && !pending.runId) return requestChat(settings, pending.message, onProgress)
  try {
    return await resumeOwnedChat(settings, onProgress)
  } catch (error) {
    if (error.status === 401) clearUnauthorizedSession(settings, token)
    throw error
  }
}

async function resumeOwnedChat(settings, onProgress) {
  const pending = settings.pendingRequest
  if (!pending?.runId) throw new Error('没有可恢复的任务')
  const base = apiMeta(settings).baseUrl
  const headers = { Authorization: `Bearer ${settings.sessionToken}` }
  const stillCurrent = () => settings.pendingRequest === pending &&
    headers.Authorization === `Bearer ${settings.sessionToken}` &&
    endpointIdentity(apiMeta(settings).baseUrl) === endpointIdentity(base)
  const finish = task => {
    if (!stillCurrent()) throw new Error('会话已变更，旧任务结果未写入当前会话。')
    if (task.status === 'failed') {
      delete settings.pendingRequest
      saveSettings(settings)
      throw new Error('本次任务未完成，可以重新发送。已提交的对话不会重复写入。')
    }
    if (task.status === 'completed' && task.result) {
      delete settings.pendingRequest
      settings.lastConditions = task.result.entities || {}
      settings.conversationId = task.result.conv_id
      saveSettings(settings)
      return normalizeChatResponse(task.result)
    }
    return null
  }
  let task = await requestJson(base, `/chat/tasks/${pending.runId}`, { headers })
  if (['completed', 'failed'].includes(task.status)) return finish(task)
  let reader
  try {
    const response = await fetch(`${base}/chat/tasks/${pending.runId}/events?after_sequence=${pending.sequence || 0}`, {
      headers, signal: AbortSignal.timeout(90000)
    })
    if (!response.ok || !response.body) {
      const error = new Error('进度连接不可用')
      error.status = response.status
      throw error
    }
    reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r/g, '')
      let end
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, end)
        buffer = buffer.slice(end + 2)
        const line = frame.split('\n').find(value => value.startsWith('data: '))
        if (!line) continue
        const event = JSON.parse(line.slice(6))
        if (!stillCurrent()) throw new Error('会话已变更')
        pending.sequence = event.sequence
        saveSettings(settings)
        if (event.type === 'final') { task = event; break }
        onProgress(event.stage)
      }
      if (task.type === 'final') break
    }
  } catch (error) {
    if (error.status === 401 || !stillCurrent()) throw error
    onProgress('reconnecting')
  } finally {
    await reader?.cancel().catch(() => {})
  }
  if (['completed', 'failed'].includes(task.status)) return finish(task)
  // Disconnection never resubmits a new task; query the same owner-bound durable ID.
  for (let attempt = 0; attempt < 60; attempt++) {
    task = await requestJson(base, `/chat/tasks/${pending.runId}`, { headers })
    if (['completed', 'failed'].includes(task.status)) return finish(task)
    const last = task.events?.at(-1)
    if (last) onProgress(last.stage)
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  throw new Error('任务仍在处理中。稍后点击“查询未完成任务”，会继续查询同一个任务。')
}

const sessionRequests = new WeakMap()

export async function ensureSession(settings) {
  enforceSessionEndpoint(settings)
  if (settings.sessionToken) return
  const endpoint = apiMeta(settings).baseUrl
  const identity = endpointIdentity(endpoint)
  if (sessionRequests.get(settings)?.endpoint !== identity) {
    const request = { endpoint: identity }
    request.promise = requestJson(endpoint, '/session', { method: 'POST' }).then(session => {
      if (endpointIdentity(apiMeta(settings).baseUrl) !== identity) throw new Error('后端已变更，请重试。')
      clearSession(settings)
      settings.sessionToken = session.session_token
      settings.sessionEndpoint = identity
      settings.userId = session.user_id
      settings.conversationId = ''
      saveSettings(settings)
    }).finally(() => {
      if (sessionRequests.get(settings) === request) sessionRequests.delete(settings)
    })
    sessionRequests.set(settings, request)
  }
  await sessionRequests.get(settings).promise
}

export async function requestMemories(settings, { method = 'GET', path = '', body, history = false } = {}) {
  await ensureSession(settings)
  const token = settings.sessionToken
  try {
    return await requestJson(apiMeta(settings).baseUrl, `/memories${path}${method === 'GET' && history ? '?history=true' : ''}`, {
      method,
      headers: { Authorization: `Bearer ${settings.sessionToken}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {})
    })
  } catch (error) {
    if (error.status === 401) clearUnauthorizedSession(settings, token)
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
