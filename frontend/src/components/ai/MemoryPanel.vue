<template>
  <e-button size="small" @click="open = true">记忆与候选</e-button>
  <e-drawer v-model:open="open" title="我的长期记忆" width="min(720px, 100vw)" :closable="!busy" :mask-closable="!busy">
    <p>仅生效条目参与召回；后台候选需要你确认。遗忘会清除记忆正文及派生索引，原始聊天仍保留。</p>
    <e-space wrap>
      <e-select v-model:value="status" :options="statuses" :disabled="busy" style="width:160px" />
      <e-button :loading="loading" :disabled="busy" @click="load(false)">刷新</e-button>
      <e-button :disabled="busy" @click="createEntry">手工新增</e-button>
      <e-button :disabled="busy || !!experienceContext" @click="rebuildSearch"
        title="仅更新已有记忆的检索索引，不请求模型">重建检索索引</e-button>
      <e-button :loading="busy" @click="reindex">重建向量索引</e-button>
      <e-button :disabled="busy || context?.contextMode === 'trim'" @click="compact">生成会话摘要</e-button>
    </e-space>
    <p v-if="context">上下文：{{ context.contextMode }}；长期记忆：{{ context.memoryMode }}；
      摘要：{{ context.summary ? `v${context.summary.version} · ${context.summary.status}` : '尚未生成' }}</p>
    <details v-if="context?.jobs?.length"><summary>最近后台任务（刷新查看进展）</summary>
      <p v-for="job in context.jobs" :key="job.id">{{ job.kind }} · {{ job.status }} {{ job.errorCode || '' }}</p>
    </details>
    <e-alert v-if="error" type="error" :message="error" show-icon style="margin-top:12px" />
    <e-empty v-if="!loading && !rows.length" description="当前状态没有记忆" />
    <article v-for="row in rows" :key="row.id" class="memory-entry">
      <div class="memory-heading"><strong>{{ typeName(row.memoryType) }} · {{ row.memoryKey || '无键事实' }}</strong>
        <e-tag>{{ row.status }} · v{{ row.version }}</e-tag></div>
      <p class="memory-content">{{ row.content || '正文已清除' }}</p>
      <details v-if="row.sourceMessageId || row.metadata?.scope">
        <summary>来源与适用范围</summary>
        <p>消息 {{ row.sourceMessageId || '手工保存' }}；会话 {{ row.sessionId || '已解除关联' }}</p>
        <blockquote v-if="row.metadata?.quote">{{ row.metadata.quote }}</blockquote>
        <p v-if="row.metadata?.scope">适用范围：{{ row.metadata.scope }}</p>
        <p v-if="row.metadata?.evidenceIds?.length">执行证据：{{ row.metadata.evidenceIds.join(', ') }}</p>
      </details>
      <p class="memory-detail">索引：{{ indexName(row.indexStatus) }}；重要性：{{ row.importance }}；有效至：{{ row.expiresAt || '未设置' }}</p>
      <e-space wrap>
        <e-button v-if="row.status === 'candidate'" :disabled="busy" @click="editEntry(row)">查看并确认 / 纠正</e-button>
        <e-button v-if="row.status === 'candidate'" :disabled="busy" @click="mutate('Reject', row)">拒绝</e-button>
        <e-button v-if="row.status === 'active' && row.memoryType !== 'experience'" :disabled="busy" @click="editEntry(row)">纠正</e-button>
        <e-button v-if="row.status === 'active'" :disabled="busy" @click="mutate('Archive', row)">归档</e-button>
        <e-button v-if="row.status === 'archived'" :disabled="busy" @click="mutate('Restore', row)">恢复</e-button>
        <e-popconfirm v-if="row.status !== 'forgotten'" title="清除这条记忆及派生索引？原聊天仍保留。" @confirm="mutate('Delete', row)">
          <e-button danger :disabled="busy">遗忘</e-button>
        </e-popconfirm>
      </e-space>
    </article>
    <e-button v-if="hasMore" :loading="loading" :disabled="busy" @click="load(true)">加载更多</e-button>
  </e-drawer>
  <e-modal :open="!!editing" :title="editing?.id ? '确认或纠正记忆' : '手工保存记忆'" :confirm-loading="busy"
    :cancel-button-props="{ disabled: busy }" :closable="!busy" :mask-closable="!busy" @ok="save" @cancel="editing = null">
    <e-form layout="vertical">
      <e-form-item label="类型"><e-select v-model:value="draft.memoryType" :disabled="!!editing?.id || busy" :options="types" /></e-form-item>
      <e-form-item label="事实键 / 画像字段" extra="画像仅支持 language、response_format、timezone、accessibility。纠正保持原键。">
        <e-input v-model:value="draft.memoryKey" :disabled="!!editing?.id || busy" :maxlength="160" />
      </e-form-item>
      <e-form-item label="内容"><e-textarea v-model:value="draft.content" :rows="6" :maxlength="8000" :disabled="busy" /></e-form-item>
      <e-form-item label="重要性"><e-input-number v-model:value="draft.importance" :min="0" :max="1" :step="0.1" :disabled="busy" /></e-form-item>
      <e-form-item v-if="conflicts.length" label="确认替换的旧记忆（含版本）">
        <e-select v-model:value="draft.supersedesId" :disabled="busy" placeholder="请选择，避免静默覆盖"
          :options="conflicts.map(row => ({ value:String(row.id), label:`v${row.version} · ${row.content.slice(0,80)}` }))" />
      </e-form-item>
      <e-alert v-if="error" type="error" :message="error" />
    </e-form>
  </e-modal>
</template>

<script setup>
import { ref, reactive, watch, onUnmounted } from 'vue';
import { message } from '@/app/ui/controls';
import http from '@/utils/http';
import { experienceContext } from '@/app/experience/runtime';
const props = defineProps({ sessionId: { type:[String, Number], required:true } });
const open = ref(false), loading = ref(false), busy = ref(false), error = ref('');
const rows = ref([]), status = ref('candidate'), hasMore = ref(false), cursor = ref('0');
const editing = ref(null), conflicts = ref([]);
const context = ref(null);
const draft = reactive({ content:'', memoryKey:'', memoryType:'fact', importance:0.5, supersedesId:undefined });
const statuses = [['candidate','待确认'],['active','已生效'],['rejected','已拒绝'],['archived','已归档'],
  ['superseded','已修订'],['forgotten','已遗忘'],['pending','写入中'],['cancelled','已取消']].map(([value,label]) => ({value,label}));
const types = [['fact','事实'],['profile','小画像'],['project','项目状态']].map(([value,label]) => ({value,label}));
const typeName = value => [...types,{value:'experience',label:'经验'}].find(row => row.value === value)?.label || value;
const indexName = value => ({ready:'可用',unconfigured:'未配置向量模型（可用关键词）',
  requires_session_budget:'需在当前会话重建索引以计入预算',scheduled:'等待生效时间',pending:'排队中',running:'处理中',
  failed:'失败',cancelled:'已取消',uncertain:'调用结果未知，禁止自动重发',invalidated:'已失效',expired:'已过期'}[value] || value);
let revision = 0, alive = true;
function data(response) {
  if (response?.code !== 200 || response.isSuccess === false) throw new Error(response?.message || '操作未成功');
  return response.data;
}
async function load(append = false) {
  if (!open.value || !props.sessionId) return;
  if (experienceContext.value) {
    const experience = experienceContext.value;
    rows.value = experience.session.mode === 'public' && experience.session.pages.includes('memory') ? [{
      id:'demo-memory', memoryType:'profile', memoryKey:'response_format', status:'active', version:1,
      content:'虚构示例：偏好简洁的中文回答。本体验不会写入长期记忆。', importance:0.5, indexStatus:'unconfigured',
    }] : [];
    error.value = experience.session.pages.includes('memory') ? '' : '管理员尚未开放记忆展示';
    hasMore.value = false; context.value = { contextMode:'trim', memoryMode:'off' }; return;
  }
  const ticket = ++revision;
  loading.value = true; error.value = '';
  try {
    const result = data(await http.get('/api/AIAgentMemory/List', { params:{ sessionId:props.sessionId,
      status:status.value, afterId:append ? cursor.value : '0' } }));
    let progress = null;
    try { progress = data(await http.get('/api/AIChatHistorys/ContextStatus', {params:{chatId:props.sessionId}})); }
    catch { if (ticket === revision) error.value = '记忆已加载，但后台任务状态暂不可用'; }
    if (!alive || ticket !== revision) return;
    rows.value = append ? [...rows.value, ...result.items] : result.items;
    hasMore.value = result.hasMore; cursor.value = result.nextAfterId;
    context.value = progress;
  } catch (e) { if (ticket === revision) error.value = e.message; }
  finally { if (ticket === revision) loading.value = false; }
}
async function mutate(action, row) {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try {
    if (action === 'Reject') data(await http.post(`/api/AIAgentMemory/${action}`, { id:String(row.id), expectedVersion:row.version }));
    else if (action === 'Delete') data(await http.delete('/api/AIAgentMemory/Delete', {params:{Id:String(row.id)}}));
    else data(await http.post(`/api/AIAgentMemory/${action}`, null, {params:{Id:String(row.id)}}));
    await load(false);
  } catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
function createEntry() {
  editing.value = {}; conflicts.value = []; error.value = '';
  Object.assign(draft, {content:'',memoryKey:'',memoryType:'fact',importance:0.5,supersedesId:undefined});
}
async function editEntry(row) {
  if (busy.value) return;
  const ticket = revision;
  busy.value = true; error.value = '';
  try {
    let existing = [];
    if (row.status === 'candidate' && row.memoryKey) {
      const result = data(await http.get('/api/AIAgentMemory/List', {params:{sessionId:props.sessionId,status:'active',memoryKey:row.memoryKey}}));
      if (result.hasMore) throw new Error('同键冲突过多，请先整理已生效记忆');
      const now = Date.now();
      existing = result.items.filter(item => (!item.validUntil || Date.parse(item.validUntil) > now) && (!item.expiresAt || Date.parse(item.expiresAt) > now));
    }
    if (!alive || ticket !== revision) return;
    conflicts.value = existing;
    Object.assign(draft, {content:row.content,memoryKey:row.memoryKey || '',memoryType:row.memoryType,importance:row.importance,supersedesId:undefined});
    editing.value = row;
  } catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
async function save() {
  if (busy.value || !editing.value || !draft.content.trim()) return;
  busy.value = true; error.value = '';
  const row = editing.value;
  try {
    if (row.status === 'candidate') {
      const previous = conflicts.value.find(item => String(item.id) === draft.supersedesId);
      data(await http.post('/api/AIAgentMemory/Confirm', {id:String(row.id),expectedVersion:row.version,
        content:draft.content,importance:draft.importance,supersedesId:previous ? String(previous.id) : undefined, previousVersion:previous?.version}));
    } else {
      data(await http.post('/api/AIAgentMemory/Save', {sessionId:props.sessionId,content:draft.content,
        memoryKey:draft.memoryKey || null,memoryType:draft.memoryType,importance:draft.importance,
        supersedesId:row.id ? String(row.id) : undefined,expectedVersion:row.version}));
    }
    editing.value = null; await load(false);
  } catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
async function rebuildSearch() {
  if (busy.value || experienceContext.value) return;
  busy.value = true; error.value = '';
  const sessionId = props.sessionId;
  try {
    let afterId = '0', more = true, indexed = 0, queued = 0;
    while (more && alive && props.sessionId === sessionId) {
      const result = data(await http.post('/api/AIAgentMemory/RebuildSearchIndex', {sessionId,afterId}));
      indexed += result.indexed; queued += result.projectionQueued; more = result.hasMore;
      if (more && result.nextAfterId === afterId) throw new Error('索引游标未推进');
      afterId = result.nextAfterId;
    }
    if (!alive || props.sessionId !== sessionId) return;
    message.success(`已整理 ${indexed} 项记忆检索索引，提交 ${queued} 项向量投影同步；未调用模型`);
    await load(false);
  } catch (e) { if (alive && props.sessionId === sessionId) error.value = e.message; }
  finally { busy.value = false; }
}
async function reindex() {
  if (busy.value) return;
  busy.value = true; error.value = '';
  const sessionId = props.sessionId;
  try {
    let afterId = '0', more = true, queued = 0;
    while (more && alive && props.sessionId === sessionId) {
      const result = data(await http.post('/api/AIAgentMemory/Reindex', {sessionId,afterId}));
      queued += result.queued; more = result.hasMore;
      if (more && result.nextAfterId === afterId) throw new Error('索引游标未推进');
      afterId = result.nextAfterId;
    }
    message.success(`已提交 ${queued} 项索引任务`); await load(false);
  } catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
async function compact() {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try {
    const result = data(await http.post('/api/AIChatHistorys/Compact', null, {params:{chatId:props.sessionId}}));
    message.info(result.queued ? '已加入后台摘要队列，请稍后刷新状态' : '没有需要摘要的旧轮次或策略未启用');
    await load(false);
  } catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
watch([open,status,() => props.sessionId], () => {
  ++revision; rows.value = []; context.value = null; editing.value = null; conflicts.value = []; hasMore.value = false; loading.value = false;
  if (open.value) load(false);
});
onUnmounted(() => { alive = false; ++revision; });
</script>

<style scoped>
.memory-entry { margin-top:16px; padding:16px; border:1px solid var(--eic-line); border-radius:8px; background:var(--eic-surface); }
.memory-heading { display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; }
.memory-content, blockquote { white-space:pre-wrap; overflow-wrap:anywhere; }
.memory-detail { color:var(--eic-muted); font-size:var(--eic-font-meta); }
blockquote { margin:8px 0; padding-left:12px; border-left:3px solid var(--eic-line-strong); }
</style>
