<template>
  <fieldset class="reasoning-fields">
    <legend>模型思考能力</legend>
    <label class="reasoning-toggle"><input type="checkbox" :checked="policy.enabled" @change="setEnabled($event.target.checked)">允许会话选择思考档位</label>
    <p class="form-hint">关闭此设置时不发送额外思考参数，由上游默认处理。请只声明上游实际支持的档位；保存不会验证上游能力。</p>
    <template v-if="policy.enabled">
      <label>允许的档位</label>
      <n-select multiple :value="policy.levels" :options="options" @update:value="setLevels" />
      <label>默认档位</label>
      <n-select :value="policy.default" :options="options.filter(item => policy.levels.includes(item.value))" @update:value="update({ default: $event })" />
      <template v-if="adapter === 'anthropic'">
        <p class="form-hint">Messages 使用显式思考 Token 预算，各档至少 1024 且小于回答 Token 上限（{{ outputLimit }}）。</p>
        <label v-for="level in policy.levels.filter(value => value !== 'off')" :key="level">
          {{ labels[level] }}思考 Token
          <n-input-number :value="policy.budgets[level] ?? null" :min="1024" :max="Math.max(1024, outputLimit - 1)" :precision="0" @update:value="update({ budgets: { ...policy.budgets, [level]: $event } })" />
        </label>
      </template>
    </template>
  </fieldset>
</template>
<script setup>
import { computed, watch } from 'vue'
import { NSelect, NInputNumber } from 'naive-ui'
const props = defineProps({ modelValue: { type: Object, default: null }, provider: [String, Number], outputLimit: { type: Number, default: 8192 } })
const emit = defineEmits(['update:modelValue'])
const labels = { none: '关闭', off: '关闭', on: '开启', minimal: '最低', low: '低', medium: '中', high: '高', xhigh: '最高' }
const adapter = computed(() => props.provider === 'anthropic' ? 'anthropic' : String(props.provider) === '10' || props.provider === 'ollama' ? 'ollama' : 'openai')
const policy = computed(() => ({ enabled: false, levels: [], default: null, budgets: {}, ...props.modelValue }))
const options = computed(() => (adapter.value === 'ollama' ? ['off', 'on'] : adapter.value === 'anthropic' ? ['off', 'low', 'medium', 'high'] : ['none', 'minimal', 'low', 'medium', 'high', 'xhigh']).map(value => ({ value, label: labels[value] })))
function update(patch) { emit('update:modelValue', { ...policy.value, adapter: adapter.value, ...patch }) }
function setEnabled(enabled) { update({ enabled }) }
function setLevels(levels) { update({ levels, default: levels.includes(policy.value.default) ? policy.value.default : levels[0] ?? null }) }
watch(adapter, () => { if (policy.value.enabled && policy.value.adapter !== adapter.value) emit('update:modelValue', { enabled: false, adapter: adapter.value, levels: [], default: null, budgets: {} }) }, { flush: 'sync' })
</script>
<style scoped>
.reasoning-fields { display:grid; gap:10px; min-width:0; margin:18px 0; padding:16px; border:1px solid var(--eic-line); border-radius:10px; }
.reasoning-fields legend { padding:0 6px; font-weight:600; }
.reasoning-toggle { display:flex; gap:8px; align-items:center; }
.reasoning-fields label { display:grid; gap:6px; }
.reasoning-fields .reasoning-toggle { display:flex; }
</style>
