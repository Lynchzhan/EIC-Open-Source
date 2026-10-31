<template>
  <section class="appearance-settings" aria-labelledby="appearance-heading">
    <h2 id="appearance-heading">界面与阅读</h2>
    <p>调整时即时预览，保存后同步到当前账号；离开此页取消未保存调整。</p>
    <p v-if="appearanceError" role="alert">未同步：{{ appearanceError }}</p>
    <button type="button" :disabled="busy || appearanceSaving" @click="reload">重新载入服务器设置</button>
    <fieldset :disabled="busy || !appearanceReady || appearanceSaving">
      <div class="appearance-grid">
        <label>界面主题<select v-model="draft.theme" @change="preview"><option value="light">浅色</option><option value="dark">深色</option></select></label>
        <label>阅读预设<select :value="preset" @change="choosePreset($event.target.value)"><option value="standard">标准</option><option value="large">大</option><option value="extra">特大</option><option value="custom" disabled>自定义</option></select></label>
      </div>
      <details open><summary>高级字号设置</summary><div class="appearance-grid">
        <label v-for="(range, key) in ranges" :key="key">{{ labels[key] }}（{{ range[0] }}–{{ range[1] }}px）
          <input v-model.number="draft.fontSizes[key]" type="number" :min="range[0]" :max="range[1]" step="1" required @input="preview">
        </label>
      </div></details>
      <p v-if="validationError" role="alert">{{ validationError }}</p>
      <div class="appearance-sample"><h3>阅读效果预览</h3><p>文档已提交后台处理，你可以继续对话，稍后查看状态。</p><small>辅助信息与运行状态</small><pre><code>const status = 'processing';</code></pre></div>
      <div class="appearance-actions"><button type="button" @click="defaults">恢复默认</button><button type="button" @click="cancel">取消调整</button><button type="button" class="appearance-save" @click="save">{{ appearanceSaving ? '正在保存…' : '保存外观设置' }}</button></div>
    </fieldset>
    <p role="status">{{ status }}</p>
  </section>
</template>
<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { presets, ranges, confirmedAppearance, appearanceError, appearanceReady, appearanceSaving,
  previewAppearance, loadAccountAppearance, beginAppearanceEdit, cancelAppearanceEdit, saveAccountAppearance } from '@/app/appearancePreferences'
const draft = reactive({ theme:'light', fontSizes:{ ...presets.standard } })
const busy = ref(true), status = ref(''), validationError = ref('')
const labels = { nav:'导航文字', ui:'表格与表单', chat:'聊天正文', meta:'辅助文字', code:'代码文字' }
const preset = computed(() => Object.keys(presets).find(key => Object.keys(ranges).every(field => presets[key][field] === draft.fontSizes[field])) || 'custom')
let alive = true
function restoreDraft() {
  const value = confirmedAppearance.value
  if (value) { draft.theme = value.theme; draft.fontSizes = { ...value.fontSizes } }
  validationError.value = ''
}
function preview() {
  try { previewAppearance(draft); validationError.value = ''; status.value = '正在预览，尚未保存' }
  catch (error) { validationError.value = error.message }
}
function choosePreset(key) { draft.fontSizes = { ...presets[key] }; preview() }
function defaults() { draft.theme = 'light'; choosePreset('standard') }
function cancel() { cancelAppearanceEdit(); restoreDraft(); beginAppearanceEdit(); status.value = '已恢复最近确认的设置' }
async function reload() {
  busy.value = true; cancelAppearanceEdit()
  await loadAccountAppearance(true)
  if (!alive) return
  restoreDraft(); beginAppearanceEdit(); busy.value = false
  status.value = appearanceReady.value ? '已载入服务器设置' : '服务器设置未载入，暂不能保存'
}
async function save() {
  try { if (await saveAccountAppearance(draft)) { if (alive) status.value = '已保存，将在其他设备登录后或刷新时同步' } }
  catch (error) { if (alive) status.value = error.message }
}
onMounted(reload)
onUnmounted(() => { alive = false; cancelAppearanceEdit() })
</script>
<style scoped>
.appearance-settings{margin:24px 0;padding:24px;background:var(--eic-surface);border:1px solid var(--eic-line);border-radius:14px}
.appearance-settings h2{font-size:var(--eic-font-section)}.appearance-settings p,.appearance-settings small{color:var(--eic-text-secondary)}.appearance-settings small{font-size:var(--eic-font-meta)}
fieldset{border:0;padding:20px 0 0;min-width:0}.appearance-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin:16px 0}
label{display:grid;gap:8px;min-width:0}input,select{width:100%;min-height:44px;padding:8px 12px;border:1px solid var(--eic-line-strong);border-radius:8px;background:var(--eic-surface-muted);color:var(--eic-text)}
button{min-height:40px;padding:8px 14px;border:1px solid var(--eic-line-strong);border-radius:8px;background:var(--eic-surface);color:var(--eic-text);cursor:pointer}
.appearance-save{background:var(--eic-action-fill);color:var(--eic-action-text)}.appearance-actions{display:flex;flex-wrap:wrap;gap:12px;margin-top:20px}
.appearance-sample{padding:18px;background:var(--eic-surface-muted);border-radius:10px}.appearance-sample p{font-size:var(--eic-font-chat)}
pre{overflow:auto;font-size:var(--eic-font-code)}@media(max-width:768px){.appearance-grid{grid-template-columns:1fr}.appearance-settings{padding:16px}}
</style>
