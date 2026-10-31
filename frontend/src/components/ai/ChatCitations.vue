<template>
  <div class="source-tags" aria-label="检索来源">
    <a v-for="(source, index) in sources.filter(item => item.type === 'web' && safeUrl(item.url))" :key="source.url"
      class="source-tag" :href="safeUrl(source.url)" target="_blank" rel="noopener noreferrer">网页 {{ index + 1 }} · {{ source.title || source.documentName || source.url }}</a>
    <button v-for="(source, index) in sources.filter(item => item.type !== 'web')" :key="index" type="button" class="source-tag"
      @click="$emit('select', source)">
      <FileTextOutlined /> 知识库来源 {{ index + 1 }}
    </button>
  </div>
</template>
<script setup>
import { FileTextOutlined } from '@/app/ui/icons'
defineProps({ sources: { type: Array, default: () => [] } })
defineEmits(['select'])
const safeUrl = value => {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}
</script>
<style scoped>
.source-tags { display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }
.source-tag { padding:8px 12px; min-height:44px; border:1px solid var(--eic-line-strong); border-radius:10px; background:var(--eic-surface-muted); color:var(--eic-primary-dark); font:inherit; font-size:var(--eic-font-button); cursor:pointer; }
.source-tag:focus-visible { outline:3px solid var(--eic-primary); outline-offset:3px; }
@media(max-width:768px) { .source-tag { min-height:44px; } }
</style>
