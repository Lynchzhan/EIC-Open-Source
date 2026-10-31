<template>
  <div class="chat-composer-tools">
    <label class="composer-tool-option">
      <n-switch :value="onlineSearch" :disabled="searchDisabled" aria-label="联网搜索" title="按管理员配置的模型联网模式搜索" @update:value="$emit('update:onlineSearch', $event)" />
      <span>联网搜索</span>
    </label>
    <template v-if="restricted">
      <n-button @click="$emit('restricted')">语音 · 受限</n-button>
      <n-button @click="$emit('restricted')">电话 · 受限</n-button>
    </template>
    <template v-else>
      <label class="composer-tool-option">
        <n-switch :value="voiceMode" aria-label="语音模式" @update:value="$emit('update:voiceMode', $event)" />
        <span>语音模式</span>
      </label>
      <n-button size="small" @click="$emit('phone')"><template #icon><CallOutline /></template>电话</n-button>
      <template v-if="voiceMode">
        <div class="composer-speed-options" role="group" aria-label="语音倍速">
          <button v-for="speed in speedOptions" :key="speed.value" type="button"
            :aria-pressed="voiceSpeed === speed.value" @click="$emit('speed', speed.value)">{{ speed.label }}</button>
        </div>
        <label class="composer-tool-option">
          <n-switch :value="showText" aria-label="转文字" @update:value="$emit('update:showText', $event)" />
          <span>转文字</span>
        </label>
      </template>
    </template>
  </div>
</template>

<script setup>
import { NButton, NSwitch } from 'naive-ui'
import { CallOutline } from '@vicons/ionicons5'
defineProps({
  restricted: Boolean, onlineSearch: Boolean, voiceMode: Boolean, showText: Boolean,
  searchDisabled: Boolean,
  voiceSpeed: Number, speedOptions: { type: Array, default: () => [] },
})
defineEmits(['restricted', 'update:onlineSearch', 'update:voiceMode', 'update:showText', 'phone', 'speed'])
</script>

<style scoped>
.chat-composer-tools { display:flex; flex-wrap:wrap; align-items:center; gap:8px 10px; max-width:100%; }
.composer-tool-option { display:inline-flex; align-items:center; gap:8px; min-height:36px; color:var(--eic-text-secondary); font-size:var(--eic-font-meta); white-space:nowrap; }
.composer-speed-options { display:flex; flex-wrap:wrap; gap:4px; }
.composer-speed-options button { min-height:32px; padding:4px 8px; border:1px solid var(--eic-line); border-radius:6px; background:var(--eic-surface); color:var(--eic-text); cursor:pointer; }
.composer-speed-options button[aria-pressed="true"] { background:var(--eic-primary-soft); color:var(--eic-primary-dark); }
</style>
