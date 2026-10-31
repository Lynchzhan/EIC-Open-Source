<template>
  <e-modal :open="open" :title="`检索 · ${knowledgeBase?.name || '知识库'}`" :width="820" :footer="null" @cancel="$emit('close')">
    <e-form layout="vertical" @finish="search">
      <e-form-item label="检索问题"><e-textarea v-model:value="query" :rows="3" :disabled="busy" placeholder="输入问题，查看当前知识库的命中片段" /></e-form-item>
      <div class="knowledge-search-options">
        <e-form-item label="策略"><e-select v-model:value="strategy" :options="strategies" :disabled="busy" /></e-form-item>
        <e-form-item label="返回条数"><e-input-number v-model:value="limit" :min="1" :max="50" :disabled="busy" /></e-form-item>
        <e-button type="primary" html-type="submit" :loading="busy" :disabled="!canRead || !query.trim() || !validLimit">检索</e-button>
      </div>
    </e-form>
    <e-alert v-if="error" type="error" show-icon :message="error" />
    <e-spin :spinning="busy">
      <p v-if="result" class="eic-field-description">返回 {{ result.results.length }} 条 · 向量 {{ result.denseStatus || '未提供' }} · 重排 {{ result.rerankStatus || '未提供' }}</p>
      <ol class="knowledge-search-results" v-if="result?.results?.length">
        <li v-for="(item, index) in result.results" :key="`${item.chunkId || index}-${index}`">
          <strong>{{ item.documentName || item.fileName || item.documentId || `片段 ${index + 1}` }}</strong>
          <small>切片 {{ item.chunkId || '未提供' }} · 分数 {{ item.rerankScore ?? item.score ?? '未提供' }}</small>
          <p>{{ item.content }}</p>
        </li>
      </ol>
      <e-empty v-else-if="result" description="未命中片段，请调整问题或检查文档入库状态" />
    </e-spin>
  </e-modal>
</template>
<script setup>
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { recallKnowledgeTest } from '@/api/ai/aikmss'
const props = defineProps({ open: Boolean, knowledgeBase: Object, canRead: Boolean })
defineEmits(['close'])
const query = ref(''), strategy = ref('full'), limit = ref(8), busy = ref(false), error = ref(''), result = ref(null)
const validLimit = computed(() => Number.isInteger(limit.value) && limit.value >= 1 && limit.value <= 50)
const strategies = [{label:'基础',value:'basic'},{label:'进阶',value:'advanced'},{label:'完整',value:'full'}]
let sequence = 0
watch(() => [props.open, props.knowledgeBase?.id], () => {
  sequence++; busy.value = false; error.value = ''; result.value = null; query.value = ''
  strategy.value = props.knowledgeBase?.retrievalStrategy || 'full'
}, { flush: 'sync' })
async function search() {
  if (!props.open || !props.canRead || !props.knowledgeBase?.id || busy.value || !query.value.trim() || !validLimit.value) return
  const ticket = ++sequence
  busy.value = true; error.value = ''; result.value = null
  try {
    const response = await recallKnowledgeTest({ knowledgeBaseId: props.knowledgeBase.id, query: query.value.trim(), k: limit.value, strategy: strategy.value })
    if (ticket !== sequence) return
    if (response?.code !== 200 || response.isSuccess === false || !Array.isArray(response.data?.results)) throw new Error(response?.msg || '检索响应无效')
    result.value = response.data
  } catch (e) { if (ticket === sequence) error.value = e.message || '检索失败，请重试' }
  finally { if (ticket === sequence) busy.value = false }
}
onBeforeUnmount(() => { sequence++ })
</script>
<style scoped>
.knowledge-search-options { display:grid; grid-template-columns: minmax(120px,1fr) minmax(100px,1fr) auto; gap:16px; align-items:center; }
.knowledge-search-results { list-style:none; padding:0; }
.knowledge-search-results li { border:1px solid var(--eic-line); background:var(--eic-surface-muted); border-radius:12px; padding:16px; margin-top:12px; }
.knowledge-search-results small { display:block; color:var(--eic-muted); margin-top:6px; }
.knowledge-search-results p { white-space:pre-wrap; overflow-wrap:anywhere; max-height:220px; overflow:auto; }
@media(max-width:600px) { .knowledge-search-options { grid-template-columns:1fr; gap:4px; } }
</style>
