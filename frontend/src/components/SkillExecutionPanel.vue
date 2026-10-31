<template>
  <section class="execution-section" aria-label="执行资源">
    <div class="execution-actions">
      <e-button :loading="loading" @click="refresh"><template #icon><ReloadOutlined /></template>刷新</e-button>
      <e-button v-if="type === 2 && catalog.canInstall" type="primary" :loading="installing" @click="install">
        <template #icon><PlusOutlined /></template>安装内置 Skill
      </e-button>
      <e-button v-if="type === 3 && catalog.canMcpWrite" @click="editMcp()"><template #icon><PlusOutlined /></template>添加 MCP</e-button>
    </div>
    <e-alert v-if="error" role="alert" type="error" :message="error" show-icon />
    <e-spin :spinning="loading">
      <div v-if="type === 1" class="execution-list">
        <div v-for="tool in catalog.tools || []" :key="tool.name" class="execution-row">
          <div class="execution-summary"><strong>{{ tool.name }}</strong><span>{{ tool.description }}</span></div>
          <e-tag>{{ tool.requiresApproval ? '需审批' : '本地执行器' }}</e-tag>
          <e-button :disabled="!tool.executable" @click="openRun('tool', tool)"><template #icon><PlayCircleOutlined /></template>运行</e-button>
        </div>
        <e-empty v-if="!catalog.tools?.length" description="暂无已授权执行器" />
      </div>
      <div v-else-if="type === 2" class="execution-list">
        <div v-for="skill in executableSkills" :key="skill.id" class="execution-row" :data-skill-key="skill.builtinKey">
          <div class="execution-summary"><strong>{{ titles[skill.builtinKey] || skill.name || `Skill ${skill.id}` }}</strong><span>{{ skill.executionMode }} · ID {{ skill.id }}</span></div>
          <e-tag :color="skill.enabled ? 'green' : 'default'">{{ skill.enabled ? modeLabel(skill.executionMode) : '已停用' }}</e-tag>
          <e-switch :checked="skill.enabled" :disabled="!catalog.canWrite || toggling" checked-children="开" un-checked-children="关" @change="value => toggle(skill, value)" />
          <e-button :disabled="!skill.executable || !skill.enabled" @click="openRun('skill', skill)"><template #icon><PlayCircleOutlined /></template>运行</e-button>
        </div>
        <e-empty v-if="!executableSkills.length" description="暂无内置或声明式 Skill，请上传声明包并保存" />
      </div>
      <div v-else class="execution-list">
        <div v-for="mcp in mcps" :key="mcp.id" class="execution-row">
          <div class="execution-summary"><strong>{{ mcp.name }}</strong><span>{{ mcp.transport }} · {{ mcp.endpoint }}</span></div>
          <e-tag>{{ mcp.enabled === false ? '已停用' : '已配置 · 未探测' }}</e-tag>
          <e-button v-if="catalog.canMcpWrite" aria-label="编辑 MCP" @click="editMcp(mcp)"><EditOutlined /></e-button>
          <e-popconfirm v-if="catalog.canMcpWrite" title="确认删除 MCP？" @confirm="removeMcp(mcp)"><e-button aria-label="删除 MCP"><DeleteOutlined /></e-button></e-popconfirm>
          <e-button :disabled="!catalog.canMcpCall || mcp.enabled === false" @click="openRun('mcp', mcp)"><template #icon><PlayCircleOutlined /></template>调用</e-button>
        </div>
        <e-empty v-if="!mcps.length" description="暂无 MCP 服务" />
      </div>
    </e-spin>
    <e-modal v-model:open="runVisible" :title="runTitle" :footer="null" width="720px" :mask-closable="!running" :closable="!running" :keyboard="!running">
      <e-form layout="vertical">
        <e-alert v-if="target?.kind === 'skill' && target.item.executionMode === 'declarative-v1'" type="info" show-icon message="按声明步骤执行受控工具；参数需符合包内 input_schema。仅指令包不会生成模型回答；中途失败可能已有副作用，请勿盲目重跑。" />
        <e-form-item v-if="target?.kind === 'skill' && target.item.requiresAppContext" label="授权应用 ID">
          <e-input v-model:value="skillAppId" :disabled="running" placeholder="应用须同时选择本 Skill 和所需 MCP；服务将重新授权" />
        </e-form-item>
        <e-form-item v-if="target?.kind === 'mcp'" label="工具名称"><e-input v-model:value="mcpToolName" :disabled="running" /></e-form-item>
        <e-form-item label="输入 JSON"><e-textarea v-model:value="argumentsText" :rows="7" :disabled="running" /></e-form-item>
        <e-button type="primary" :loading="running" @click="run"><template #icon><PlayCircleOutlined /></template>{{ target?.kind === 'mcp' ? '调用' : '执行' }}</e-button>
      </e-form>
      <e-alert v-if="runError" role="alert" :message="runError" type="error" show-icon class="run-feedback" />
      <div v-if="runResult" class="run-feedback" data-testid="skill-run-result">
        <e-tag :color="mcpToolFailed ? 'red' : runResult.executed === false ? 'default' : 'green'">{{ mcpToolFailed ? '工具执行失败' : runResult.executed === false ? '仅指令，未执行工具' : '服务已返回结果' }}</e-tag><e-tag>{{ target?.kind === 'mcp' ? 'MCP 返回' : modeLabel(runResult.executionMode || target?.item.executionMode) }}</e-tag>
        <pre>{{ JSON.stringify(runResult, null, 2) }}</pre>
      </div>
    </e-modal>
    <e-modal v-model:open="mcpVisible" title="MCP 配置" :confirm-loading="savingMcp" :closable="!savingMcp" :mask-closable="!savingMcp" :keyboard="!savingMcp" :cancel-button-props="{ disabled:savingMcp }" @ok="saveMcp">
      <e-form layout="vertical" :disabled="savingMcp">
        <e-form-item label="名称"><e-input v-model:value="mcpForm.name" /></e-form-item>
        <e-form-item label="Endpoint"><e-input v-model:value="mcpForm.endpoint" /></e-form-item>
        <e-form-item label="Transport"><e-select v-model:value="mcpForm.transport" :options="[{ value: 'http', label: 'HTTP' }, { value: 'sse', label: 'SSE' }]" /></e-form-item>
        <e-form-item label="Headers JSON"><e-textarea v-model:value="mcpForm.headersText" :rows="3" /></e-form-item>
        <p>编辑时 Headers 留为 {} 保留现有凭据；不会回显已保存密钥。</p>
        <e-form-item label="启用服务"><e-switch v-model:checked="mcpForm.enabled" /></e-form-item>
        <e-form-item label="超时（秒）"><e-input-number v-model:value="mcpForm.timeout" :min="1" :max="120" :precision="0" /></e-form-item>
        <e-form-item label="chatAutoTools（JSON 远程名数组）"><e-textarea v-model:value="mcpForm.autoText" :rows="3" :disabled="!isSuperAdmin || savingMcp" placeholder='["search"]' /></e-form-item>
        <e-form-item label="chatReadOnlyTools（JSON 远程名数组）"><e-textarea v-model:value="mcpForm.readOnlyText" :rows="3" :disabled="!isSuperAdmin || savingMcp" placeholder='["search"]' /></e-form-item>
        <e-alert type="warning" show-icon message="chatAutoTools 和 chatReadOnlyTools 两份名单都会授予模型自动调用所列工具的入口，均须系统管理员明确授权并填写远程原始工具名；只读名单不是仅作分类。远程 annotation 不授予权限，执行时仍核验租户、权限和预算。" />
        <p v-if="!isSuperAdmin">仅系统管理员可写名单，本次保存不提交名单字段。修改连接或凭据可能撤销原自动工具授权，需管理员重新核定。</p>
      </e-form>
    </e-modal>
  </section>
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { DeleteOutlined, EditOutlined, PlayCircleOutlined, PlusOutlined, ReloadOutlined } from '@/app/ui/icons';
import { message } from '@/app/ui/controls';
import { addMcp, callMcp, deleteMcp, executeSkill, executeTool, getExecutionCatalog, GetAllMcps, installBuiltinSkills, setSkillEnabled } from '@/api/ai/aiskilltoolManagement';
import { getMcpExecutionCapabilities } from '@/api/ai/aiskilltoolManagement';
import { createApprovalIdempotencyKey } from '@/shared/ui/chatApproval';

const props = defineProps({ type: { type: Number, required: true } });
const emit = defineEmits(['changed', 'capabilities']);
const catalog = ref({});
const mcps = ref([]);
const loading = ref(false);
const error = ref('');
const installing = ref(false);
const toggling = ref(false);
const runVisible = ref(false);
const running = ref(false);
const target = ref(null);
const argumentsText = ref('{}');
const mcpToolName = ref('');
const skillAppId = ref('');
const runError = ref('');
const runResult = ref(null);
const runKeys = new Map();
const mcpToolFailed = computed(() => runResult.value?.isError === true || runResult.value?.status === 'failed' || runResult.value?.status === 'uncertain');
const isSuperAdmin = ref(false);
const mcpVisible = ref(false);
const savingMcp = ref(false);
const mcpForm = reactive({ id: null, name: '', endpoint: '', transport: 'http', headersText: '{}', enabled:true, timeout:30, autoText:'[]', readOnlyText:'[]' });
const titles = computed(() => Object.fromEntries((catalog.value.builtins || []).map(item => [item.key, item.name])));
const executableSkills = computed(() => (catalog.value.skills || []).filter(item => ['deterministic', 'declarative-v1'].includes(item.executionMode)));
const modeLabel = (mode) => mode === 'declarative-v1' ? '声明式步骤' : mode === 'deterministic' ? '确定性 · 非模型' : '执行模式未提供';
const runTitle = computed(() => target.value ? `${target.value.kind === 'mcp' ? '调用' : '运行'} · ${target.value.item.builtinKey || target.value.item.name || target.value.item.id}` : '运行');
let refreshVersion = 0;

async function refresh() {
  const version = ++refreshVersion;
  loading.value = true;
  error.value = '';
  isSuperAdmin.value = false;
  catalog.value = {};
  emit('capabilities', {});
  try {
    const response = props.type === 3 ? await getMcpExecutionCapabilities() : await getExecutionCatalog();
    if (response?.code !== 200 || response.isSuccess === false || !response.data ||
        (props.type !== 3 && !Array.isArray(response.data.skills))) throw new Error('执行能力加载失败');
    const servers = props.type === 3 ? await GetAllMcps() : null;
    if (version !== refreshVersion) return;
    catalog.value = response.data;
    emit('capabilities', response.data);
    if (servers && (servers.code !== 200 || servers.isSuccess === false || !Array.isArray(servers.data))) throw new Error('MCP 列表读取失败');
    mcps.value = servers?.data || [];
    isSuperAdmin.value = props.type === 3 && response.data.isSuperAdmin === true;
  } catch (failure) {
    if (version !== refreshVersion) return;
    catalog.value = {};
    emit('capabilities', {});
    mcps.value = [];
    error.value = failure.message || '执行目录加载失败';
  } finally {
    if (version === refreshVersion) loading.value = false;
  }
}

async function install() {
  installing.value = true;
  try {
    await installBuiltinSkills((catalog.value.builtins || []).map(item => item.key));
    message.success('内置 Skill 安装完成');
    await refresh();
    emit('changed');
  } catch (failure) { error.value = failure.message; }
  finally { installing.value = false; }
}

async function toggle(skill, enabled) {
  if (!catalog.value.canWrite || toggling.value) return;
  toggling.value = true;
  try {
    // Snowflake ID 在请求体中保持字符串，避免 JavaScript Number 精度丢失。
    const response = await setSkillEnabled({ skillId: skill.id, enabled });
    if (response?.code !== 200 || response.isSuccess === false || response.data !== true) throw new Error('技能启停未确认');
    await refresh();
    emit('changed');
  } catch (failure) { error.value = failure.message; }
  finally { toggling.value = false; }
}

function openRun(kind, item) {
  if (running.value || (kind === 'skill' && (!item.enabled || !item.executable))) return;
  target.value = { kind, item };
  argumentsText.value = JSON.stringify(item.example || (item.name === 'calculator' ? { a: 1, b: 2 } : {}), null, 2);
  runResult.value = null;
  runError.value = '';
  mcpToolName.value = '';
  skillAppId.value = '';
  runVisible.value = true;
}

async function run() {
  if (running.value || !target.value) return;
  runError.value = '';
  runResult.value = null;
  let args;
  try {
    args = JSON.parse(argumentsText.value);
    if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('输入必须是 JSON 对象');
    if (target.value.kind === 'mcp' && !mcpToolName.value.trim()) throw new Error('请输入工具名称');
    if (target.value.kind === 'skill' && target.value.item.requiresAppContext
        && !/^[1-9][0-9]*$/.test(skillAppId.value.trim())) throw new Error('请输入授权应用 ID');
  } catch (failure) { runError.value = failure.message; return; }
  running.value = true;
  try {
    const { kind, item } = target.value;
    const remoteName = kind === 'mcp' ? mcpToolName.value.trim() : '';
    const fingerprint = JSON.stringify([kind, String(item.id || item.name), remoteName, skillAppId.value.trim(), args]);
    if (!runKeys.has(fingerprint)) runKeys.set(fingerprint, createApprovalIdempotencyKey());
    const idempotencyKey = runKeys.get(fingerprint);
    const response = kind === 'skill' ? await executeSkill({ skillId: item.id, arguments: args, idempotencyKey,
      ...(item.requiresAppContext ? { appId:skillAppId.value.trim() } : {}) })
      : kind === 'tool' ? await executeTool({ name: item.name, arguments: args, idempotencyKey })
      : await callMcp({ mcpId: item.id, name: remoteName, arguments: args, idempotencyKey });
    if (response.code !== 200 || response.isSuccess === false) throw new Error(response.message || response.errMsg || '执行未成功');
    runResult.value = response.data;
    const result = response.data;
    const confirmed = result && typeof result === 'object' && result.isError !== true &&
      (result.status === 'success' || (result.status == null &&
        (kind !== 'mcp' || Array.isArray(result.content))));
    if (confirmed) runKeys.delete(fingerprint);
  } catch (failure) { runError.value = `${target.value.kind === 'mcp' ? 'MCP 调用未确认' : '执行未确认'}：${failure.message}。可能已有副作用，请先核对执行记录；不会自动重跑。`; }
  finally { running.value = false; }
}

function editMcp(item) {
  if (!catalog.value.canMcpWrite || savingMcp.value) return;
  Object.assign(mcpForm, { id: item?.id || null, name: item?.name || '', endpoint: item?.endpoint || '', transport: item?.transport || 'http', headersText: '{}',
    enabled:item?.enabled !== false, timeout:item?.timeout ?? 30, autoText:JSON.stringify(item?.chatAutoTools ?? [], null, 2), readOnlyText:JSON.stringify(item?.chatReadOnlyTools ?? [], null, 2) });
  mcpVisible.value = true;
}

async function saveMcp() {
  if (savingMcp.value || !catalog.value.canMcpWrite) return;
  try {
    if (!mcpForm.name.trim() || !mcpForm.endpoint.trim()) throw new Error('请输入名称和 Endpoint');
    const headers = JSON.parse(mcpForm.headersText);
    if (!headers || typeof headers !== 'object' || Array.isArray(headers)) throw new Error('Headers 必须是 JSON 对象');
    if (Object.values(headers).some(value => typeof value !== 'string')) throw new Error('Headers 值必须是字符串');
    if (!Number.isInteger(mcpForm.timeout) || mcpForm.timeout < 1 || mcpForm.timeout > 120) throw new Error('超时必须是 1 至 120 秒的整数');
    const parseNames = (text) => {
      const names = JSON.parse(text);
      if (!Array.isArray(names) || names.length > 64 || names.some(name => typeof name !== 'string' || !name.trim() || name !== name.trim() || name.length > 128) || new Set(names).size !== names.length) throw new Error('工具名单必须是无重复的 JSON 字符串数组，最多 64 项，每项 1 至 128 字符且两端无空格');
      return names;
    };
    const lists = isSuperAdmin.value ? { chatAutoTools:parseNames(mcpForm.autoText), chatReadOnlyTools:parseNames(mcpForm.readOnlyText) } : {};
    savingMcp.value = true;
    const response = await addMcp({ id: mcpForm.id, name: mcpForm.name.trim(), endpoint: mcpForm.endpoint.trim(), transport: mcpForm.transport, headers, enabled:mcpForm.enabled, timeout:mcpForm.timeout, ...lists });
    if (response?.code !== 200 || response.isSuccess === false || response.data !== true) throw new Error('MCP 配置保存未确认');
    mcpVisible.value = false;
    await refresh();
  } catch (failure) { message.error(failure.message); }
  finally { savingMcp.value = false; }
}

async function removeMcp(item) {
  try { await deleteMcp(item.id); await refresh(); }
  catch (failure) { error.value = failure.message; }
}

watch(() => props.type, () => { if (!running.value) runVisible.value = false; if (!savingMcp.value) mcpVisible.value = false; refresh(); });
onMounted(refresh);
onBeforeUnmount(() => { refreshVersion += 1; });
defineExpose({ refresh, editMcp });
</script>

<style scoped>
.execution-section { margin-bottom: 24px; }
.execution-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin-bottom: 12px; }
.execution-row { display: flex; align-items: center; gap: 12px; padding: 14px 0; border-bottom: 1px solid var(--border-color, #e5e7eb); }
.execution-summary { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; overflow-wrap: anywhere; }
.execution-summary span { font-size: var(--eic-font-meta); color: var(--eic-text-secondary); }
.run-feedback { margin-top: 16px; }
pre { max-height: 360px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 12px; padding: 12px; background: var(--eic-surface-muted); border-radius: 6px; font-size:var(--eic-font-code); }
@media (max-width: 600px) { .execution-row { flex-wrap: wrap; gap: 8px; } .execution-summary { flex-basis: 100%; } }
</style>
