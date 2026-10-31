<template>
  <div class="task-workspace">
    <header class="task-heading eic-page-toolbar"><p>知识库检查与 Agent 任务 · 计划与执行记录</p>
      <e-space wrap><e-button :loading="loading" :disabled="!!busy || saving" @click="load"><ReloadOutlined aria-hidden="true" />刷新</e-button>
        <e-button :disabled="!canCreate || !permissions.knowledge || loading || !!busy || saving" @click="showDemoTask"><ExperimentOutlined aria-hidden="true" />演示创建</e-button>
        <e-button type="primary" :disabled="!canCreate || loading || !!busy || saving" @click="showAddTask"><PlusOutlined aria-hidden="true" />创建任务</e-button></e-space></header>
    <details class="task-guide">
      <summary class="task-guide-heading">任务操作指南<span>创建 · 运行 · 结果 · 暂停</span></summary>
      <ol>
        <li><span>01</span><div><strong>创建与编辑</strong><p>选择知识库检查或 Agent，填写计划及任务内容。提交后会保存真实计划。</p></div></li>
        <li><span>02</span><div><strong>运行</strong><p>点击“立即运行”。排队不是完成，最终状态以持久执行记录为准。</p></div></li>
        <li><span>03</span><div><strong>看结果</strong><p>打开“查看结果”，刷新持久记录，查看知识库报告或 Agent 回答、停止原因与重试安全提示。</p></div></li>
        <li><span>04</span><div><strong>暂停</strong><p>取消后续计划及未完成记录，不撤回已发生动作。编辑保存会重新启用计划。</p></div></li>
      </ol>
      <details><summary>演示步骤与能力边界</summary><p>知识库检查为只读；Agent 任务可能调用模型与工具，消耗预算并产生副作用。执行时由服务重新核验租户、权限、凭据和预算。hold 仅处理漏触发，准点计划仍会派发；取消不撤回已发生动作。</p>
        <p>操作演示：演示创建 → 选择当前租户知识库 → 创建任务 → 立即运行 → 查看结果并刷新 → 暂停后续计划。只有后端返回最终完成记录时才显示完成；失败、队列不可用或结果待确认会保留真实状态。</p>
        <p>创建和运行需要任务写入及所选资源权限，并依赖可用执行服务；只读账户可查看自己的任务。编辑会替换后续计划；运行中或结果不确定时先处理记录。删除会使任务与记录不再可读。</p></details>
    </details>
    <e-alert v-if="operationNotice" class="task-operation-notice" :type="operationNotice.type" show-icon :message="operationNotice.text"
      :description="`任务 ID ${operationNotice.taskId} · 运行 ID ${operationNotice.runId || '未提供（未确认执行成功）'}`" />
    <e-alert v-if="!permissions.read" type="warning" show-icon message="没有读取任务的权限" />
    <e-alert v-if="error && !permissions.read" type="error" show-icon :message="error" />
    <template v-else>
      <e-alert v-if="!capability.execution" type="info" show-icon :message="capability.reason || '执行能力未确认，写入执行入口保持关闭'" />
      <e-alert v-if="!permissions.write" type="info" show-icon message="只读任务视图：当前账户没有任务写入权限。" />
      <section class="task-panel">
        <div class="task-filters">
          <e-input v-model:value="query" aria-label="搜索任务" placeholder="搜索任务名称、ID 或 handler" allow-clear />
          <e-select v-model:value="bucket" aria-label="筛选任务状态" :options="groups" />
          <span>{{ loading || error ? '数量未提供' : `当前账户 ${rows.length} 项任务` }}</span>
        </div>
        <e-alert v-if="error" type="error" show-icon :message="error" />
        <e-skeleton v-if="loading" active />
        <template v-else-if="!error">
          <e-empty v-if="!filtered.length" :description="query || bucket !== 'all' ? '没有匹配的任务' : '暂无任务'" />
          <div v-else class="task-flow-list">
            <article v-for="task in filtered.slice((page - 1) * 12, page * 12)" :key="task.key" class="task-row">
              <div class="task-main"><e-tag :color="taskStatus(task.status).color">{{ taskStatus(task.status).label }}</e-tag>
                <h2>{{ task.name }}</h2><p>ID {{ task.id || '未提供' }} · {{ task.handlerType || 'handler 未提供' }}</p></div>
              <dl class="task-schedule"><div><dt>下次计划</dt><dd>{{ taskDate(task.next) }}</dd></div>
                <div><dt>最近执行</dt><dd>{{ taskDate(task.last) }}</dd></div>
                <div><dt>Cron / 时区</dt><dd>{{ task.cron || '未提供' }} / {{ task.timeZone || '未提供' }}</dd></div></dl>
              <div class="task-actions">
                <e-button :disabled="!task.id" @click="selected = task.id"><HistoryOutlined aria-hidden="true" />查看结果</e-button>
                <e-button v-if="permissions.write" :disabled="!canEdit(task)" @click="showEditTask(task)">编辑</e-button>
                <e-button v-if="permissions.execute" type="primary" :loading="busy === `execute:${task.id}`"
                  :disabled="!canExecute(task)" @click="execute(task)"><PlayCircleOutlined aria-hidden="true" />立即运行</e-button>
                <e-button v-if="permissions.write" :disabled="!task.id || !task.enabled || !!busy" @click="confirmCancel(task)"><PauseCircleOutlined aria-hidden="true" />暂停后续计划</e-button>
                <e-button v-if="permissions.write" danger :disabled="!task.id || !uniqueName(task) || !!busy" @click="confirmDelete(task)">删除</e-button>
              </div>
            </article>
          </div>
          <e-pagination v-if="filtered.length > 12" v-model:current="page" :total="filtered.length" :page-size="12" :show-size-changer="false" />
        </template>
      </section>
    </template>
    <TaskRunDrawer :open="!!selected" :task-id="selected" @close="selected = ''" />
    <e-modal v-model:open="modalOpen" :title="editingId ? '编辑任务（替换后续计划）' : demoCreation ? '演示创建 · 知识库检查任务' : '创建任务'" :ok-text="editingId ? '保存计划' : '创建任务'" cancel-text="取消"
      :confirm-loading="saving" :closable="!saving" :mask-closable="!saving" :keyboard="!saving"
      :ok-button-props="{ disabled: !canSubmit }" :cancel-button-props="{ disabled: saving }" @ok="save">
      <e-form layout="vertical" :disabled="saving">
        <e-alert v-if="demoCreation" type="warning" show-icon message="提交将创建真实任务；当前只读检查不调用 Agent 或模型。" style="margin-bottom:16px" />
        <e-form-item label="任务名称" required><e-input v-model:value="form.name" :maxlength="200" placeholder="例如：知识库每日检查" /></e-form-item>
        <e-form-item label="Cron" required><e-input v-model:value="form.cron" placeholder="0 2 * * *" /></e-form-item>
        <e-form-item label="时区" required><e-input v-model:value="form.timeZone" placeholder="Asia/Shanghai" /></e-form-item>
        <e-form-item label="任务类型" required><e-select v-model:value="form.handlerType" :disabled="demoCreation" :options="handlerOptions" /></e-form-item>
        <e-form-item v-if="form.handlerType === 'knowledge-check'" label="知识库" required><e-select v-model:value="form.knowledgeBaseId" :options="knowledgeOptions" :loading="knowledgeLoading" :disabled="!knowledgeReady || knowledgeLoading" show-search option-filter-prop="label" placeholder="选择当前账户可读知识库" />
          <e-alert v-if="knowledgeError" type="error" show-icon :message="knowledgeError" /></e-form-item>
        <template v-else>
          <e-form-item label="Agent / App" required><e-select v-model:value="form.agentId" :options="agentOptions" :loading="agentLoading" show-search option-filter-prop="label" placeholder="选择当前账户可用 Agent" />
            <e-alert v-if="agentError" type="error" show-icon :message="agentError" />
            <e-button v-if="agentError" @click="loadAgentOptions">重新读取</e-button></e-form-item>
          <e-form-item label="任务内容" required><e-textarea v-model:value="form.prompt" :rows="5" :maxlength="16000" placeholder="描述每次定时运行要完成的任务，请勿填入密钥" /></e-form-item>
          <e-form-item label="最大执行步数" required><e-input-number v-model:value="form.maxSteps" :min="1" :max="32" :precision="0" /></e-form-item>
          <e-alert type="warning" show-icon message="Agent 可消耗模型预算并调用工具；每次执行重新授权，人工审批不会被定时任务绕过。" />
          <e-alert type="info" show-icon message="定时 Agent 暂不支持 RAG/知识库、MCP 或 responseSchema 聊天上下文。请使用不带这些配置的 App；服务会按保存配置检查，非空 mcps（即使未勾选）也可能被拒绝。此限制不影响聊天使用 MCP。" />
        </template>
        <e-form-item><e-checkbox v-model:checked="scheduleAcknowledged">我已知悉：提交会创建真实任务，准点计划仍会自动派发；hold 仅处理漏触发，演示结束需暂停后续计划。</e-checkbox></e-form-item>
      </e-form>
      <e-alert type="info" message="hold 不是暂停开关，准点计划仍会自动派发。创建失败后同内容重试复用幂等键；编辑会替换旧计划并重新启用任务。" />
    </e-modal>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue';
import { message, Modal } from '@/app/ui/controls';
import { experienceContext } from '@/app/experience/runtime';
import { ReloadOutlined, PlusOutlined, ExperimentOutlined, HistoryOutlined, PlayCircleOutlined, PauseCircleOutlined } from '@/app/ui/icons';
import { getAIKmssALLList } from '@/api/ai/aikmss';
import { getMyAIAppsALLList } from '@/api/ai/aiapps';
import { toUserMessage } from '@/utils/userMessages';
import { getUserPermissions } from '@/api/baseapi';
import { getTokenUser } from '@/api/userapi';
import { resolveWorkspaceIdentity } from '@/shared/ui/workbenchContract';
import { getTaskList, getTaskCapabilities, addEditTask, triggerCronTask, cancelTask, removeCronTask } from '@/api/ai/aitask';
import { taskPermissions, taskRows, filterTasks, taskStatus, taskDate } from '@/shared/ui/taskWorkspace';
import TaskRunDrawer from '@/components/ai/TaskRunDrawer.vue';

const permissions = ref({ read:false, write:false, execute:false });
const rows = ref([]);
const loading = ref(false);
const error = ref('');
const capability = ref({ execution:false, reason:'' });
const query = ref('');
const bucket = ref('all');
const page = ref(1);
const selected = ref('');
const busy = ref('');
const modalOpen = ref(false);
const saving = ref(false);
const demoCreation = ref(false), knowledgeOptions = ref([]), knowledgeLoading = ref(false), knowledgeError = ref('');
const knowledgeReady = ref(0), scheduleAcknowledged = ref(false);
const editingId = ref('');
const agentOptions = ref([]), agentLoading = ref(false), agentError = ref(''), agentReady = ref(false);
let agentSequence = 0;
const operationNotice = ref(null);
const emptyForm = () => ({ name:'', cron:'0 2 * * *', timeZone:'Asia/Shanghai', knowledgeBaseId:'', handlerType:'knowledge-check', agentId:'', referenceKey:'agentId', prompt:'', maxSteps:8, schedulePolicy:'hold', maxRetries:3, description:'' });
const form = ref(emptyForm());
const creationKeys = new Map();
const groups = [{ value:'all', label:'全部状态' }, { value:'waiting', label:'待处理' },
  { value:'active', label:'运行中' }, { value:'attention', label:'需关注' },
  { value:'done', label:'已完成' }, { value:'inactive', label:'已取消 / 禁用' }];
const filtered = computed(() => filterTasks(rows.value, query.value, bucket.value));
const canCreate = computed(() => permissions.value.read && permissions.value.execute && capability.value.execution &&
  (permissions.value.knowledge || permissions.value.agent));
const handlerOptions = computed(() => [{ value:'knowledge-check', label:'知识库健康检查', disabled:!permissions.value.knowledge },
  { value:'agent', label:'Agent 模型任务', disabled:!permissions.value.agent || !capability.value.handlerTypes?.includes('agent') }]);
const canSubmit = computed(() => canCreate.value && modalOpen.value && scheduleAcknowledged.value &&
  (form.value.handlerType === 'knowledge-check' ? permissions.value.knowledge && knowledgeReady.value !== 0 && knowledgeReady.value === knowledgeSequence && !knowledgeLoading.value && !knowledgeError.value :
    form.value.handlerType === 'agent' && permissions.value.agent && capability.value.handlerTypes?.includes('agent') && agentReady.value && !agentLoading.value && !agentError.value));
const uniqueName = (task) => rows.value.filter((row) => row.name === task.name).length === 1;
const resourceAllowed = (task) => task.handlerType === 'agent' ? permissions.value.agent && capability.value.handlerTypes?.includes('agent') : task.handlerType === 'knowledge-check' && permissions.value.knowledge;
const canEdit = (task) => canCreate.value && resourceAllowed(task) && task.id && task.payload && (!task.taskType || task.taskType === 'cron') &&
  !['running', 'uncertain'].includes(task.status) && !busy.value && !loading.value && !saving.value;
const canExecute = (task) => canCreate.value && resourceAllowed(task) && task.id && task.canExecute && uniqueName(task) &&
  !['running', 'uncertain'].includes(task.status) && !busy.value && !loading.value && !saving.value;
let revision = 0, knowledgeSequence = 0, operationSequence = 0;
watch([query, bucket], () => { page.value = 1; operationSequence += 1; operationNotice.value = null; });
watch(() => filtered.value.length, () => { page.value = Math.min(page.value, Math.max(1, Math.ceil(filtered.value.length / 12))); });
watch(modalOpen, (open) => { if (!open) { invalidateKnowledge(); invalidateAgents(); } }, { flush:'sync' });
watch(selected, () => { operationSequence += 1; operationNotice.value = null; }, { flush:'sync' });

function invalidateKnowledge() {
  knowledgeSequence += 1;
  knowledgeReady.value = 0;
  knowledgeOptions.value = []; knowledgeError.value = ''; knowledgeLoading.value = false;
  scheduleAcknowledged.value = false;
  form.value.knowledgeBaseId = '';
}

async function load() {
  const ticket = ++revision;
  operationNotice.value = null;
  modalOpen.value = false;
  invalidateKnowledge();
  permissions.value = { read:false, write:false, execute:false };
  rows.value = [];
  error.value = '';
  capability.value = { execution:false, reason:'正在核验队列与 worker 能力' };
  loading.value = true;
  try {
    const identity = await resolveWorkspaceIdentity(getUserPermissions, getTokenUser);
    if (ticket !== revision) return;
    if (experienceContext.value) {
      permissions.value = { read:true, write:true, execute:true, knowledge:true, agent:true };
    } else {
      localStorage.setItem('UserPermissions', JSON.stringify(identity.permissions));
      localStorage.setItem('user', JSON.stringify(identity.user));
      permissions.value = taskPermissions();
    }
  } catch {
    if (ticket === revision) {
      error.value = '账户权限核验失败，操作入口保持关闭';
      loading.value = false;
    }
    return;
  }
  if (!permissions.value.read) { loading.value = false; return; }
  const capabilityPromise = getTaskCapabilities().then((response) => {
    if (ticket !== revision) return;
    if (response?.code !== 200 || response?.isSuccess === false) throw new Error('能力响应无效');
    capability.value = { execution:response.data?.execution === true, handlerTypes:Array.isArray(response.data?.handlerTypes) ? response.data.handlerTypes : [],
      reason:toUserMessage(response.data?.reason, '执行能力未提供，入口保持关闭') };
  }).catch(() => { if (ticket === revision) capability.value = { execution:false, reason:'能力服务不可用，执行入口保持关闭' }; });
  try {
    const response = await getTaskList();
    if (ticket !== revision) return;
    if (response?.code !== 200 || response?.isSuccess === false) throw new Error(response?.message || '读取任务失败');
    rows.value = taskRows(response.data);
  } catch (failure) {
    if (ticket === revision) error.value = failure.message || '读取任务失败，请重试';
  } finally {
    await capabilityPromise;
    if (ticket === revision) loading.value = false;
  }
  return ticket === revision && !error.value && permissions.value.read;
}
function showAddTask() {
  if (!canCreate.value || saving.value) return;
  operationNotice.value = null;
  invalidateKnowledge();
  editingId.value = '';
  form.value = emptyForm();
  if (!permissions.value.knowledge && permissions.value.agent) form.value.handlerType = 'agent';
  demoCreation.value = false;
  modalOpen.value = true;
  loadKnowledgeOptions();
  loadAgentOptions();
}
function showEditTask(task) {
  if (!canEdit(task)) return;
  showAddTask();
  editingId.value = task.id;
  const input = task.payload;
  form.value = { ...emptyForm(), name:task.name, cron:task.cron, timeZone:task.timeZone || input.timeZone,
    handlerType:task.handlerType, knowledgeBaseId:String(input.knowledge_base_id || ''),
    agentId:String(input.agentId || input.appId || ''), referenceKey:input.appId != null ? 'appId' : 'agentId', prompt:typeof input.prompt === 'string' ? input.prompt : '', maxSteps:input.maxSteps ?? 8,
    schedulePolicy:task.schedulePolicy || input.schedulePolicy || 'hold', maxRetries:task.maxRetries ?? 3, description:input.description || '' };
}
function invalidateAgents() {
  agentSequence += 1; agentOptions.value = []; agentReady.value = false; agentError.value = ''; agentLoading.value = false;
}
async function loadAgentOptions() {
  invalidateAgents();
  if (!modalOpen.value || !permissions.value.agent) return;
  const ticket = agentSequence;
  const current = () => ticket === agentSequence && modalOpen.value;
  agentLoading.value = true;
  try {
    const response = await getMyAIAppsALLList();
    if (!current()) return;
    if (response?.code !== 200 || response?.isSuccess === false || !Array.isArray(response.data)) throw new Error('Agent 列表响应无效');
    agentOptions.value = response.data.filter((row) => row && row.enabled !== false && /^[1-9][0-9]*$/.test(String(row.id))).map((row) => ({ value:String(row.id), label:row.name || String(row.id) }));
    agentReady.value = true;
  } catch (failure) { if (current()) agentError.value = toUserMessage(failure.message, 'Agent 列表读取失败'); }
  finally { if (current()) agentLoading.value = false; }
}
function showDemoTask() {
  if (!canCreate.value || !permissions.value.knowledge || saving.value) return;
  showAddTask();
  form.value.handlerType = 'knowledge-check';
  demoCreation.value = true;
  form.value.name = `演示知识库检查-${new Date().toISOString().slice(0, 19)}`;
}
async function loadKnowledgeOptions() {
  if (!modalOpen.value || !permissions.value.knowledge) return;
  invalidateKnowledge();
  const ticket = knowledgeSequence;
  const current = () => ticket === knowledgeSequence && modalOpen.value;
  knowledgeLoading.value = true;
  try {
    const response = await getAIKmssALLList();
    if (!current()) return;
    if (response?.code !== 200 || response?.isSuccess === false || !Array.isArray(response.data)) throw new Error('知识库列表响应无效');
    knowledgeOptions.value = response.data.map((row) => ({ value:String(row.id), label:row.name || String(row.id) }));
    knowledgeReady.value = ticket;
  } catch (failure) { if (current()) knowledgeError.value = failure.message || '知识库读取失败'; }
  finally { if (current()) knowledgeLoading.value = false; }
}
async function save() {
  if (!canCreate.value || saving.value) return;
  operationNotice.value = null;
  if (!canSubmit.value) { message.warning('请等待资源列表加载完成，并确认真实计划会自动派发'); return; }
  if (!form.value.name.trim() || !form.value.cron.trim() || !form.value.timeZone?.trim()) { message.warning('请填写名称、Cron 和时区'); return; }
  let payload;
  if (form.value.handlerType === 'knowledge-check') {
    if (!knowledgeOptions.value.some((row) => row.value === form.value.knowledgeBaseId)) { message.warning('请选择当前账户可读知识库'); return; }
    payload = { handlerType:'knowledge-check', knowledge_base_id:form.value.knowledgeBaseId };
  } else {
    if (!agentOptions.value.some((row) => row.value === form.value.agentId) || !form.value.prompt.trim() || form.value.prompt.length > 16000 ||
      !Number.isInteger(form.value.maxSteps) || form.value.maxSteps < 1 || form.value.maxSteps > 32) { message.warning('请选择可用 Agent、填写任务内容及 1 至 32 的整数步数'); return; }
    if (!capability.value.handlerTypes?.includes('agent')) { message.warning('服务尚未注册 Agent 任务执行能力'); return; }
    payload = { handler:'agent', [form.value.referenceKey]:form.value.agentId, prompt:form.value.prompt.trim(), maxSteps:form.value.maxSteps };
  }
  const data = { ...(editingId.value ? { id:editingId.value } : {}), name:form.value.name.trim(), cron:form.value.cron.trim(), taskType:'cron', maxRetries:form.value.maxRetries,
    payload:{ ...payload, ...(form.value.description ? { description:form.value.description } : {}), timeZone:form.value.timeZone.trim(), schedulePolicy:form.value.schedulePolicy } };
  const fingerprint = JSON.stringify(data);
  if (!editingId.value && !creationKeys.has(fingerprint)) creationKeys.set(fingerprint,
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, '0')).join(''));
  saving.value = true;
  try {
    const response = await addEditTask(data, editingId.value ? undefined : creationKeys.get(fingerprint));
    if (response?.code !== 200 || response?.isSuccess === false || response.data !== true) throw new Error(response?.message || '任务保存失败');
    message.success('任务已保存');
    modalOpen.value = false;
    // 成功后的下一次同内容创建是新意图；不复用已完成创建的键。
    creationKeys.delete(fingerprint);
    await load();
  } catch (failure) { message.error(toUserMessage(failure.message, editingId.value ? '保存未确认，请刷新列表核对计划后再编辑' : '保存失败；相同内容重试将复用幂等键')); }
  finally { saving.value = false; }
}
async function mutate(task, action, request) {
  if (busy.value) return;
  const ticket = ++operationSequence, pageTicket = revision;
  operationNotice.value = null;
  busy.value = `${action}:${task.id}`;
  try {
    const response = await request();
    if (ticket !== operationSequence || pageTicket !== revision) return;
    if (response?.code !== 200 || response?.isSuccess === false) throw new Error(response?.message || '操作失败');
    let notice;
    const taskId = String(task.id);
    if (action === 'execute') {
      const status = response.data?.status;
      const runId = typeof response.data?.runId === 'string' ? response.data.runId.trim() :
        Number.isSafeInteger(response.data?.runId) && response.data.runId > 0 ? String(response.data.runId) : '';
      if (response.data?.taskId != null && String(response.data.taskId) !== taskId) throw new Error('任务响应归属不一致，请查看执行记录');
      if (status === 'success' && runId) notice = { taskId, runId, type:'success', text:'持久记录已完成，请查看运行结果。' };
      else if (status === 'queued') notice = { taskId, runId, type:'info', text:'任务已排队，尚未确认执行成功。' };
      else notice = { taskId, runId, type:'warning', text:status === 'success' ? '响应缺少运行 ID，尚未确认执行成功；请查看执行记录。' : `任务状态：${taskStatus(status).label}；请读取最终记录。` };
    } else {
      if (response.data !== true) throw new Error('操作未完成，请刷新确认');
      message.info(action === 'cancel' ? '后续计划已取消，已发生动作不会撤回' : '任务已删除');
      notice = { taskId, runId:'', type:'info', text:action === 'cancel' ? '后续计划已暂停（取消）；已发生动作不会撤回。' : '任务已删除。' };
    }
    const refreshedRevision = revision + 1;
    const refreshed = await load();
    if (refreshed && ticket === operationSequence && revision === refreshedRevision) operationNotice.value = notice;
  } catch (failure) {
    if (ticket === operationSequence && pageTicket === revision) {
      operationNotice.value = null;
      message.error(failure.message || '操作失败，请刷新后重试');
    }
  }
  finally { if (busy.value === `${action}:${task.id}`) busy.value = ''; }
}
function execute(task) { if (canExecute(task)) return mutate(task, 'execute', () => triggerCronTask(task.name)); }
function confirmCancel(task) {
  if (!permissions.value.write || busy.value || !task.id || !task.enabled) return;
  operationNotice.value = null;
  Modal.confirm({ title:'暂停后续计划？', content:'现有接口会取消后续计划及未完成的持久执行记录，不保证强制中断进行中的动作，也不会撤回已发生动作。再次编辑保存会重新启用计划。', okText:'确认暂停', cancelText:'返回',
    onOk:() => mutate(task, 'cancel', () => cancelTask(task.id)) });
}
function confirmDelete(task) {
  if (!permissions.value.write || busy.value || !task.id || !uniqueName(task)) return;
  operationNotice.value = null;
  Modal.confirm({ title:`删除任务“${task.name}”？`, content:'删除后任务和它的记录将不再可读。', okText:'确认删除', okType:'danger', cancelText:'返回',
    onOk:() => mutate(task, 'delete', () => removeCronTask(task.name)) });
}
onMounted(load);
onBeforeUnmount(() => { revision += 1; operationSequence += 1; operationNotice.value = null; invalidateKnowledge(); invalidateAgents(); });
</script>

<style scoped>
.task-workspace { color:var(--eic-text); min-width:0; }
.task-heading { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:10px 16px; margin-bottom:12px; }
.task-heading p { flex:1; color:var(--eic-text-secondary); margin:0; font-size:var(--eic-font-meta); }
.task-panel { margin-top:16px; padding:20px; border:1px solid var(--eic-line); border-radius:var(--eic-radius-lg); background:var(--eic-surface); }
.task-operation-notice { margin-bottom:16px; }
.task-guide { padding:0 16px; border:1px solid var(--eic-line); border-radius:10px; background:var(--eic-surface-muted); }
.task-guide-heading { position:relative; padding-right:24px !important; font-weight:600; }
.task-guide-heading::after { content:'+'; position:absolute; right:0; top:8px; }
.task-guide[open] > .task-guide-heading::after { content:'-'; }
.task-guide-heading > span { margin-left:16px; font-weight:400; }
.task-guide-heading > span { color:var(--eic-text-secondary); font-size:12px; }
.task-guide ol { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); padding:0; margin:20px 0 16px; gap:24px; list-style:none; }
.task-guide li { display:flex; align-items:flex-start; gap:10px; }
.task-guide li > span { color:var(--eic-primary-dark); font-size:12px; font-weight:600; line-height:22px; }
.task-guide strong { font-size:14px; }
.task-guide p { font-size:13px; line-height:1.7; color:var(--eic-text-secondary); margin:6px 0 0; }
.task-guide summary { cursor:pointer; color:var(--eic-primary-dark); font-size:13px; padding:8px 0; }
.task-guide summary:focus-visible { outline:2px solid var(--eic-primary); outline-offset:4px; }
.task-filters { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin-bottom:20px; }
.task-filters > :first-child { flex:1 1 260px; min-width:180px; }
.task-filters > :nth-child(2) { flex:0 1 180px; width:180px; }
.task-filters > span { margin-left:auto; white-space:nowrap; }
.task-filters span { color:var(--eic-text-secondary); font-size:13px; }
.task-row { display:grid; grid-template-columns:minmax(180px,1fr) minmax(240px,1.2fr); gap:12px 24px; border-bottom:1px solid var(--eic-line); padding:20px 0; }
.task-main h2 { margin:8px 0 4px; font-size:17px; overflow-wrap:anywhere; } .task-main p { margin:0; font-size:12px; color:var(--eic-text-secondary); overflow-wrap:anywhere; }
.task-schedule { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:0; }
.task-schedule div:last-child { grid-column:1/-1; } dt { font-size:12px; color:var(--eic-text-secondary); } dd { margin:4px 0 0; font-size:13px; overflow-wrap:anywhere; }
.task-actions { display:flex; flex-wrap:wrap; gap:8px; grid-column:1/-1; }
@media(max-width:600px) { .task-heading { align-items:stretch; flex-direction:column; gap:10px; }
  .task-guide-heading > span { display:block; margin-left:0; } .task-guide ol { grid-template-columns:1fr; gap:16px; }
  .task-panel { padding:16px; } .task-row { grid-template-columns:1fr; } }
.task-main h2 { font-size:var(--eic-font-section); }
.task-guide-heading > span,.task-guide li > span,.task-main p,dt { font-size:var(--eic-font-meta); }
.task-guide strong,.task-guide p,.task-filters span,dd { font-size:var(--eic-font-ui); }
.task-guide summary { font-size:var(--eic-font-button); }
</style>
