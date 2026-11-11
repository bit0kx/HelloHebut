<template>
  <section class="panel preference-panel">
    <div class="panel-heading">
      <h2>我的偏好</h2>
      <button @click="reload" :disabled="loading">刷新</button>
    </div>
    <p class="muted">确认后用于同一身份的新对话。不会保存分数、位次或替其他人继承偏好。</p>
    <p class="muted">当前为匿名身份，令牌失效后无法找回旧偏好；建议在失效前删除不再需要的记录。</p>
    <p v-if="error" role="alert" class="preference-error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <label class="preference-history">
      <input v-model="showHistory" type="checkbox" @change="reload" />显示版本记录
    </label>
    <p v-if="ready && !items.length" class="muted">暂无偏好。可在这里保存，或在聊天中表达后确认。</p>
    <article v-for="item in items" :key="item.memory_id" class="preference-item">
      <strong>{{ labels[item.fact_key] }} · {{ statusNames[item.status] }}</strong>
      <p>{{ item.value }}</p>
      <details>
        <summary>来源与有效期 · v{{ item.version }}</summary>
        <p>{{ item.source_kind === 'chat' ? '来自本人的聊天原话' : '本人在偏好面板录入' }}：{{ item.source_excerpt }}</p>
        <p>有效至 {{ new Date(item.valid_until).toLocaleDateString() }}</p>
        <small v-if="item.source_kind === 'chat'">来源消息：{{ item.source_message_id }}</small>
      </details>
      <div class="actions">
        <template v-if="item.status === 'pending'">
          <button :disabled="loading || !ready" @click="change('POST', `/${item.memory_id}/confirm`)">确认记住</button>
          <button :disabled="loading || !ready" @click="change('POST', `/${item.memory_id}/reject`)">忽略</button>
        </template>
        <template v-else>
          <button v-if="item.status === 'active'" :disabled="loading || !ready" @click="edit(item)">更正</button>
          <button :disabled="loading || !ready" @click="change('DELETE', `/${item.fact_key}`)">删除该偏好全部版本</button>
        </template>
      </div>
    </article>
    <form @submit.prevent="save">
      <label><span>偏好类别</span><select v-model="form.key"><option v-for="(label, key) in labels" :key="key" :value="key">{{ label }}</option></select></label>
      <label><span>内容</span><input v-model="form.value" maxlength="120" placeholder="例如：计算机科学与技术" required /></label>
      <label><span>有效天数</span><select v-model.number="form.valid_days"><option :value="30">30 天</option><option :value="90">90 天</option><option :value="180">180 天</option></select></label>
      <div class="actions">
        <button :disabled="loading || !ready || !form.value.trim()">保存并确认</button>
        <button type="button" :disabled="loading || !ready || !items.length" @click="change('DELETE', '')">删除全部偏好</button>
      </div>
    </form>
    <p class="muted">删除会清除偏好及其版本，不会删除聊天原文。已开始的回答可能已读取此前的偏好。</p>
  </section>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { requestMemories } from '../lib/backends'

const props = defineProps({ settings: { type: Object, required: true } })
const labels = { major_preference: '专业偏好', interests: '兴趣方向', budget_preference: '费用偏好', location_preference: '地域偏好' }
const statusNames = { pending: '待确认', active: '已确认', superseded: '已被更正', expired: '已过期' }
const items = ref([]), revision = ref(0), ready = ref(false), loading = ref(false)
const showHistory = ref(false), error = ref(''), notice = ref('')
const form = reactive({ key: 'major_preference', value: '', valid_days: 180 })
let pending = null

async function reload() {
  loading.value = true
  error.value = ''
  try {
    const result = await requestMemories(props.settings, { history: showHistory.value })
    items.value = result.items
    revision.value = result.revision
    ready.value = true
  } catch (e) {
    error.value = e.message
    ready.value = false
    items.value = []
  } finally {
    loading.value = false
  }
}

async function change(method, path, payload = {}) {
  loading.value = true
  error.value = ''
  notice.value = ''
  const body = { ...payload, expected_revision: revision.value }
  const signature = JSON.stringify([method, path, body])
  if (pending?.signature !== signature) {
    pending = { signature, id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}` }
  }
  try {
    await requestMemories(props.settings, { method, path, body: { ...body, operation_id: pending.id } })
    pending = null
    notice.value = method === 'DELETE' ? '已删除偏好及相应版本。' : '偏好已更新。'
    await reload()
  } catch (e) {
    error.value = e.message
    if (e.status === 409) {
      pending = null
      await reload()
      error.value = '偏好已变更，已刷新。请核对后重试。'
    } else if (e.status === 401) {
      ready.value = false
      items.value = []
    }
  } finally {
    loading.value = false
  }
}

function edit(item) { form.key = item.fact_key; form.value = item.value }
async function save() { await change('POST', '', { ...form, value: form.value.trim() }) }
onMounted(reload)
defineExpose({ reload })
</script>

<style scoped>
.preference-panel { font-size: 0.88rem; }
.preference-item { padding: 0.8rem 0; border-top: 1px solid #dce4ef; overflow-wrap: anywhere; }
.preference-item p { margin: 0.45rem 0; }
.preference-item details { margin-bottom: 0.6rem; }
.preference-history { display: flex; align-items: center; gap: 0.5rem; }
.preference-history input { width: auto; }
.preference-error { color: #9b2432; overflow-wrap: anywhere; }
.preference-panel select { width: 100%; padding: 0.6rem; border: 1px solid #ccd7e5; border-radius: 0.5rem; background: white; }
</style>
