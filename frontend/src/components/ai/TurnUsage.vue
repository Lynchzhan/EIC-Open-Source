<template>
  <dl class="turn-usage" aria-label="本轮用量" data-testid="turn-usage">
    <div v-for="item in fields" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ tokenValue(turn.usage?.[item.key]) }}</dd></div>
    <div><dt>处理</dt><dd>{{ seconds(turn.processingMs) }}</dd></div>
    <div><dt>端到端</dt><dd>{{ seconds(turn.durationMs) }}</dd></div>
    <div v-if="turn.approvalWaitMs > 0"><dt>审批等待</dt><dd>{{ seconds(turn.approvalWaitMs) }}</dd></div>
    <div v-if="!turn.timingComplete"><dt>耗时覆盖</dt><dd>统计不完整</dd></div>
    <div v-if="turn.traceIds?.length"><e-button type="link" size="small" @click="$emit('trace', turn.traceIds[0])">调用链路</e-button></div>
  </dl>
</template>
<script setup>
import { tokenValue, seconds } from '@/shared/ui/conversationObservation';
defineProps({ turn:{ type:Object, required:true } });
defineEmits(['trace']);
const fields = [{ key:'inputTokens', label:'输入 Token' }, { key:'outputTokens', label:'输出 Token' },
  { key:'totalTokens', label:'总 Token' }, { key:'cachedTokens', label:'缓存 Token' }];
</script>
<style scoped>
.turn-usage { display:flex; flex-wrap:wrap; gap:8px 18px; margin:12px 0 0; padding-top:10px; border-top:1px solid var(--eic-line); color:var(--eic-text-secondary); font-size:var(--eic-font-meta); }
.turn-usage > div { display:flex; gap:5px; align-items:center; min-width:0; }
dt, dd { margin:0; overflow-wrap:anywhere; } dd { color:var(--eic-text); font-variant-numeric:tabular-nums; }
</style>
