<template>
  <e-drawer :open="open" :title="`会话 #${chatId}`" width="min(860px, 100vw)" :push="false" :destroy-on-close="true" @close="$emit('close')">
    <e-alert v-if="error" type="error" show-icon :message="error" />
    <e-skeleton v-else-if="loading" active />
    <template v-else-if="data">
      <dl class="session-facts"><div><dt>轮次</dt><dd>{{ data.summary.roundCount }}</dd></div>
        <div><dt>累计 Token</dt><dd>{{ tokenValue(data.summary.usage.totalTokens) }}</dd></div>
        <div><dt>累计处理</dt><dd>{{ seconds(data.summary.processingMs) }}</dd></div>
        <div><dt>耗时覆盖</dt><dd>{{ data.summary.timingComplete ? '完整' : '统计不完整' }}</dd></div></dl>
      <e-empty v-if="!data.data.length" description="暂无接受的提问" />
      <article v-for="(turn, index) in data.data" :key="turn.messageId" class="session-turn">
        <header><strong>第 {{ (page - 1) * 20 + index + 1 }} 轮</strong>
          <e-tag :color="eventColor(turn.status)">{{ statusLabel(turn.status) }}</e-tag>
          <time>{{ eventTime(turn.startedAt) }}</time></header>
        <TurnUsage :turn="turn" @trace="showTrace($event, turn)" />
        <dl class="session-breakdown"><div><dt>模型调用</dt><dd>{{ turn.modelCallCount }}</dd></div>
          <div><dt>工具调用</dt><dd>{{ turn.toolCallCount }}</dd></div>
          <div><dt>显式重试</dt><dd>{{ turn.retryProvidedCalls ? turn.retryCount : '未采集' }}</dd></div>
          <div v-for="kind in ['model','embedding','rerank']" :key="kind"><dt>{{ kind }} Token</dt>
            <dd>{{ tokenValue(turn.usageByKind[kind].totalTokens) }}</dd></div></dl>
        <e-button v-for="trace in turn.traceIds.slice(1)" :key="trace" type="link" @click="showTrace(trace, turn)">续行 Trace {{ trace.slice(0,8) }}</e-button>
      </article>
      <footer class="session-pages"><e-button :disabled="page <= 1" @click="changePage(-1)">上一页</e-button>
        <span>第 {{ page }} 页</span><e-button :disabled="!data.hasMore" @click="changePage(1)">下一页</e-button></footer>
    </template>
    <ObservationTraceDrawer :open="!!traceId" :trace-id="traceId" :query="traceQuery" :admin="admin" @close="traceId = ''" />
  </e-drawer>
</template>
<script setup>
import { ref, watch, onBeforeUnmount } from 'vue';
import { getObservationSession } from '@/api/ai/observability';
import { checkedSession, seconds, tokenValue } from '@/shared/ui/conversationObservation';
import { eventTime, eventColor, statusLabel } from '@/shared/ui/observabilityWorkspace';
import TurnUsage from './TurnUsage.vue';
import ObservationTraceDrawer from './ObservationTraceDrawer.vue';
const props = defineProps({ open:Boolean, chatId:String, query:Object, admin:Boolean });
defineEmits(['close']);
const data = ref(null), error = ref(''), loading = ref(false), page = ref(1), traceId = ref(''), traceQuery = ref(null);
let revision = 0;
async function load() {
  const ticket = ++revision;
  data.value = null; error.value = ''; loading.value = true;
  const params = { scope:props.query?.scope || 'own', chatId:props.chatId, page:page.value, pageSize:20 };
  try {
    const response = await getObservationSession(params);
    if (ticket === revision && props.open) data.value = checkedSession(response, params);
  } catch (failure) { if (ticket === revision) error.value = failure.message || '会话统计读取失败'; }
  finally { if (ticket === revision) loading.value = false; }
}
function changePage(delta) { page.value += delta; load(); }
function showTrace(id, turn) {
  const start = Date.parse(turn.startedAt), end = Date.parse(turn.finishedAt || new Date().toISOString());
  traceQuery.value = { scope:props.query?.scope || 'own',
    start:new Date(start - 1000).toISOString(), end:new Date(Math.max(start + 1000, end + 1000)).toISOString() };
  traceId.value = id;
}
watch(() => [props.open, props.chatId, props.query], () => {
  revision += 1; page.value = 1; data.value = null; traceId.value = ''; error.value = '';
  if (props.open && props.chatId) load(); else loading.value = false;
});
onBeforeUnmount(() => { revision += 1; });
</script>
<style scoped>
.session-facts { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px; color:var(--eic-text); margin:0 0 24px; }
dt, time { font-size:var(--eic-font-meta); color:var(--eic-text-secondary); } dd { margin:6px 0 0; font-variant-numeric:tabular-nums; }
.session-turn { border-top:1px solid var(--eic-line); padding:20px 0; color:var(--eic-text); }
header { display:flex; gap:12px; align-items:center; flex-wrap:wrap; } header time { margin-left:auto; }
.session-breakdown { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-bottom:0; }
.session-pages { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-top:20px; }
@media(max-width:600px) { .session-breakdown { grid-template-columns:repeat(2,minmax(0,1fr)); } header time { width:100%; margin-left:0; } }
</style>
