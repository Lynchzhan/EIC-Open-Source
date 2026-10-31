<template>
  <e-modal
    v-model:open="visible"
    :title="modalTitle"
    wrap-class-name="agent-editor"
    @ok="handleOk"
    @cancel="handleCancel"
    :confirm-loading="confirmLoading"
    :closable="!confirmLoading"
    :mask-closable="!confirmLoading"
    :keyboard="!confirmLoading"
    :cancel-button-props="{ disabled: confirmLoading }"
    :ok-button-props="{ disabled: !optionsReady || isViewMode }"
    width="900px"
    :footer="isViewMode ? null : undefined"
  >
    <e-alert v-if="optionError" type="error" show-icon :message="optionError">
      <template #action><e-button :loading="optionsLoading" @click="loadOptions">重试配置选项</e-button></template>
    </e-alert>
    <p v-if="optionsLoading" role="status">正在加载模型、提示词、知识库与工具配置…</p>
    <e-alert v-if="saveError" type="error" show-icon :message="saveError" />
    <e-tabs v-model:value="mainTabKey">
      <e-tab-pane key="info" name="info" tab="智能体信息">
        <e-form :model="form" :label-col="{ span: 6 }" :wrapper-col="{ span: 18 }">
      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="名称" v-bind="validateInfos.name">
            <e-input v-model:value="form.name" placeholder="请输入名称" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="类型" v-bind="validateInfos.type">
            <e-input v-model:value="form.type" placeholder="请输入类型" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
      </e-row> 
      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="描述" v-bind="validateInfos.describe">
        <e-textarea v-model:value="form.describe" :rows="2" placeholder="请输入描述" :disabled="isViewMode" />
      </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="提示词" v-bind="validateInfos.aiPromptIDs">
            <e-select
              v-model:value="form.aiPromptIDs"
              mode="multiple"
              placeholder="请选择一个或多个提示词"
              :options="promptOptions"
              allow-clear
              show-search
              optionFilterProp="label"
              :disabled="isViewMode"
            />
          </e-form-item>
        </e-col>
      </e-row>
      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="模型" v-bind="validateInfos.chatModelID">
            <e-select
              v-model:value="form.chatModelID"
              placeholder="请选择会话模型"
              allow-clear
              show-search
              optionFilterProp="label"
              :disabled="isViewMode"
            >
              <e-select-option :value="'auto'">
                🤖 Auto (自动切换)
              </e-select-option>
              <e-select-option 
                v-for="model in modelList" 
                :key="model.id" 
                :value="model.id"
              >
                {{ model.modelDescription || model.name || model.modelName || model.id }}
              </e-select-option>
            </e-select>
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="知识库" v-bind="validateInfos.kmsIds">
            <e-select
              v-model:value="form.kmsIds"
              mode="multiple"
              placeholder="请选择一个或多个知识库"
              :options="kmsOptions"
              allow-clear
              show-search
              optionFilterProp="label"
              :disabled="isViewMode"
            />
          </e-form-item>
        </e-col>
      </e-row> 
      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="温度" v-bind="validateInfos.temperature">
            <div class="slider-container">
              <e-slider
                v-model:value="form.temperature"
                :min="0"
                :max="100"
                :step="1"
                :disabled="isViewMode"
              />
              <div class="slider-value">{{ form.temperature }}°</div>
            </div>
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="相似度" v-bind="validateInfos.relevance">
            <div class="slider-container">
              <e-slider
                v-model:value="form.relevance"
                :min="0"
                :max="100"
                :step="1"
                :disabled="isViewMode"
              />
              <div class="slider-value">{{ form.relevance }}%</div>
            </div>
          </e-form-item>
        </e-col>
      </e-row>
      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="向量匹配数" v-bind="validateInfos.maxMatchesCount">
            <e-input-number
              v-model:value="form.maxMatchesCount"
              :min="0"
              style="width: 100%"
              placeholder="请输入匹配数量"
              :disabled="isViewMode"
            />
          </e-form-item>
        </e-col> 
          <e-col :span="12">
          <e-form-item label="消息输出类型" v-bind="validateInfos.msgType">
            <e-select v-model:value="form.msgType" placeholder="消息输出类型" :disabled="isViewMode">
                <e-select-option   :value="1">非流式文本</e-select-option>
                <e-select-option  :value="2">流式文本</e-select-option>
              </e-select>
          </e-form-item>
        </e-col>
      </e-row>

      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="AI请求日志">
            <e-switch v-model:checked="form.isHttpLog" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="最大重试次数">
            <e-input-number v-model:value="form.maxRetries" :min="0" style="width: 100%" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
      </e-row>
      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="对话消息限制">
            <e-input-number v-model:value="form.chatMessageLimit" :min="1" style="width: 100%" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item
            label="内容长度限制"
            extra="超过限制后会进行截断（知识库、互联网搜索、AI工具内容、文件内容等）"
          >
            <e-input-number v-model:value="form.contentLengthLimit" :min="1" style="width: 100%" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
      </e-row>
      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="流式AI思考过程">
            <e-switch v-model:checked="form.isThinkingLog" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="流式AI工具调用">
            <e-switch v-model:checked="form.isToolLog" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
      </e-row>
      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="安全拦截" extra="开启后会对python脚本和命令进行安全拦截">
            <e-switch v-model:checked="form.isSecurityIntercept" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
         <e-col :span="12">
          <e-form-item label="返回格式">
            <e-select v-model:value="form.responseFormat" placeholder="默认不配置" :disabled="isViewMode" allow-clear>
              <e-select-option :value="''">不配置</e-select-option>
              <e-select-option value="Json">Json</e-select-option>
              <e-select-option value="Text">Text</e-select-option>
            </e-select>
          </e-form-item>
        </e-col>
      </e-row>
       <e-row :gutter="16"> 
        <e-col :span="12">
          <e-form-item label="思考能力">
            <e-select v-model:value="form.reasoningEffort" placeholder="默认不配置" :disabled="isViewMode" allow-clear>
              <e-select-option :value="undefined">不配置</e-select-option>
              <e-select-option :value="0">0.None</e-select-option>
              <e-select-option :value="1">1.Low</e-select-option>
              <e-select-option :value="2">2.Medium</e-select-option>
              <e-select-option :value="3">3.High</e-select-option>
              <e-select-option :value="4">4.ExtraHigh</e-select-option>
            </e-select>
          </e-form-item>
        </e-col>
        <e-col :span="12">
          <e-form-item label="思考输出">
            <e-select v-model:value="form.reasoningOutput" placeholder="默认不配置" :disabled="isViewMode" allow-clear>
              <e-select-option :value="undefined">不配置</e-select-option>
              <e-select-option :value="0">0.None</e-select-option>
              <e-select-option :value="1">1.Summary</e-select-option>
              <e-select-option :value="2">2.Full</e-select-option>
            </e-select>
          </e-form-item>
        </e-col>
      </e-row> 
      <e-row :gutter="16">
        <e-col :span="12">
          <e-form-item label="请求超时(分钟)">
            <e-input-number v-model:value="form.networkTimeout" :min="1" style="width: 100%" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
            <e-col :span="12">
          <e-form-item label="请求白名单">
             <e-textarea v-model:value="form.authorizedDomains" placeholder="*为所有，逗号分隔多个域名前缀" :rows="3" :maxlength="300" :disabled="isViewMode" />
          </e-form-item>
        </e-col>
      </e-row>
 
        </e-form>
      </e-tab-pane>
      <e-tab-pane key="skillTool" name="skillTool" tab="技能工具">
        <e-row :gutter="16">
          <e-col :span="6">
            <e-form-item label="AI工具">
              <e-switch v-model:checked="form.isAITools" :disabled="isViewMode" />
            </e-form-item>
          </e-col>
          <e-col :span="6">
            <e-form-item label="Skill技能">
              <e-switch v-model:checked="form.isSkill" :disabled="isViewMode" />
            </e-form-item>
          </e-col>
          <e-col :span="6">
            <e-form-item label="Mcp工具">
              <e-switch v-model:checked="form.isMcp" :disabled="isViewMode" />
            </e-form-item>
          </e-col>
          <e-col :span="6">
            <e-form-item label="智能体记忆">
              <e-tooltip title="控制长期记忆的读取与保存；不影响当前会话的短期上下文。默认关闭。">
                <e-switch :checked="form.memorySettings.mode !== 'off'" :disabled="isViewMode"
                  @change="enabled => form.memorySettings.mode = enabled ? 'manual' : 'off'" />
              </e-tooltip>
            </e-form-item>
          </e-col>
        </e-row>
        <e-tabs v-model:value="skillToolTabKey">
          <e-tab-pane key="tools" name="tools" tab="Tools工具" :disabled="!form.isAITools">
            <e-table
              :columns="skillToolColumns"
              :data-source="toolsList"
              :row-selection="toolsRowSelection"
              :pagination="false"
              :scroll="{ y: 250 }"
              row-key="id"
              size="small"
            />
          </e-tab-pane>
          <e-tab-pane key="skills" name="skills" tab="Skills技能" :disabled="!form.isSkill">
            <e-table
              :columns="skillToolColumns"
              :data-source="skillsList"
              :row-selection="skillsRowSelection"
              :pagination="false"
              :scroll="{ y: 250 }"
              row-key="id"
              size="small"
            />
          </e-tab-pane>
          <e-tab-pane key="mcps" name="mcps" tab="Mcp工具" :disabled="!form.isMcp">
            <e-table
              :columns="skillToolColumns"
              :data-source="mcpList"
              :row-selection="mcpsRowSelection"
              :pagination="false"
              :scroll="{ y: 250 }"
              row-key="id"
              size="small"
            />
          </e-tab-pane>
        </e-tabs>
      </e-tab-pane>
      <e-tab-pane key="bind" name="bind" tab="用户/角色/智能体绑定" v-if="!props.hideBindTab">
        <e-tabs v-model:value="bindTabKey">
          <e-tab-pane key="users" name="users" tab="绑定用户">
            <div style="margin-bottom: 8px;">
              <e-input-search
                v-model:value="userSearchValue"
                placeholder="搜索用户名或账号"
                style="width: 250px;"
                @search="handleUserSearch"
                allowClear
              />
            </div>
          <e-table
            :columns="userColumns"
            :data-source="userList"
            :pagination="userPagination"
            :row-selection="userRowSelection"
            :loading="userLoading"
            :scroll="{ y: 300 }"
            row-key="id"
            size="small"
            @change="handleUserTableChange"
          />
        </e-tab-pane>
        <e-tab-pane key="roles" name="roles" tab="绑定角色">
          <div style="margin-bottom: 8px;">
            <e-input-search
              v-model:value="roleSearchValue"
              placeholder="搜索角色名称"
              style="width: 250px;"
              @search="handleRoleSearch"
              allowClear
            />
          </div>
          <e-table
            :columns="roleColumns"
            :data-source="roleList"
            :pagination="rolePagination"
            :row-selection="roleRowSelection"
            :loading="roleLoading"
            :scroll="{ y: 300 }"
            row-key="id"
            size="small"
            @change="handleRoleTableChange"
          />
        </e-tab-pane>
        <e-tab-pane key="agents" name="agents" tab="绑定智能体">
          <div style="margin-bottom: 8px;">
            <e-input-search
              v-model:value="agentSearchValue"
              placeholder="搜索智能体名称"
              style="width: 250px;"
              @search="handleAgentSearch"
              allowClear
            />
          </div>
          <e-table
            :columns="agentColumns"
            :data-source="agentList"
            :pagination="agentPagination"
            :row-selection="agentRowSelection"
            :loading="agentLoading"
            :scroll="{ y: 300 }"
            row-key="id"
            size="small"
            @change="handleAgentTableChange"
          />
        </e-tab-pane>
      </e-tabs>
      <div class="selected-summary" v-if="selectedBindCount > 0">
        已选择 {{ selectedBindCount }} 个用户/角色/智能体
      </div>
      </e-tab-pane>
      <e-tab-pane key="compaction" name="compaction" tab="压缩策略">
        <AgentContextEditor v-model:settings="form.contextSettings" v-model:short-term-enabled="form.shortTermEnabled"
          v-model:summary-model-id="form.summaryModelID" :disabled="isViewMode" :models-ready="directoryReady.models"
          :chat-options="summaryModelOptions" :default-summary-prompt="defaultSummaryPrompt" />
      </e-tab-pane>
      <e-tab-pane key="long-memory" name="long-memory" tab="长期记忆">
        <AgentMemoryEditor v-model="form.memorySettings" :disabled="isViewMode" :models-ready="directoryReady.models"
          :embedding-options="memoryEmbeddingOptions" :embedding-ready="memoryEmbeddingReady" :chat-options="summaryModelOptions" />
      </e-tab-pane>
      <e-tab-pane key="runtime-policy" name="runtime-policy" tab="运行限制与降级策略">
        <RunPolicyEditor v-model="form.runtimePolicy" :models="modelList" :disabled="isViewMode" />
      </e-tab-pane>
    </e-tabs>
  </e-modal>
</template>

<script setup>
/* eslint-disable no-undef */
import { ref, reactive, computed, watch, onMounted } from 'vue';
import RunPolicyEditor from './RunPolicyEditor.vue';
import AgentContextEditor from './AgentContextEditor.vue';
import AgentMemoryEditor from './AgentMemoryEditor.vue';
import { Form } from '@/app/ui/controls';
import { getAIModelsALLList } from '@/api/ai/aiModels';
import { getAIPromptsALLList } from '@/api/ai/aiPrompts';
import { getAIKmssList, getAIKmssALLList } from '@/api/ai/aikmss';
import { getUserList } from '@/api/userapi';
import { getRolePage } from '@/api/roleapi';
import { GetAllTools, GetAllSkills, GetAllMcps } from '@/api/ai/aiskilltoolManagement';
import { getAIAppsALLList } from '@/api/ai/aiapps';
import { agentSavePayload, agentSelectedBindings, agentBindingId } from '@/shared/ui/agentWorkspace';
const emit = defineEmits(['ok', 'cancel']);

const props = defineProps({
  open: {
    type: Boolean,
    default: false
  },
  agentData: {
    type: Object,
    default: () => ({})
  },
  modalType: {
    type: String,
    default: 'add'
  },
  hideBindTab: {
    type: Boolean,
    default: false
  },
  submit: {
    type: Function,
    default: null
  }
});

const useForm = Form.useForm;

const visible = ref(false);
const confirmLoading = ref(false);
const optionsReady = ref(false);
const optionsLoading = ref(false);
const optionError = ref('');
const saveError = ref('');

const defaultSummaryPrompt = '请按六段输出会话摘要：当前目标、当前状态、已确认决策、未完成事项、关键证据与引用、待确认冲突。无对应信息写“无”。保留关键约束、数值、单位、ID、路径、失败原因和来源引用，区分事实与待确认信息；不得编造，不输出内部思维链，不强制压缩比例。';
const defaultContextSettings = () => ({
  mode: 'trim', automatic: true, keepTurns: 6, triggerPercent: 60,
  summaryModelId: null, maxSummaryChars: 12000, historyMaxMessages: 1000, summaryPrompt: ''
});
const boundedInteger = (value, fallback, min, max) => {
  if (value === null || value === undefined || value === '') return fallback;
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(max, Math.max(min, Math.trunc(number))) : fallback;
};
const loadContextSettings = (data) => {
  const saved = data.contextSettings && typeof data.contextSettings === 'object' && !Array.isArray(data.contextSettings)
    ? data.contextSettings : {};
  const defaults = defaultContextSettings();
  return {
    ...defaults, ...saved,
    mode: ['trim', 'summary', 'retrieval'].includes(saved.mode) ? saved.mode
      : data.isAIMessageCompaction === true ? 'summary' : 'trim',
    automatic: typeof saved.automatic === 'boolean' ? saved.automatic
      : typeof data.isAutoGetAIMessageCompaction === 'boolean' ? data.isAutoGetAIMessageCompaction : defaults.automatic,
    keepTurns: boundedInteger(saved.keepTurns ?? data.conversationTurnsExceed, 6, 4, 20),
    triggerPercent: boundedInteger(saved.triggerPercent, 60, 50, 90),
    summaryPrompt: typeof saved.summaryPrompt === 'string' ? saved.summaryPrompt
      : typeof data.aiMessageCompactionPrompt === 'string' ? data.aiMessageCompactionPrompt : ''
  };
};
const memoryIsEnabled = (data) => {
  if (['off', 'manual', 'review'].includes(data.memorySettings?.mode)) return data.memorySettings.mode !== 'off';
  if (data.memoryEnabled === false || data.isMemory === false) return false;
  return data.memoryEnabled === true || data.isMemory === true;
};
const defaultMemorySettings = () => ({ mode:'off', embeddingModelId:null, extractionModelId:null,
  extractionMaxCandidates:5, maxMemoryChars:2000, maxActiveMemories:1000,
  maxPendingCandidates:100, retentionDays:180, experienceEnabled:false });

// 表单数据
const form = reactive({
  id: '',
  name: '',
  describe: '',
  icon: 'windows',
  type: '',
  chatModelID: undefined,
  runtimePolicy: null,
  kmsId: undefined,
  kmsIds: [],
  embeddingModelID: undefined,
  rerankModelID: '',
  imageModelID: '',
  temperature: 50,
  relevance: 50,
  maxMatchesCount: 3,
  rerankCount: 20,
  aiPromptID: undefined,
  aiPromptIDs: [],
  msgType: 1,
  isAITools: true,
  isSkill: true,
  isMcp: true,
  isMemory: false,
  memorySettings: defaultMemorySettings(),
  shortTermEnabled: true,
  contextSettings: defaultContextSettings(),
  summaryModelID: undefined,
  tools: [],
  skills: [],
  mcps: [],
  isHttpLog: false,
  maxRetries: 2,
  networkTimeout: 30,
  authorizedDomains: '*',
  bindIds: [],
  chatMessageLimit: 100,
  contentLengthLimit: 50000,
  isThinkingLog: true,
  isToolLog: true,
  isSecurityIntercept: true,
  responseFormat: '',
  reasoningEffort: undefined,
  reasoningOutput: undefined,
  conversationTurnsExceed: 6,
  isAIMessageCompaction: false,
  isAutoGetAIMessageCompaction: true,
  aiMessageCompactionPrompt: defaultSummaryPrompt
});

// 表单验证规则
const rules = reactive({
  name: [
    { required: true, message: '请输入名称' }
  ],
  describe: [
    { required: true, message: '请输入描述' }
  ],
  icon: [
    { required: true, message: '请输入图标' }
  ],
  type: [
    { required: true, message: '请输入类型' }
  ],
  aiPromptIDs: [
    { required: true, type: 'array', min: 1, message: '请选择提示词' }
  ],
  chatModelID: [
    { required: true, message: '请选择会话模型' }
  ] , msgType: [
    { required: true, message: '请选择消息输出类型' }
  ]
});

// 表单验证
const { validate, validateInfos, resetFields } = useForm(form, rules);

// 模型列表
const modelList = ref([]);
const memoryEmbeddingModels = ref([]);
const memoryEmbeddingReady = ref(false);
const memoryEmbeddingOptions = computed(() => memoryEmbeddingModels.value.map(model => ({
  value: String(model.id), label: model.modelDescription || model.name || model.modelName || String(model.id)
})));
const promptList = ref([]);
const kmsList = ref([]); // 知识库列表
const userList = ref([]);
const roleList = ref([]);
const agentList = ref([]);

// 绑定Tab
const bindTabKey = ref('users');
const mainTabKey = ref('info');
const skillToolTabKey = ref('tools');

// 技能工具表格列
const skillToolColumns = [
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '描述', dataIndex: 'description', key: 'description' }
];

// 工具列表
const toolsList = ref([]);
// 技能列表
const skillsList = ref([]);
// Mcp列表
const mcpList = ref([]);

const directoryGroups = {
  models: ['chatModelID', 'embeddingModelID', 'rerankModelID', 'imageModelID', 'summaryModelID'],
  prompts: ['aiPromptID', 'aiPromptIDs'],
  knowledge: ['kmsId', 'kmsIds'],
  tools: ['isAITools', 'tools'],
  skills: ['isSkill', 'skills'],
  mcps: ['isMcp', 'mcps']
};
const directoryReady = reactive({});
const directoryInitialized = {};
const directoryDirty = {};
let initializingDirectories = false;
let directoryBaseline = {};
const directoryValue = (name) => JSON.stringify(directoryGroups[name].map((field) => form[field]));
const captureDirectoryBaseline = () => {
  directoryBaseline = Object.fromEntries(Object.keys(directoryGroups).map((name) => {
    directoryInitialized[name] = false;
    directoryDirty[name] = false;
    return [name, directoryValue(name)];
  }));
};
for (const [name, fields] of Object.entries(directoryGroups)) {
  watch(() => fields.map((field) => form[field]), () => {
    if (props.open && !initializingDirectories) directoryDirty[name] = true;
  }, { deep: true, flush: 'sync' });
}
const preserveUnavailableDirectories = (params) => {
  for (const [name, fields] of Object.entries(directoryGroups)) {
    if (directoryReady[name]) continue;
    if (directoryDirty[name] || directoryValue(name) !== directoryBaseline[name]) {
      throw new Error(`选项目录 ${name} 未加载，不能修改依赖该目录的字段；请重新打开表单重试`);
    }
    // 删除覆盖值，由 agentSavePayload 保留原配置，包括字段缺省状态。
    for (const field of fields) delete params[field];
  }
};

// Tools行选择
const toolsRowSelection = computed(() => {
  return {
    selectedRowKeys: form.tools,
    onChange: isViewMode.value || optionsLoading.value ? () => {} : (selectedRowKeys) => {
      form.tools = [...selectedRowKeys];
    },
    preserveSelectedRows: true
  };
});

// Skills行选择
const skillsRowSelection = computed(() => {
  return {
    selectedRowKeys: form.skills,
    onChange: isViewMode.value || optionsLoading.value ? () => {} : (selectedRowKeys) => {
      form.skills = [...selectedRowKeys];
    },
    preserveSelectedRows: true
  };
});

// Mcps行选择
const mcpsRowSelection = computed(() => {
  return {
    selectedRowKeys: form.mcps,
    onChange: isViewMode.value || optionsLoading.value ? () => {} : (selectedRowKeys) => {
      form.mcps = [...selectedRowKeys];
    },
    preserveSelectedRows: true
  };
});

// 用户搜索状态
const userSearchValue = ref('');

// 角色搜索状态
const roleSearchValue = ref('');

// 智能体搜索状态
const agentSearchValue = ref('');

// 用户表格列
const userColumns = [
  { title: '用户名', dataIndex: 'name', key: 'name' },
  { title: '账号', dataIndex: 'phone', key: 'phone' }
];

// 角色表格列
const roleColumns = [
  { title: '角色名称', dataIndex: 'name', key: 'name' }
];

// 智能体表格列
const agentColumns = [
  { title: '智能体名称', dataIndex: 'name', key: 'name' },
  { title: '类型', dataIndex: 'type', key: 'type' },
  { title: '描述', dataIndex: 'describe', key: 'describe' }
];

// 用户分页
const userPagination = reactive({
  current: 1,
  pageSize: 100,
  total: 0,
  showSizeChanger: true,
  pageSizeOptions: ['100', '500', '1000', '2000'],
  showTotal: (total) => `共 ${total} 条`
});

// 角色分页
const rolePagination = reactive({
  current: 1,
  pageSize: 100,
  total: 0,
  showSizeChanger: true,
  pageSizeOptions: ['100', '500', '1000', '2000'],
  showTotal: (total) => `共 ${total} 条`
});

// 智能体分页
const agentPagination = reactive({
  current: 1,
  pageSize: 100,
  total: 0,
  showSizeChanger: true,
  pageSizeOptions: ['100', '500', '1000', '2000'],
  showTotal: (total) => `共 ${total} 条`
});

const userLoading = ref(false);
const roleLoading = ref(false);
const agentLoading = ref(false);

// 用户行选择
const userRowSelection = computed(() => ({
  selectedRowKeys: form.bindIds
    .filter(id => String(id).startsWith('user_'))
    .map(id => String(id).replace('user_', '')),
  onChange: (selectedRowKeys) => {
    const roleIds = form.bindIds.filter(id => String(id).startsWith('role_'));
    const agentIds = form.bindIds.filter(id => String(id).startsWith('agent_'));
    form.bindIds = [...roleIds, ...agentIds, ...selectedRowKeys.map(id => `user_${id}`)];
  },
  preserveSelectedRows: true
}));

// 角色行选择
const roleRowSelection = computed(() => ({
  selectedRowKeys: form.bindIds
    .filter(id => String(id).startsWith('role_'))
    .map(id => String(id).replace('role_', '')),
  onChange: (selectedRowKeys) => {
    const userIds = form.bindIds.filter(id => String(id).startsWith('user_'));
    const agentIds = form.bindIds.filter(id => String(id).startsWith('agent_'));
    form.bindIds = [...userIds, ...agentIds, ...selectedRowKeys.map(id => `role_${id}`)];
  },
  preserveSelectedRows: true
}));

// 智能体行选择
const agentRowSelection = computed(() => ({
  selectedRowKeys: form.bindIds
    .filter(id => String(id).startsWith('agent_'))
    .map(id => String(id).replace('agent_', '')),
  onChange: (selectedRowKeys) => {
    const userIds = form.bindIds.filter(id => String(id).startsWith('user_'));
    const roleIds = form.bindIds.filter(id => String(id).startsWith('role_'));
    form.bindIds = [...userIds, ...roleIds, ...selectedRowKeys.map(id => `agent_${id}`)];
  },
  preserveSelectedRows: true,
  getCheckboxProps: (record) => ({
    disabled: isViewMode.value || record.id === form.id
  })
}));

// 已选择数量
const selectedBindCount = computed(() => form.bindIds?.length || 0);

// 用户搜索
const handleUserSearch = () => {
  userPagination.current = 1;
  loadUserList();
};

// 角色搜索
const handleRoleSearch = () => {
  rolePagination.current = 1;
  loadRoleList();
};

// 用户表格变化
const handleUserTableChange = (pagination) => {
  userPagination.current = pagination.current;
  userPagination.pageSize = pagination.pageSize;
  loadUserList();
};

// 角色表格变化
const handleRoleTableChange = (pagination) => {
  rolePagination.current = pagination.current;
  rolePagination.pageSize = pagination.pageSize;
  loadRoleList();
};

// 智能体搜索
const handleAgentSearch = () => {
  agentPagination.current = 1;
  loadAgentList();
};

// 智能体表格变化
const handleAgentTableChange = (pagination) => {
  agentPagination.current = pagination.current;
  agentPagination.pageSize = pagination.pageSize;
  loadAgentList();
};

// 模型选项（已通过插槽直接渲染，无需计算属性）

const toIdList = (value) => {
  if (value == null || value === '') return [];
  const list = Array.isArray(value) ? value : [value];
  return [...new Set(list.map((item) => String(item)).filter((item) => item && item !== '0' && item !== 'undefined' && item !== 'null'))];
};

const unwrapList = (response) => {
  if (Array.isArray(response)) return response;
  if (response && Array.isArray(response.data)) return response.data;
  if (response && response.data && Array.isArray(response.data.data)) return response.data.data;
  if (response && response.data && Array.isArray(response.data.list)) return response.data.list;
  return [];
};

// 知识库选项
const kmsOptions = computed(() => {
  if(!kmsList.value || !Array.isArray(kmsList.value)) {
    return [];
  }
  return kmsList.value.map(kms => ({
    label: kms.name,
    value: String(kms.id)
  }));
});

// 提示词选项
const promptOptions = computed(() => {
  return promptList.value.map(prompt => ({
    label: prompt.name,
    value: String(prompt.id)
  }));
});

const summaryModelOptions = computed(() => modelList.value.map((model) => ({
  value: model.id,
  label: model.modelDescription || model.name || model.modelName || String(model.id)
})));


// 加载用户列表
const loadUserList = async () => {
  try {
    userLoading.value = true;
    const response = await getUserList({
      pageNum: userPagination.current,
      pageSize: userPagination.pageSize,
      searchKey: userSearchValue.value || undefined
    });
    if (response && response.code === 200 && response.data) {
      userList.value = response.data.data || response.data.list || [];
      userPagination.total = response.data.total || 0;
    }
  } catch (error) {
    console.error('加载用户列表失败:', error);
  } finally {
    userLoading.value = false;
  }
};

// 加载角色列表
const loadRoleList = async () => {
  try {
    roleLoading.value = true;
    const response = await getRolePage({
      pageNum: rolePagination.current,
      pageSize: rolePagination.pageSize,
      searchKey: roleSearchValue.value || undefined
    });
    if (response && response.code === 200) {
      roleList.value = response.data.data || response.data.list || response.data || [];
      rolePagination.total = response.data.total || 0;
    }
  } catch (error) {
    console.error('加载角色列表失败:', error);
  } finally {
    roleLoading.value = false;
  }
};

// 加载智能体列表
const loadAgentList = async () => {
  try {
    agentLoading.value = true;
    const response = await getAIAppsALLList();
    if (response && response.code === 200 && response.data) {
      let filteredData = response.data;
      if (agentSearchValue.value) {
        filteredData = filteredData.filter(agent => 
          agent.name && agent.name.includes(agentSearchValue.value)
        );
      }
      filteredData = filteredData.filter(agent => agent.id !== form.id);
      agentList.value = filteredData;
      agentPagination.total = filteredData.length;
    }
  } catch (error) {
    console.error('加载智能体列表失败:', error);
  } finally {
    agentLoading.value = false;
  }
};
 
 
// 模态框标题
const modalTitle = computed(() => {
  if (props.modalType === 'view') return '智能体详情';
  return props.modalType === 'edit' ? '编辑智能体' : '添加智能体';
});

const isViewMode = computed(() => props.modalType === 'view');

// 监听open属性变化
const hydrateBindings = (fields = ['tools', 'skills', 'mcps']) => {
  if (!props.open || props.modalType === 'add' || !props.agentData) return;
  const legacy = toIdList(props.agentData.aiSkillsToolsBindIds);
  for (const [field, rows] of [['tools', toolsList.value], ['skills', skillsList.value], ['mcps', mcpList.value]]) {
    if (!directoryReady[field] || !fields.includes(field)) continue;
    const stored = props.agentData[field];
    const selected = Array.isArray(stored)
      ? stored.filter((item) => typeof item !== 'object' || item.isSelect !== false)
        .map(agentBindingId)
      : legacy;
    const selectedIds = new Set(toIdList(selected));
    form[field] = rows.filter((item) => selectedIds.has(String(item.id))).map((item) => item.id);
  }
};
const initializeAvailableDirectories = () => {
  if (!props.open) return;
  for (const name of Object.keys(directoryGroups)) {
    if (!directoryReady[name] || directoryInitialized[name]) continue;
    // 首次成功是唯一初始化机会；用户已编辑时不回填、不移动基线。
    directoryInitialized[name] = true;
    if (directoryDirty[name] || directoryValue(name) !== directoryBaseline[name]) continue;
    initializingDirectories = true;
    try {
      if (['tools', 'skills', 'mcps'].includes(name)) hydrateBindings([name]);
      directoryBaseline[name] = directoryValue(name);
    } finally { initializingDirectories = false; }
  }
};
watch(() => props.open, (newVal) => {
  visible.value = newVal;
  if (newVal) {
    initializingDirectories = true;
    // 重置表单
    resetFields();
    saveError.value = '';

    // 如果是编辑或预览模式，填充表单数据
    if ((props.modalType === 'edit' || props.modalType === 'view') && props.agentData) {
      Object.keys(form).forEach(key => {
        if (['bindIds', 'aiPromptID', 'aiPromptIDs', 'kmsId', 'kmsIds', 'contextSettings', 'summaryModelID', 'memorySettings'].includes(key)) {
          return;
        }
        if (props.agentData[key] !== undefined) {
          form[key] = props.agentData[key];
        }
      });
      form.aiPromptIDs = toIdList(props.agentData.aiPromptIDs || props.agentData.aiPromptID || props.agentData.promptId);
      form.runtimePolicy = props.agentData.runtimePolicy ? JSON.parse(JSON.stringify(props.agentData.runtimePolicy)) : null;
      form.kmsIds = toIdList(props.agentData.kmsIds || props.agentData.kmsId || props.agentData.knowledgeBaseId);
      form.aiPromptID = form.aiPromptIDs[0];
      form.kmsId = form.kmsIds[0];
      form.isMemory = memoryIsEnabled(props.agentData);
      form.memorySettings = { ...defaultMemorySettings(),
        embeddingModelId: props.agentData.memoryEmbeddingModelId ?? null,
        ...(props.agentData.memorySettings || {}),
        mode: props.agentData.memorySettings?.mode || (form.isMemory ? 'manual' : 'off') };
      form.shortTermEnabled = props.agentData.shortTermEnabled !== false;
      form.contextSettings = loadContextSettings(props.agentData);
      form.summaryModelID = form.contextSettings.summaryModelId ?? undefined;

      // bindIds 包含用户、角色和智能体
      form.bindIds = props.agentData.bindIds || [];
    } else {
      // 添加模式，设置默认值
      Object.assign(form, {
        id: props.agentData?.id || '',
        name: '',
        describe: '',
        icon: 'windows',
        type: '',
        chatModelID: undefined,
        runtimePolicy: null,
        embeddingModelID: undefined,
        kmsId: undefined,
        kmsIds: [],
        rerankModelID: '',
        imageModelID: '',
        temperature: 50,
        relevance: 50,
        maxMatchesCount: 3,
        rerankCount: 20,
        aiPromptID: undefined,
        aiPromptIDs: [],
        msgType: 1,
        isAITools: true,
        isSkill: true,
        isMcp: true,
        isMemory: false,
        memorySettings: defaultMemorySettings(),
        shortTermEnabled: true,
        contextSettings: defaultContextSettings(),
        summaryModelID: undefined,
        tools: [],
        skills: [],
        mcps: [],
        isHttpLog: false,
        maxRetries: 2,
        networkTimeout: 30,
        authorizedDomains: '*',
        bindIds: [],
        chatMessageLimit: 100,
        contentLengthLimit: 50000,
        isThinkingLog: true,
        isToolLog: true,
        responseFormat: '',
        reasoningEffort: undefined,
        reasoningOutput: undefined,
        conversationTurnsExceed: 6,
        isAIMessageCompaction: false,
        isAutoGetAIMessageCompaction: true,
        aiMessageCompactionPrompt: defaultSummaryPrompt
      });
    }
    if (props.modalType !== 'add' && props.agentData) {
      try {
        const legacy = toIdList(props.agentData.aiSkillsToolsBindIds);
        for (const field of ['tools', 'skills', 'mcps']) {
          const stored = props.agentData[field];
          form[field] = Array.isArray(stored)
            ? toIdList(stored.filter((item) => typeof item !== 'object' || item?.isSelect !== false).map(agentBindingId))
            : [...legacy];
        }
      } catch (error) { saveError.value = error.message; }
    }
    captureDirectoryBaseline();
    initializingDirectories = false;
    try { initializeAvailableDirectories(); } catch (error) { saveError.value = error.message; }
    loadAgentList();
  }
}, { immediate: true });

// 监听visible变化
watch(visible, (newVal) => {
  if (!newVal) {
    emit('cancel');
  }
});

// 处理确认
const handleOk = () => {
  if (confirmLoading.value || optionsLoading.value || !optionsReady.value || isViewMode.value) return;
  confirmLoading.value = true;
  saveError.value = '';
  validate().then(async () => {
    try {
      for (const field of ['tools', 'skills', 'mcps']) {
        const stored = props.agentData?.[field];
        if (Array.isArray(stored)) stored.forEach(agentBindingId);
      }
      const buildSelectedData = (selectedIds, originalData, field) =>
        directoryReady[field] ? agentSelectedBindings(selectedIds, originalData, props.agentData?.[field])
          : props.agentData?.[field];

      // 构建 bindIds，只包含用户和角色（不包含工具和技能）
      const userRoleBindIds = (form.bindIds || []).filter(id => !String(id).startsWith('tool_') && !String(id).startsWith('skill_'));

      const contextSettings = {
        ...form.contextSettings,
        keepTurns: boundedInteger(form.contextSettings.keepTurns, 6, 4, 20),
        triggerPercent: boundedInteger(form.contextSettings.triggerPercent, 60, 50, 90),
        summaryModelId: form.summaryModelID ?? null
      };
      const memorySettings = { ...form.memorySettings,
        embeddingModelId: form.memorySettings.embeddingModelId ?? null,
        extractionModelId: form.memorySettings.extractionModelId ?? null };
      form.isMemory = memorySettings.mode !== 'off';

      const params = {
        id: form.id,
        name: form.name,
        describe: form.describe,
        icon: form.icon,
        type: form.type,
        chatModelID: form.chatModelID,
        runtimePolicy: form.runtimePolicy,
        kmsId: (form.kmsIds && form.kmsIds[0]) || form.kmsId,
        kmsIds: toIdList(form.kmsIds),
        embeddingModelID: form.embeddingModelID,
        rerankModelID: form.rerankModelID,
        imageModelID: form.imageModelID,
        temperature: form.temperature,
        relevance: form.relevance,
        maxMatchesCount: form.maxMatchesCount,
        rerankCount: form.rerankCount,
        aiPromptID: (form.aiPromptIDs && form.aiPromptIDs[0]) || form.aiPromptID,
        aiPromptIDs: toIdList(form.aiPromptIDs),
        msgType: form.msgType,
        isAITools: form.isAITools,
        isSkill: form.isSkill,
        isMcp: form.isMcp,
        isMemory: form.isMemory,
        memoryEnabled: form.isMemory,
        memorySettings,
        shortTermEnabled: form.shortTermEnabled,
        contextSettings,
        tools: form.isAITools ? buildSelectedData(form.tools || [], toolsList.value, 'tools') : [],
        skills: form.isSkill ? buildSelectedData(form.skills || [], skillsList.value, 'skills') : [],
        mcps: form.isMcp ? buildSelectedData(form.mcps || [], mcpList.value, 'mcps') : [],
        isHttpLog: form.isHttpLog,
        maxRetries: form.maxRetries,
        networkTimeout: form.networkTimeout,
        authorizedDomains: form.authorizedDomains,
        bindIds: userRoleBindIds,
        chatMessageLimit: form.chatMessageLimit,
        contentLengthLimit: form.contentLengthLimit,
        isThinkingLog: form.isThinkingLog,
        isToolLog: form.isToolLog,
        isSecurityIntercept: form.isSecurityIntercept,
        responseFormat: form.responseFormat,
        reasoningEffort: form.reasoningEffort,
        reasoningOutput: form.reasoningOutput,
        conversationTurnsExceed: contextSettings.keepTurns,
        isAIMessageCompaction: contextSettings.mode !== 'trim',
        isAutoGetAIMessageCompaction: contextSettings.automatic,
        aiMessageCompactionPrompt: contextSettings.summaryPrompt
      };
      
      preserveUnavailableDirectories(params);
      // 摘要模型复用模型目录的保护机制，内部表单字段不发送给后端。
      if (!directoryReady.models) {
        params.contextSettings.summaryModelId = form.contextSettings.summaryModelId ?? null;
      }
      delete params.summaryModelID;
      const payload = agentSavePayload(props.modalType === 'edit' ? props.agentData : null, params);
      if (props.submit) await props.submit(payload);
      else emit('ok', payload);
    } catch (error) {
      console.error('保存智能体失败:', error);
      saveError.value = `保存失败：${error.message || '请重试'}`;
    } finally {
      confirmLoading.value = false;
    }
  }).catch(err => {
    console.log('表单验证失败:', err);
    confirmLoading.value = false;
  });
};

// 处理取消
const handleCancel = () => {
  if (confirmLoading.value) return;
  visible.value = false;
};

// 加载模型列表
const loadModelList = async () => {
  try {
    memoryEmbeddingReady.value = false;
    try {
      const embeddings = await getAIModelsALLList(2);
      if (embeddings?.code === 200 && Array.isArray(embeddings.data)) {
        memoryEmbeddingModels.value = embeddings.data;
        memoryEmbeddingReady.value = true;
      }
    } catch { memoryEmbeddingModels.value = []; }
    const response = await getAIModelsALLList(1);
    if (response && response.code === 200 && response.data) {
      modelList.value = response.data;
    } else throw new Error('模型选项响应无效');
  } catch (error) {
    console.error('加载模型列表失败:', error);
    throw error;
  }
};

// 加载提示词列表
const loadPromptList = async () => {
  try {
    const response = await getAIPromptsALLList();
    if (response && response.code === 200 && response.data) {
      promptList.value = response.data;
    } else throw new Error('提示词选项响应无效');
  } catch (error) {
    console.error('加载提示词列表失败:', error);
    throw error;
  }
};

// 加载知识库列表
const loadKmsList = async () => {
  try {
    let rows = [];
    try {
      rows = unwrapList(await getAIKmssALLList());
    } catch (error) {
      rows = [];
    }
    if (!rows.length) {
      rows = unwrapList(await getAIKmssList({ page: 1, limit: 200 }));
    }
    kmsList.value = Array.isArray(rows) ? rows : [];
  } catch (error) {
    console.error('加载知识库列表失败:', error);
    kmsList.value = [];
    throw error;
  }
};

// 加载工具列表
const loadToolsList = async () => {
  try {
    const response = await GetAllTools();
    if (response && response.code === 200 && response.data) {
      toolsList.value = response.data;
    } else throw new Error('工具选项响应无效');
  } catch (error) {
    console.error('加载工具列表失败:', error);
    throw error;
  }
};

// 加载技能列表
const loadSkillsList = async () => {
  try {
    const response = await GetAllSkills();
    if (response && response.code === 200 && response.data) {
      skillsList.value = response.data;
    } else throw new Error('Skill 选项响应无效');
  } catch (error) {
    console.error('加载技能列表失败:', error);
    throw error;
  }
};

// 加载Mcp列表
const loadMcpsList = async () => {
  try {
    const response = await GetAllMcps();
    if (response && response.code === 200 && response.data) {
      mcpList.value = response.data;
    } else throw new Error('MCP 选项响应无效');
  } catch (error) {
    console.error('加载Mcp列表失败:', error);
    throw error;
  }
};

// 组件挂载时加载数据
const loadOptions = async () => {
  if (optionsLoading.value) return;
  optionsLoading.value = true; optionsReady.value = false; optionError.value = '';
  try {
    const names = Object.keys(directoryGroups);
    const results = await Promise.allSettled([loadModelList(), loadPromptList(), loadKmsList(), loadToolsList(), loadSkillsList(), loadMcpsList()]);
    const failures = [];
    results.forEach((result, index) => {
      directoryReady[names[index]] = result.status === 'fulfilled';
      if (result.status === 'rejected') failures.push(names[index]);
    });
    if (failures.length) optionError.value = `以下目录未加载：${failures.join('、')}。未修改的配置会保留；修改依赖字段时不能保存，请重新打开表单重试。`;
    try { initializeAvailableDirectories(); }
    catch (error) { saveError.value = error.message; }
    optionsReady.value = true;
  }
  finally { optionsLoading.value = false; }
};
onMounted(() => {
  loadOptions();
  loadUserList();
  loadRoleList();
  loadAgentList();
});

// 暴露方法给父组件
defineExpose({
  resetFields
});
</script>

<style scoped>
.checkbox-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  max-height: 250px;
  overflow-y: auto;
  padding: 4px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 4px;
}

.slider-container {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.slider-value {
  text-align: center;
  font-size: var(--eic-font-meta);
  color: var(--eic-muted, #64748b);
}
.selected-summary {
  margin-top: 8px;
  padding: 8px;
  background: var(--eic-line);
  border-radius: 4px;
  font-size: var(--eic-font-meta);
  color: var(--eic-primary);
}
</style>
<style>
@media (max-width: 767px) {
}
</style>
