<template>
  <div class="chat-reasoning-selector">
    <span v-if="loading" role="status">正在读取思考设置…</span>
    <template v-else-if="error">
      <span role="alert">思考设置暂不可用</span><button type="button" @click="load">重试</button>
    </template>
    <template v-else-if="capability?.enabled">
      <label :for="selectId">思考强度</label>
      <n-select :input-props="{ id: selectId }" :value="selection" :options="options" :disabled="disabled || saving" size="small" style="width:156px;max-width:100%;min-width:0" @update:value="save" />
      <span v-if="saving" role="status">保存中…</span>
    </template>
    <span v-else class="reasoning-default">思考：上游默认</span>
  </div>
</template>
<script setup>
import { computed, onBeforeUnmount, ref, watch, useId } from 'vue'
import { NSelect } from 'naive-ui'
import http from '@/utils/http'
import { message } from '@/app/ui/feedback'
const props = defineProps({ sessionId: [String, Number], appId: [String, Number], disabled: Boolean })
const emit = defineEmits(['busy'])
const selectId = useId()
const capability = ref(null), loading = ref(false), saving = ref(false), error = ref(false)
let generation = 0
const labels = { none: '关闭', off: '关闭', on: '开启', minimal: '最低', low: '低', medium: '中', high: '高', xhigh: '最高' }
const selection = computed(() => capability.value?.selection?.effort ?? '')
const options = computed(() => [{ label: `模型默认（${labels[capability.value?.default] || '默认'}）`, value: '' }, ...(capability.value?.levels || []).map(value => ({ value, label: labels[value] || value }))])
watch([loading, saving], ([a, b]) => emit('busy', a || b), { flush: 'sync' })
async function load() {
  const run = ++generation
  saving.value = false
  capability.value = null
  error.value = false
  loading.value = !!props.sessionId
  if (!props.sessionId) return
  try {
    const response = await http.get('/api/AIChats/Reasoning', { params: { sessionId: props.sessionId }, timeout: 15000 })
    if (response?.code !== 200 || response.isSuccess === false || !response.data) throw new Error('读取思考设置未成功')
    if (run === generation) capability.value = response.data
  } catch { if (run === generation) error.value = true }
  finally { if (run === generation) loading.value = false }
}
async function save(effort) {
  if (saving.value || props.disabled || !capability.value) return
  const run = generation
  saving.value = true
  try {
    const response = await http.put('/api/AIChats/Reasoning', { sessionId: String(props.sessionId), selection: effort ? { modelId: String(capability.value.modelId), effort } : null }, { timeout: 15000 })
    if (response?.code !== 200 || response.isSuccess === false || !response.data) throw new Error('保存思考设置未成功')
    if (run === generation) capability.value = response.data
  } catch {
    if (run === generation) {
      message.error('思考强度保存未确认，正在重新读取设置；请确认后重试')
      await load()
    }
  }
  finally { if (run === generation) saving.value = false }
}
watch(() => [props.sessionId, props.appId], load, { immediate: true })
onBeforeUnmount(() => { generation++; emit('busy', false) })
</script>
<style scoped>
.chat-reasoning-selector { display:flex; flex-wrap:wrap; align-items:center; gap:8px; font-size:var(--eic-font-caption, 13px); }
.reasoning-default { color:var(--eic-muted); }
.chat-reasoning-selector button { border:1px solid var(--eic-line); background:var(--eic-surface); color:var(--eic-text); border-radius:6px; cursor:pointer; padding:4px 10px; }
</style>
