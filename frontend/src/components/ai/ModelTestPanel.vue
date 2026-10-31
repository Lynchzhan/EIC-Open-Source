<template>
  <n-modal v-model:show="settingsOpen" preset="card" title="Chat 自定义测试" class="model-test-settings" style="width:min(640px,calc(100vw - 24px))" :mask-closable="!saving" :closable="!saving" :close-on-esc="!saving">
    <p class="probe-note">按当前账号保存并跨设备同步。只影响 Chat 测试；输出预算和思考档位使用被测模型配置。</p>
    <n-spin v-if="loadingSettings" size="small" />
    <n-alert v-if="settingsError" type="error" class="probe-error">{{ settingsError }}<br /><e-button size="small" :disabled="saving" @click="loadSettings">重新载入</e-button></n-alert>
    <e-form layout="vertical" :disabled="saving || loadingSettings || !settingsReady">
      <e-form-item label="测试文本" required>
        <e-textarea v-model:value="settingsDraft.prompt" :rows="6" :maxlength="16000" placeholder="输入希望发送给 Chat 模型的测试内容" />
      </e-form-item>
      <e-form-item label="测试超时（秒）" required>
        <e-input-number v-model:value="settingsDraft.timeoutSeconds" :min="10" :max="300" :precision="0" style="width:100%" />
      </e-form-item>
    </e-form>
    <p class="probe-note">超时范围 10–300 秒，默认 180 秒。恢复默认仅回填表单，保存后生效；请勿在测试文本中填写密钥。</p>
    <div class="probe-actions">
      <e-button :disabled="!settingsReady || loadingSettings || saving" @click="restoreDefaults">恢复默认</e-button>
      <e-button :disabled="saving" @click="settingsOpen=false">取消</e-button>
      <e-button type="primary" :disabled="!settingsReady || loadingSettings || !validDraft" :loading="saving" @click="saveSettings">保存设置</e-button>
    </div>
  </n-modal>

  <e-drawer :open="resultOpen" title="模型测试结果" width="min(680px,100vw)" @close="closeResult">
    <section class="model-probe-result">
      <div class="probe-result-heading"><h2>{{ result.modelName }}</h2><e-tag :color="statusColor">{{ statusLabel }}</e-tag></div>
      <p class="probe-note">{{ result.source === 'form' ? '当前表单配置 · 未自动保存模型' : '已保存模型配置' }}</p>
      <div class="probe-status">
        <span v-if="running" class="probe-dot" aria-hidden="true"></span>
        <span role="status" aria-live="polite" aria-atomic="true">{{ running ? '正在等待模型响应' : statusLabel }}</span>
        <span>· {{ elapsedSeconds }} 秒</span>
      </div>
      <p v-if="running" class="probe-note">单次请求，不自动重试。非流式探测会等待完整响应后展示回答。</p>
      <n-alert v-if="result.error" :type="result.status === 'cancelled' ? 'warning' : 'error'" class="probe-error">{{ result.error }}</n-alert>
      <n-alert v-if="result.diagnostics?.projection" :type="result.diagnostics.projection.status === 'compatible' ? 'success' : 'warning'" class="probe-error">
        上游模型响应成功；{{ projectionLabels[result.diagnostics.projection.status] || '向量存储状态待确认' }}。
      </n-alert>
      <dl class="probe-facts">
        <div><dt>请求协议</dt><dd>{{ result.diagnostics?.provider || (running ? '读取中' : '未提供') }}</dd></div>
        <div><dt>超时上限</dt><dd>{{ result.diagnostics?.timeoutSeconds ? `${result.diagnostics.timeoutSeconds} 秒` : (running ? '读取中' : '未提供') }}</dd></div>
        <div v-if="parameters"><dt>回答 Token 上限</dt><dd>{{ parameters.outputLimit }}</dd></div>
        <div v-if="parameters"><dt>思考档位</dt><dd>{{ parameters.reasoning?.effort || '上游默认' }}</dd></div>
      </dl>
      <section v-if="result.prompt != null" class="probe-section"><h3>发送的测试文本</h3><pre>{{ result.prompt }}</pre></section>
      <p v-else-if="result.kind !== 'chat'" class="probe-note">该模型使用内置的 {{ result.kind === 'embedding' ? 'Embedding' : 'Rerank' }} 专用探测，不使用 Chat 自定义文本。</p>
      <section v-if="result.reply" class="probe-section"><h3>模型回复</h3><pre>{{ result.reply }}</pre></section>
      <p v-else-if="result.status === 'succeeded'">{{ result.message }}</p>
      <details v-if="result.reason || result.diagnostics || result.usage" class="probe-diagnostics">
        <summary>脱敏诊断详情</summary><pre>{{ diagnosticText }}</pre>
      </details>
      <p v-if="copyNotice" class="probe-note" role="status">{{ copyNotice }}</p>
      <div class="probe-actions">
        <e-button v-if="running" danger @click="cancelTest">取消测试</e-button>
        <e-button v-else :disabled="!lastRequest" @click="retry">重试本次配置</e-button>
        <e-button :disabled="running" @click="copyResult">复制结果</e-button>
        <e-button @click="closeResult">{{ running ? '取消并关闭' : '关闭' }}</e-button>
      </div>
    </section>
  </e-drawer>
</template>

<script setup>
import { ref, computed, onBeforeUnmount, watch } from 'vue'
import { NModal, NAlert, NSpin } from 'naive-ui'
import { message } from '@/app/ui/controls'
import { getModelTestPreferences, saveModelTestPreferences, testAIModelConnection } from '@/api/ai/aiModels'

const emit = defineEmits(['busy'])
const defaults = () => ({ prompt:'请用一句话简要介绍自己。', timeoutSeconds:180 })
const settingsOpen = ref(false), settingsDraft = ref(defaults()), settingsRevision = ref(0)
const loadingSettings = ref(false), saving = ref(false), settingsError = ref(''), settingsReady = ref(false)
let settingsGeneration = 0, settingsController
const validDraft = computed(() => typeof settingsDraft.value.prompt === 'string' && settingsDraft.value.prompt.trim().length > 0 && settingsDraft.value.prompt.length <= 16000 && Number.isInteger(settingsDraft.value.timeoutSeconds) && settingsDraft.value.timeoutSeconds >= 10 && settingsDraft.value.timeoutSeconds <= 300)
const validSettings = value => value?.schemaVersion === 1 && Number.isSafeInteger(value.revision) && value.revision >= 0 && typeof value.prompt === 'string' && value.prompt.trim() && value.prompt.length <= 16000 && Number.isInteger(value.timeoutSeconds) && value.timeoutSeconds >= 10 && value.timeoutSeconds <= 300
const errorText = error => error?.cause?.response?.data?.detail?.errMsg || error?.message || '请求未完成，请重试'

async function loadSettings() {
  const ticket = ++settingsGeneration
  settingsController?.abort()
  const controller = settingsController = new AbortController()
  loadingSettings.value = true; settingsReady.value = false; settingsError.value = ''
  try {
    const response = await getModelTestPreferences({ signal:controller.signal, timeout:15000 })
    if (ticket !== settingsGeneration) return
    if (response?.code !== 200 || !validSettings(response.data)) throw new Error('测试设置读取失败或格式不兼容')
    settingsDraft.value = { prompt:response.data.prompt, timeoutSeconds:response.data.timeoutSeconds }
    settingsRevision.value = response.data.revision
    settingsReady.value = true
  } catch (error) {
    if (ticket === settingsGeneration && !controller.signal.aborted) settingsError.value = errorText(error)
  } finally { if (ticket === settingsGeneration) loadingSettings.value = false }
}
function openSettings() { settingsOpen.value = true; loadSettings() }
function restoreDefaults() { settingsDraft.value = defaults() }
async function saveSettings() {
  if (saving.value || !validDraft.value || !settingsReady.value) return
  const ticket = settingsGeneration
  saving.value = true; settingsError.value = ''
  try {
    const response = await saveModelTestPreferences({ schemaVersion:1, expectedRevision:settingsRevision.value, ...settingsDraft.value })
    if (ticket !== settingsGeneration) return
    if (response?.code !== 200 || !validSettings(response.data)) throw new Error('测试设置保存未生效')
    settingsRevision.value = response.data.revision
    settingsOpen.value = false
    message.success('Chat 测试设置已保存，可跨设备使用')
  } catch (error) { if (ticket === settingsGeneration) settingsError.value = errorText(error) }
  finally { saving.value = false }
}
watch(settingsOpen, open => { if (!open) { settingsGeneration++; settingsController?.abort() } })

const resultOpen = ref(false), running = ref(false), result = ref({}), elapsedMs = ref(0), copyNotice = ref('')
let generation = 0, requestController, ticker, startedAt = 0, lastRequest = null
const elapsedSeconds = computed(() => (elapsedMs.value / 1000).toFixed(1))
const statusLabel = computed(() => ({ running:'测试中', succeeded:'测试成功', failed:'测试失败', cancelled:'已取消' })[result.value.status] || '准备测试')
const statusColor = computed(() => ({ running:'blue', succeeded:'green', failed:'red', cancelled:'orange' })[result.value.status] || 'default')
const projectionLabels = { compatible:'当前向量表维度兼容', incompatible:'当前向量表维度不兼容，需管理员迁移投影后再入库', unavailable:'向量存储检查失败，入库可用性尚未确认', not_initialized:'向量表尚未初始化，首次入库时建立', unknown:'模型维度未配置，入库时仍需核验' }
const parameters = computed(() => result.value.diagnostics?.parameters)
const diagnosticText = computed(() => JSON.stringify({ reason:result.value.reason || null, diagnostics:result.value.diagnostics || null, usage:result.value.usage || null }, null, 2))
function stopTicker() { clearInterval(ticker); ticker = null }
function busy(value, options) { emit('busy', { running:value, modelId:options?.id || null, source:options?.source || null }) }

async function start(payload, options) {
  if (running.value) return
  const snapshot = JSON.parse(JSON.stringify(payload))
  lastRequest = { payload:snapshot, options:{ ...options } }
  const ticket = ++generation
  const controller = requestController = new AbortController()
  result.value = { status:'running', modelName:options.name, source:options.source, kind:options.kind }
  resultOpen.value = true; running.value = true; copyNotice.value = ''; elapsedMs.value = 0
  startedAt = performance.now()
  ticker = setInterval(() => { elapsedMs.value = performance.now() - startedAt }, 100)
  busy(true, options)
  try {
    let preferences = options.kind === 'chat' ? defaults() : { timeoutSeconds:15 }
    if (options.kind === 'chat') {
      const response = await getModelTestPreferences({ signal:controller.signal, timeout:15000 })
      if (response?.code !== 200 || !validSettings(response.data)) throw new Error('未能读取账号测试设置，未发送模型请求')
      preferences = response.data
    }
    if (ticket !== generation || controller.signal.aborted) return
    result.value.prompt = options.kind === 'chat' ? preferences.prompt : null
    result.value.diagnostics = { timeoutSeconds:preferences.timeoutSeconds }
    const response = await testAIModelConnection({ ...snapshot, configSource:options.source,
      ...(options.kind === 'chat' ? { expectedPreferencesRevision:preferences.revision } : {}),
    }, {
      signal:controller.signal, timeout:315000,
    })
    if (ticket !== generation) return
    if (response?.code !== 200 || response.isSuccess === false || response.data?.ok !== true) throw new Error('测试未返回有效的成功结果')
    result.value = { ...result.value, ...response.data, status:'succeeded',
      prompt:response.data.diagnostics?.prompt ?? result.value.prompt }
  } catch (error) {
    if (ticket !== generation || controller.signal.aborted) return
    const detail = error?.cause?.response?.data?.detail
    result.value = { ...result.value, status:'failed', error:errorText(error),
      reason:detail?.reason || 'test_request_failed', diagnostics:detail?.diagnostics || result.value.diagnostics,
      prompt:detail?.diagnostics?.prompt ?? result.value.prompt }
  } finally {
    if (ticket === generation) {
      elapsedMs.value = performance.now() - startedAt
      stopTicker(); running.value = false; busy(false, options)
    }
  }
}
function cancelTest() {
  if (!running.value) return
  generation++; requestController?.abort(); stopTicker()
  elapsedMs.value = performance.now() - startedAt
  running.value = false
  result.value = { ...result.value, status:'cancelled', reason:'test_cancelled', error:'已取消等待并关闭请求。上游可能已经消耗额度，取消不撤回已发生的调用。' }
  busy(false)
}
function closeResult() {
  cancelTest(); resultOpen.value = false; lastRequest = null; result.value = {}; copyNotice.value = ''
}
function retry() { if (lastRequest && !running.value) start(lastRequest.payload, lastRequest.options) }
async function copyResult() {
  const text = JSON.stringify({ modelName:result.value.modelName, source:result.value.source, status:result.value.status,
    elapsedMs:Math.round(elapsedMs.value), prompt:result.value.prompt, reply:result.value.reply,
    error:result.value.error, reason:result.value.reason, diagnostics:result.value.diagnostics, usage:result.value.usage }, null, 2)
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
    } else {
      const previousFocus = document.activeElement
      const area = document.createElement('textarea')
      area.value = text; area.setAttribute('readonly', '')
      area.style.position = 'fixed'; area.style.opacity = '0'; area.style.pointerEvents = 'none'
      document.body.appendChild(area)
      try { area.select(); if (!document.execCommand('copy')) throw new Error('copy failed') }
      finally { area.remove(); previousFocus?.focus?.() }
    }
    copyNotice.value = '结果已复制'
  } catch { copyNotice.value = '当前浏览器不允许自动复制，请选中面板文字手动复制。' }
}
onBeforeUnmount(() => { settingsGeneration++; settingsController?.abort(); cancelTest(); lastRequest = null; stopTicker() })
defineExpose({ start, openSettings })
</script>

<style scoped>
.model-probe-result { color:var(--eic-text); min-width:0; }
.probe-result-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
.probe-result-heading h2 { margin:0; font-size:var(--eic-font-section); overflow-wrap:anywhere; }
.probe-note { color:var(--eic-text-secondary); font-size:var(--eic-font-meta); line-height:1.75; }
.probe-status { display:flex; align-items:center; gap:10px; margin:20px 0 8px; font-weight:600; }
.probe-dot { width:9px; height:9px; border-radius:50%; background:var(--eic-primary); animation:probe-pulse 1.6s ease-in-out infinite; }
.probe-error { margin:16px 0; overflow-wrap:anywhere; }
.probe-facts { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; padding:16px; margin:20px 0; background:var(--eic-surface-muted); border-radius:var(--eic-radius); }
.probe-facts dt { color:var(--eic-text-secondary); font-size:var(--eic-font-meta); }
.probe-facts dd { margin:4px 0 0; overflow-wrap:anywhere; }
.probe-section { margin:24px 0; }
.probe-section h3 { margin:0 0 10px; font-size:var(--eic-font-ui); }
.probe-section pre,.probe-diagnostics pre { white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; line-height:1.75; margin:0; padding:16px; border:1px solid var(--eic-line); border-radius:var(--eic-radius); background:var(--eic-surface-muted); }
.probe-diagnostics summary { cursor:pointer; margin-bottom:12px; }
.probe-diagnostics pre { font-size:var(--eic-font-code); }
.probe-actions { display:flex; justify-content:flex-end; flex-wrap:wrap; gap:12px; margin-top:24px; }
@keyframes probe-pulse { 50% { opacity:.35; } }
@media(max-width:480px) { .probe-facts { grid-template-columns:1fr; } }
@media(prefers-reduced-motion:reduce) { .probe-dot { animation:none; } }
</style>
