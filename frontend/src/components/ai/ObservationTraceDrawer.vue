<template>
  <e-drawer :open="open" title="Trace 摘要" :width="width" :push="false" :destroy-on-close="true" @close="$emit('close')">
    <div class="trace-reader" :aria-busy="loading">
      <p class="trace-id">{{ traceId }}</p>
      <p class="trace-note">只展示当前筛选区间内可见事件，不代表完整历史。时间以本地时区显示。</p>
      <e-space wrap>
        <e-button :loading="loading" @click="load">刷新 Trace</e-button>
        <e-checkbox v-if="admin" v-model:checked="detail" :disabled="loading">管理员脱敏详情</e-checkbox>
      </e-space>
      <e-alert v-if="error" type="error" show-icon :message="error" class="trace-alert" />
      <e-skeleton v-if="loading" active class="trace-alert" />
      <template v-else-if="data && !error">
        <p v-if="admin && verifiedAdmin" class="trace-note">
          <a v-if="data.langfuse?.url" :href="data.langfuse.url" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">打开 Langfuse 管理员详情</a>
          <span v-else>Langfuse 详情入口未提供。</span>
          共享项目可能包含其他租户数据；需要 Langfuse 自身授权。链接不代表已导出，记录可能尚未到达或已过期。
        </p>
        <dl class="trace-facts">
          <div><dt>可见事件</dt><dd>{{ metric(data.eventCount) }}</dd></div>
          <div><dt>物理模型调用</dt><dd>{{ metric(data.modelCallCount) }}</dd></div>
          <div><dt>显式重试</dt><dd>{{ metric(data.retryCount) }}</dd></div>
          <div><dt>重试信息覆盖</dt><dd>{{ coverage(data.retryCoverage) }}</dd></div>
        </dl>
        <e-alert :type="data.firstFailure ? 'warning' : 'info'" show-icon
          :message="data.firstFailure ? `首个可见失败：${data.firstFailure.stage || '阶段未提供'} / ${data.firstFailure.errorCode || '错误码未提供'}` : '当前可见事件中未记录失败，不等于完整 Trace 成功'" />
        <section class="span-reader" aria-label="调用树与时间轴">
          <h2>调用链路</h2>
          <e-alert v-if="data.spansComplete === false" type="warning" message="节点超过 500，当前链路展示不完整" />
          <e-empty v-if="!spans.length" description="调用节点未采集" />
          <article v-for="node in spans" :key="node.key" class="span-node">
            <div :style="{ paddingLeft:`${node.depth * 12}px` }"><strong>{{ node.stage || eventLabel(node.category) }}</strong>
              <e-tag :color="eventColor(node.status)">{{ statusLabel(node.status) }}</e-tag>
              <small v-if="node.relation !== 'recorded'">{{ node.relation === 'cycle' ? '父节点关系异常' : '父节点未采集或不可见' }}</small></div>
            <div class="span-track" aria-hidden="true"><i :style="{ left:`${node.left}%`, width:`${node.width}%` }" /></div>
            <p>+{{ seconds(node.offsetMs) }} · {{ seconds(node.durationMs) }} · Token {{ tokenValue({ value:node.usage?.totalTokens, complete:true }) }}</p>
          </article>
        </section>
        <ol class="trace-timeline">
          <li v-for="row in data.data" :key="row.eventKey">
            <div class="trace-event-heading"><strong>{{ eventLabel(row.category) }}</strong>
              <e-tag :color="eventColor(row.status)">{{ statusLabel(row.status) }}</e-tag></div>
            <time>{{ eventTime(row.occurredAt) }}</time>
            <p>{{ row.stage || '阶段未提供' }} · {{ metric(row.durationMs, ' ms') }}</p>
            <p v-if="row.errorCode" class="trace-error">{{ row.errorCode }}</p>
            <dl><div><dt>来源</dt><dd>{{ row.source || '未提供' }}</dd></div>
              <div><dt>调用 / 父节点</dt><dd>{{ row.spanId || '未提供' }} / {{ row.parentSpanId || '未提供' }}</dd></div>
              <div><dt>模型 / 工具</dt><dd>{{ row.modelId || '未提供' }} / {{ row.toolName || '未提供' }}</dd></div>
              <div><dt>Usage 来源</dt><dd>{{ row.usage?.source || '未提供' }}</dd></div>
            </dl>
            <pre v-if="admin && detail && row.summary">{{ JSON.stringify(row.summary, null, 2) }}</pre>
          </li>
        </ol>
        <div class="trace-pages"><e-button :disabled="page <= 1" @click="changePage(-1)">上一页</e-button>
          <span>第 {{ page }} 页</span><e-button :disabled="!data.hasMore || page >= 501" @click="changePage(1)">下一页</e-button></div>
        <p v-if="data.hasMore && page >= 501" class="trace-note">已达分页上限，请缩小查询区间。</p>
      </template>
    </div>
  </e-drawer>
</template>

<script setup>
import { ref, computed, watch, onBeforeUnmount } from 'vue';
import { traceLayout, seconds, tokenValue } from '@/shared/ui/conversationObservation';
import { getUserPermissions } from '@/api/baseapi';
import { getTokenUser } from '@/api/userapi';
import { resolveWorkspaceIdentity } from '@/shared/ui/workbenchContract';
import { getObservationTrace } from '@/api/ai/observability';
import { checkedObservation, observationPermissions, metric, coverage, eventTime, eventLabel,
  statusLabel, eventColor } from '@/shared/ui/observabilityWorkspace';
const props = defineProps({ open:Boolean, traceId:String, query:Object, admin:Boolean });
defineEmits(['close']);
const width = 'min(920px, 100vw)';
const data = ref(null), error = ref(''), loading = ref(false), detail = ref(false), page = ref(1);
const spans = computed(() => traceLayout(data.value?.spans || []));
const verifiedAdmin = ref(false);
let revision = 0;
async function load() {
  const ticket = ++revision;
  data.value = null; error.value = ''; loading.value = true; verifiedAdmin.value = false;
  try {
    const identity = await resolveWorkspaceIdentity(getUserPermissions, getTokenUser);
    if (ticket !== revision || !props.open) return;
    const permissions = observationPermissions(identity);
    verifiedAdmin.value = permissions.admin;
    if (!permissions.read) throw new Error('没有读取观测数据的权限');
    if (props.query?.scope === 'tenant' && !permissions.admin) throw new Error('租户聚合权限已变更，请刷新仪表盘');
    if (detail.value && !permissions.admin) throw new Error('脱敏详情权限已变更，请刷新仪表盘');
    const params = { ...props.query, traceId:props.traceId, page:page.value, pageSize:20, detail:detail.value };
    const response = await getObservationTrace(params);
    if (ticket === revision && props.open) data.value = checkedObservation(response, 'trace', params);
  } catch (failure) { if (ticket === revision) error.value = failure.message || '读取 Trace 失败，请重试'; }
  finally { if (ticket === revision) loading.value = false; }
}
function changePage(delta) { page.value += delta; load(); }
watch(() => [props.open, props.traceId, props.query], () => {
  revision += 1; data.value = null; error.value = ''; page.value = 1; detail.value = false;
  if (props.open && props.traceId) load(); else loading.value = false;
});
watch(detail, () => { if (props.open) { page.value = 1; load(); } });
onBeforeUnmount(() => { revision += 1; });
</script>

<style scoped>
.trace-reader { color:var(--eic-text); min-width:0; }
.span-reader { margin-top:24px; } .span-reader h2 { font-size:var(--eic-font-section); margin:0 0 12px; }
.span-node { display:grid; grid-template-columns:minmax(130px,1fr) 1fr; gap:8px 16px; padding:12px 0; border-bottom:1px solid var(--eic-line); min-width:0; }
.span-node strong { font-size:var(--eic-font-ui); overflow-wrap:anywhere; }
.span-node small { display:block; color:var(--eic-text-secondary); font-size:var(--eic-font-meta); }
.span-track { position:relative; align-self:center; height:8px; background:var(--eic-background, #eef2f6); overflow:hidden; border-radius:3px; }
.span-track i { position:absolute; height:100%; background:var(--eic-primary, #1677ff); min-width:2px; }
.span-node p { grid-column:1/-1; margin:0; font-size:var(--eic-font-meta); color:var(--eic-text-secondary); }
.trace-id { font-weight:600; overflow-wrap:anywhere; }
.trace-note, time, dt { color:var(--eic-text-secondary); font-size:var(--eic-font-meta); }
.trace-alert { margin-top:16px; }
.trace-facts { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin:24px 0; }
dd { margin:4px 0 0; overflow-wrap:anywhere; }
.trace-timeline { list-style:none; padding:0; margin:24px 0; }
.trace-timeline li { border-top:1px solid var(--eic-line); padding:18px 0; }
.trace-event-heading { display:flex; justify-content:space-between; gap:12px; }
.trace-timeline p { margin:6px 0; } .trace-timeline dl { font-size:var(--eic-font-meta); display:grid; gap:8px; }
.trace-error { color:var(--eic-danger, #b42318); overflow-wrap:anywhere; }
pre { background:var(--eic-background, #F5F8FC); padding:12px; white-space:pre-wrap; overflow-wrap:anywhere; }
.trace-pages { display:flex; justify-content:space-between; align-items:center; gap:12px; }
@media(max-width:600px) { }
</style>
