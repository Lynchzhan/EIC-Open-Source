<template>
  <div class="agent-workspace v4-page">
    <header class="agent-page-heading eic-page-toolbar">
      <p>配置、试用与执行结果</p>
      <n-space wrap>
        <n-button :loading="loading" @click="loadAgents">刷新列表</n-button>
        <n-button v-if="canWrite" type="primary" :disabled="editorLoading" @click="openEditor()">添加智能体</n-button>
      </n-space>
    </header>
    <n-alert v-if="loadError" type="error" show-icon class="agent-alert">{{ loadError }}</n-alert>
    <n-alert v-if="!loading && !canWrite" type="info" show-icon class="agent-alert"
     >{{ management ? '当前为只读模式；管理操作需要 ai.resource.write 权限。' : '当前为只读查看；试用会保存到你自己的会话，不修改智能体配置。' }}</n-alert>
    <section class="agent-collection" aria-label="智能体列表" :aria-busy="loading">
      <div class="agent-filters">
        <n-input v-model:value="query" placeholder="搜索名称、描述或负责人" aria-label="搜索智能体" clearable class="agent-search" />
        <n-select v-model:value="status" aria-label="按配置状态筛选" :options="statusOptions" />
        <n-select v-model:value="type" aria-label="按类型筛选" :options="typeOptions" />
        <span class="agent-count">{{ filtered.length }} 个结果 / {{ agents.length }} 个已返回智能体</span>
      </div>
      <n-spin :show="loading" description="正在加载智能体">
        <div class="agent-grid">
          <article v-for="agent in pageAgents" :key="agent.id" class="agent-tile">
            <div class="agent-tile-top"><span class="agent-mark" aria-hidden="true"><RobotOutlined /></span>
              <span class="agent-state" :class="agentConfiguration(agent).key">{{ agentConfiguration(agent).label }}</span></div>
            <h2>{{ agent.name || '未命名智能体' }}</h2>
            <p class="agent-description">{{ agent.describe || agent.description || '描述未提供' }}</p>
            <dl class="agent-metadata">
              <div><dt>类型</dt><dd>{{ agent.type || '未提供' }}</dd></div>
              <div><dt>负责人</dt><dd>{{ agent.ownerName || '未提供' }}</dd></div>
              <div><dt>最近运行</dt><dd>{{ agent.lastRunAt || '暂未接入' }}</dd></div>
              <div><dt>权限范围</dt><dd>{{ agent.permissionScope || '同租户资源；细分范围暂未接入' }}</dd></div>
            </dl>
            <div class="agent-tile-actions">
              <n-button type="primary" secondary @click="openDetails(agent)">查看与试用</n-button>
              <n-button v-if="canWrite" :disabled="editorLoading" @click="openEditor(agent)">编辑</n-button>
              <n-popconfirm v-if="canWrite" positive-text="删除" negative-text="取消" @positive-click="removeAgent(agent)">
                <template #trigger><n-button type="error" secondary :loading="deletingId === String(agent.id)">删除</n-button></template>
                确认删除这个智能体？
              </n-popconfirm>
            </div>
          </article>
        </div>
        <n-empty v-if="!loading && !loadError && !filtered.length" description="没有匹配的智能体，请调整筛选条件" />
      </n-spin>
      <n-pagination v-if="filtered.length" v-model:page="page" :page-size="pageSize" :item-count="filtered.length" class="agent-pagination">
        <template #prefix>共 {{ filtered.length }} 个智能体</template>
      </n-pagination>
    </section>
    <n-drawer :show="detailOpen" :width="detailWidth" :mask-closable="!trialState.busy" :close-on-esc="!trialState.busy"
      @update:show="(show) => { if (!show) closeDetails() }">
      <n-drawer-content title="智能体详情与试用" closable @close="closeDetails">
      <div class="agent-detail" :aria-busy="detailLoading">
        <n-spin v-if="detailLoading" description="正在加载真实配置" />
        <n-alert v-if="detailError" type="error" show-icon>{{ detailError }}
          <n-button @click="loadDetails(selectedId)">重试详情</n-button>
        </n-alert>
        <template v-if="detail && !detailLoading && !detailError">
          <div class="agent-detail-heading"><h2>{{ detail.name }}</h2><p>{{ detail.describe || detail.description || '描述未提供' }}</p></div>
          <div class="agent-dimensions">
            <section v-for="dimension in agentDimensions(detail)" :key="dimension.name">
              <h3>{{ dimension.name }}</h3><dl>
                <div v-for="[label, value] in dimension.items" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></div>
              </dl>
            </section>
          </div>
          <section class="agent-trial" aria-label="内嵌试用">
            <div class="agent-trial-heading"><div><h3>内嵌试用</h3><p>{{ experienceContext ? '体验模式请从 AI 对话选择授权智能体；工具与长期记忆关闭。' : '使用真实会话、SSE、引用、工具和人工确认链路。' }}</p></div>
              <n-button v-if="!trialOpen" type="primary" :disabled="!canRead || detail.enabled === false" @click="trialOpen = true">打开试用</n-button>
              <n-button v-else :disabled="trialState.busy" @click="closeTrial">收起试用</n-button>
            </div>
            <p v-if="!trialOpen" class="agent-trial-empty">{{ experienceContext ? '这里只展示授权配置，体验对话不会保存到正式个人会话。' : '点击打开试用后显式创建个人会话；不会自动运行或读取其他智能体的历史。' }}</p>
            <template v-else>
              <p class="agent-trial-note">试用会话会保留在 AI 对话中。生成期间请先停止，再关闭；关闭面板本身不取消后端执行。</p>
              <p v-if="trialState.pending" role="status">当前请求等待人工确认，收起后可从 AI 对话继续处理。</p>
              <MyAIChat :key="String(detail.id)" embedded :agent-id="String(detail.id)" @runtime-state="trialState = $event" />
            </template>
          </section>
        </template>
      </div>
    </n-drawer-content>
    </n-drawer>
    <AgentAddEdit v-if="editorOpen" :open="editorOpen" :modal-type="editorType" :agent-data="editorAgent"
      :submit="saveAgent" @cancel="editorOpen = false" />
  </div>
</template>
<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { message } from '@/app/ui/feedback';
import { experienceContext } from '@/app/experience/runtime';
import { NButton, NSpace, NAlert, NInput, NSelect, NSpin, NEmpty, NPagination, NPopconfirm, NDrawer, NDrawerContent } from 'naive-ui';
import { HardwareChipOutline as RobotOutlined } from '@vicons/ionicons5';
import { getUserPermissions } from '@/api/baseapi';
import { getAIAppsALLList, getMyAIAppsALLList, getAIAppsDetails, addEditAIApp, deleteAIApp, NewInitialization } from '@/api/ai/aiapps';
import { isMutationSuccess } from '@/utils/http';
import { agentConfiguration, agentDimensions, filterAgents, hasAgentPermission } from '@/shared/ui/agentWorkspace';
import AgentAddEdit from './AgentAddEdit.vue';
import MyAIChat from '@/pages/ai/MyAIChat.vue';
import './agentWorkspace.css';
const props = defineProps({ management: Boolean });
const agents = ref([]), permissions = ref([]), loading = ref(false), loadError = ref('');
const query = ref(''), status = ref('all'), type = ref('all'), page = ref(1), pageSize = 6;
const canRead = computed(() => hasAgentPermission(permissions.value, 'ai.resource.read'));
const canWrite = computed(() => props.management && hasAgentPermission(permissions.value, 'ai.resource.write'));
const filtered = computed(() => filterAgents(agents.value, query.value, status.value, type.value));
const pageAgents = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize));
const statusOptions = [{ value: 'all', label: '全部配置状态' }, { value: 'ready', label: '配置就绪' },
  { value: 'draft', label: '草稿' }, { value: 'disabled', label: '已停用' }, { value: 'unknown', label: '配置状态未提供' }];
const typeOptions = computed(() => [{ value: 'all', label: '全部类型' }, ...[...new Set(
  agents.value.map((agent) => agent.type).filter(Boolean))].map((value) => ({ value, label: value }))]);
watch([query, status, type], () => { page.value = 1; });
watch(filtered, (rows) => { page.value = Math.min(page.value, Math.max(1, Math.ceil(rows.length / pageSize))); });
const detailOpen = ref(false), detail = ref(null), selectedId = ref(null), detailLoading = ref(false), detailError = ref('');
const trialOpen = ref(false), trialState = ref({ busy: false, pending: false });
const viewport = ref(window.innerWidth);
const resize = () => { viewport.value = window.innerWidth; };
const detailWidth = computed(() => viewport.value < 768 ? '100%' : 'min(100vw, 1000px)');
const editorOpen = ref(false), editorType = ref('add'), editorAgent = ref(null), editorLoading = ref(false);
const deletingId = ref(null);
let alive = true, listSeq = 0, detailSeq = 0, editSeq = 0;
const errorText = (error, prefix) => `${prefix}：${error?.message || '请检查连接或访问权限后重试'}`;
const responseData = (response) => {
  if (response?.code !== 200 || response?.data == null) throw new Error(response?.msg || '接口返回未成功');
  return response.data;
};
const refreshPermissions = async () => {
  permissions.value = [];
  const rows = responseData(await getUserPermissions());
  if (!Array.isArray(rows)) throw new Error('权限响应无效');
  if (alive) permissions.value = rows;
  return rows;
};
async function loadAgents() {
  const seq = ++listSeq;
  loading.value = true; loadError.value = '';
  try {
    await refreshPermissions();
    const rows = responseData(await (props.management ? getAIAppsALLList() : getMyAIAppsALLList()));
    if (!Array.isArray(rows)) throw new Error('智能体列表响应无效');
    if (alive && seq === listSeq) agents.value = rows;
  } catch (error) {
    if (alive && seq === listSeq) { agents.value = []; loadError.value = errorText(error, '智能体列表加载失败'); }
  } finally { if (alive && seq === listSeq) loading.value = false; }
}
async function loadDetails(id) {
  const seq = ++detailSeq;
  detail.value = null; detailLoading.value = true; detailError.value = '';
  try {
    const row = responseData(await getAIAppsDetails(id));
    if (String(row.id) !== String(id)) throw new Error('详情 ID 不匹配');
    if (alive && seq === detailSeq) detail.value = row;
  } catch (error) {
    if (alive && seq === detailSeq) detailError.value = errorText(error, '智能体详情加载失败');
  } finally { if (alive && seq === detailSeq) detailLoading.value = false; }
}
function openDetails(agent) {
  if (experienceContext.value) experienceContext.value.selectAgent(agent.id);
  if (trialState.value.busy) return message.warning('请先停止当前试用，再切换智能体');
  trialOpen.value = false; trialState.value = { busy: false, pending: false };
  selectedId.value = agent.id; detailOpen.value = true; loadDetails(agent.id);
}
function closeTrial() {
  if (trialState.value.busy) return message.warning('请先停止生成，再收起试用');
  trialOpen.value = false; trialState.value = { busy: false, pending: false };
}
function closeDetails() {
  if (trialState.value.busy) return message.warning('请先停止生成，再关闭详情');
  ++detailSeq; closeTrial(); detailOpen.value = false; detail.value = null;
}
async function openEditor(agent) {
  if (editorLoading.value || editorOpen.value) return;
  const seq = ++editSeq;
  editorLoading.value = true; editorAgent.value = null;
  try {
    const rights = await refreshPermissions();
    if (!hasAgentPermission(rights, 'ai.resource.write')) throw new Error('没有 ai.resource.write 权限');
    const row = responseData(await (agent ? getAIAppsDetails(agent.id) : NewInitialization()));
    if (agent && String(row.id) !== String(agent.id)) throw new Error('编辑详情 ID 不匹配');
    if (!alive || seq !== editSeq) return;
    editorType.value = agent ? 'edit' : 'add'; editorAgent.value = row; editorOpen.value = true;
  } catch (error) { if (alive) message.error(errorText(error, '打开编辑失败')); }
  finally { if (alive && seq === editSeq) editorLoading.value = false; }
}
async function saveAgent(params) {
  const rights = await refreshPermissions();
  if (!hasAgentPermission(rights, 'ai.resource.write')) throw new Error('管理权限已失效，请刷新');
  const response = await addEditAIApp(params);
  if (!isMutationSuccess(response)) throw new Error('保存未生效，请核对接口响应后重试');
  message.success(editorType.value === 'edit' ? '智能体更新成功' : '智能体添加成功');
  editorOpen.value = false;
  await loadAgents();
}
async function removeAgent(agent) {
  if (deletingId.value) return;
  if (trialState.value.busy) return message.warning('请先停止试用，再删除智能体');
  deletingId.value = String(agent.id);
  try {
    const rights = await refreshPermissions();
    if (!hasAgentPermission(rights, 'ai.resource.write')) throw new Error('管理权限已失效，请刷新');
    if (!isMutationSuccess(await deleteAIApp(agent.id))) throw new Error('删除未生效');
    message.success('智能体删除成功');
    if (String(selectedId.value) === String(agent.id)) closeDetails();
    await loadAgents();
  } catch (error) { message.error(errorText(error, '删除失败')); }
  finally { deletingId.value = null; }
}
onMounted(() => { window.addEventListener('resize', resize); loadAgents(); });
onUnmounted(() => { alive = false; ++listSeq; ++detailSeq; ++editSeq; window.removeEventListener('resize', resize); });
</script>
