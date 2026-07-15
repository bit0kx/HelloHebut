<template>
  <main class="app-shell">
    <aside class="sidebar">
      <section class="brand">
        <div class="brand-mark">HE</div>
        <div>
          <h1>河工大报考咨询</h1>
          <p>普通本科招生智能咨询</p>
        </div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <h2>后端</h2>
          <span class="pill">{{ currentBackend.label }}</span>
        </div>
        <div class="segmented">
          <button :class="{ active: settings.backend === 'java' }" @click="switchBackend('java')">Java</button>
          <button :class="{ active: settings.backend === 'python' }" @click="switchBackend('python')">Python</button>
        </div>

        <label>
          <span>Java API</span>
          <input v-model="settings.endpoints.java" @change="persist" placeholder="/api/java" />
        </label>
        <label>
          <span>Python API</span>
          <input v-model="settings.endpoints.python" @change="persist" placeholder="/api/python" />
        </label>
        <label>
          <span>用户 ID</span>
          <input v-model="settings.userId" @change="persist" placeholder="u1001" />
        </label>
        <label>
          <span>会话 ID</span>
          <input v-model="settings.conversationId" @change="persist" placeholder="自动生成" />
        </label>
        <label>
          <span>管理令牌（仅保存在当前页面内存）</span>
          <input v-model="adminToken" type="password" autocomplete="off" placeholder="知识导入时需要" />
        </label>

        <div class="actions">
          <button @click="checkHealth">健康检查</button>
          <button @click="loadStats">刷新状态</button>
        </div>
      </section>

      <section class="panel status-panel">
        <div class="panel-heading">
          <h2>状态</h2>
          <span :class="['status-dot', healthOk ? 'online' : 'offline']"></span>
        </div>
        <dl>
          <div>
            <dt>当前后端</dt>
            <dd>{{ currentBackend.label }}</dd>
          </div>
          <div>
            <dt>健康状态</dt>
            <dd :class="healthOk ? 'ok' : 'muted'">{{ healthLabel }}</dd>
          </div>
          <div>
            <dt>知识片段</dt>
            <dd>{{ knowledgeCount }}</dd>
          </div>
        </dl>
        <pre v-if="statusText">{{ statusText }}</pre>
      </section>
    </aside>

    <section class="workspace">
      <header class="workspace-header">
        <div>
          <span class="eyebrow">HEBUT Undergraduate Admissions</span>
          <h2>本科报考咨询</h2>
          <p>{{ currentBackend.baseUrl }}</p>
        </div>
        <div class="header-actions">
          <a href="https://zs.hebut.edu.cn/" target="_blank" rel="noreferrer">本科招生网</a>
          <a :href="docsUrl" target="_blank" rel="noreferrer">API 文档</a>
        </div>
      </header>

      <section class="chat-panel">
        <div class="messages" ref="messageList">
          <article v-for="item in messages" :key="item.id" :class="['message', item.role]">
            <div class="message-meta">
              <span>{{ item.role === 'user' ? '用户' : currentBackend.label }}</span>
              <small v-if="item.meta">{{ item.meta }}</small>
            </div>
            <p>{{ item.content }}</p>
            <ul v-if="item.citations?.length" class="citations">
              <li v-for="(citation, index) in item.citations" :key="`${item.id}-${index}`">
                <a v-if="citation.url" :href="citation.url" target="_blank" rel="noreferrer">
                  来源{{ index + 1 }}：{{ citation.title }}
                </a>
                <span v-else>来源{{ index + 1 }}：{{ citation.title }}</span>
                <small v-if="citation.effective_year">（{{ citation.effective_year }}）</small>
              </li>
            </ul>
          </article>
          <div v-if="messages.length === 0" class="empty-state">
            <h3>开始一次本科报考咨询</h3>
            <p>咨询录取风险时，请尽量提供年份、省份、选科、位次和目标专业。</p>
          </div>
        </div>

        <form class="composer" @submit.prevent="sendMessage">
          <textarea v-model="draft" rows="3" placeholder="例如：2026年河北物理类、位次12000，报电气专业风险如何？"></textarea>
          <button :disabled="busy || !draft.trim()">{{ busy ? '发送中' : '发送' }}</button>
        </form>
      </section>

      <section class="tools-grid">
        <article class="tool-panel">
          <div class="panel-heading">
            <h2>知识库检索</h2>
            <span class="pill soft">RAG</span>
          </div>
          <div class="inline-form">
            <input v-model="searchQuery" placeholder="2026年专业录取规则" />
            <button @click="searchKnowledge" :disabled="busy || !searchQuery.trim()">检索</button>
          </div>
          <div class="result-list">
            <article v-for="item in searchResults" :key="item.id || item.title" class="result-item">
              <strong>{{ item.title || '未命名结果' }}</strong>
              <span>相关性 {{ item.rerank_score ?? '-' }}/10</span>
              <p>{{ item.content }}</p>
            </article>
          </div>
        </article>

        <article class="tool-panel">
          <div class="panel-heading">
            <h2>导入知识</h2>
            <span class="pill soft">Docs</span>
          </div>
          <label>
            <span>标题</span>
            <input v-model="docTitle" placeholder="2026年招生政策补充" />
          </label>
          <label>
            <span>内容</span>
            <textarea v-model="docContent" rows="5" placeholder="输入知识库内容"></textarea>
          </label>
          <label>
            <span>河北工业大学官方来源 URL</span>
            <input v-model="docSourceUrl" placeholder="https://zs.hebut.edu.cn/..." />
          </label>
          <div class="actions">
            <button @click="submitKnowledge" :disabled="busy || !docTitle.trim() || !docContent.trim() || !docSourceUrl.trim()">添加文档</button>
            <label class="file-button">
              上传文件
              <input type="file" accept=".txt,.md,.json" @change="handleUpload" />
            </label>
          </div>
        </article>
      </section>
    </section>
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import {
  addKnowledge,
  backendMeta,
  createInitialSettings,
  requestChat,
  requestHealth,
  requestKnowledgeStats,
  requestMonitor,
  requestSearch,
  saveSettings,
  uploadKnowledge
} from './lib/backends'

const settings = reactive(createInitialSettings())
const messages = ref([])
const draft = ref('')
const busy = ref(false)
const healthOk = ref(false)
const healthLabel = ref('未检查')
const statusText = ref('')
const knowledgeCount = ref('-')
const adminToken = ref('')
const searchQuery = ref('2026年专业录取规则')
const searchResults = ref([])
const docTitle = ref('2026年招生政策补充')
const docContent = ref('请粘贴河北工业大学官方发布内容，并在管理端同时记录来源链接和发布日期。')
const docSourceUrl = ref('https://zs.hebut.edu.cn/')
const messageList = ref(null)

const currentBackend = computed(() => backendMeta(settings.backend, settings))
const docsUrl = computed(() => {
  if (settings.backend === 'java') return `${currentBackend.value.baseUrl}/docs`
  return `${currentBackend.value.baseUrl}/docs`
})

watch(
  () => settings.conversationId,
  () => persist()
)

onMounted(() => {
  checkHealth()
  loadStats()
})

function createId() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID()
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

function switchBackend(type) {
  settings.backend = type
  persist()
  healthOk.value = false
  healthLabel.value = '未检查'
  statusText.value = ''
  searchResults.value = []
  checkHealth()
}

function persist() {
  saveSettings(settings)
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content) return
  messages.value.push({ id: createId(), role: 'user', content })
  draft.value = ''
  busy.value = true
  try {
    const response = await requestChat(settings.backend, settings, content)
    if (response.conversationId && !settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    const meta = [
      response.intent,
      response.agentType,
      response.admissionDataUsed ? '结构化录取数据' : '',
      response.knowledgeUsed && !response.admissionDataUsed ? 'RAG' : '',
      response.escalated ? '转人工' : ''
    ].filter(Boolean).join(' · ')
    messages.value.push({
      id: createId(),
      role: 'assistant',
      content: response.response,
      meta,
      citations: response.citations
    })
  } catch (error) {
    messages.value.push({
      id: createId(),
      role: 'assistant',
      content: error.message,
      meta: '请求失败'
    })
  } finally {
    busy.value = false
    await nextTick()
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
  }
}

async function checkHealth() {
  try {
    const data = await requestHealth(settings.backend, settings)
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    healthOk.value = false
    healthLabel.value = '不可用'
    statusText.value = error.message
  }
}

async function loadStats() {
  try {
    const [stats, monitor] = await Promise.allSettled([
      requestKnowledgeStats(settings.backend, settings),
      requestMonitor(settings.backend, settings)
    ])
    if (stats.status === 'fulfilled') {
      knowledgeCount.value = stats.value.total_chunks ?? stats.value.totalChunks ?? '-'
    }
    if (monitor.status === 'fulfilled') {
      statusText.value = JSON.stringify(monitor.value, null, 2)
    }
  } catch (error) {
    statusText.value = error.message
  }
}

async function searchKnowledge() {
  busy.value = true
  try {
    const data = await requestSearch(settings.backend, settings, searchQuery.value, 5)
    searchResults.value = data.results || []
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function submitKnowledge() {
  busy.value = true
  try {
    const data = await addKnowledge(settings.backend, settings, [
      { title: docTitle.value.trim(), content: docContent.value.trim(), source_url: docSourceUrl.value.trim() }
    ], adminToken.value)
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  busy.value = true
  try {
    const data = await uploadKnowledge(settings.backend, settings, file, docSourceUrl.value.trim(), adminToken.value)
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}
</script>
