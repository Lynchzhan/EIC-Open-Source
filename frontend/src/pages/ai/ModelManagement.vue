<template>
  <div class="management-container">
    <e-card class="management-card">
      <template #title>
        <div class="card-header">
          <div class="header-actions">
            <e-input-search
              class="search-input"
              placeholder="搜索模型..."
              style="width: 250px; margin-right: 16px"
              @search="onSearch"
            />
            <e-button type="primary" class="add-button" @click="showAddModelModal">
              <template #icon>
                <PlusOutlined />
              </template>
              添加模型
            </e-button>
            <e-button v-if="!experienceContext" @click="modelTestPanel?.openSettings()">自定义测试</e-button>
          </div>
        </div>
      </template>

      <TenantModelDefaults />
      <ConversationLabelModel v-if="!experienceContext" />
      <div class="cards-container">
        <e-row :gutter="[24, 24]">
          <e-col :xs="24" :sm="12" :md="12" :lg="8" :xl="6" v-for="model in modelList" :key="model.id">
            <e-card class="model-card" hoverable>
              <template #title>
                <div class="card-title">
                  <span class="model-name">{{ model.modelName }}</span>
                </div>
              </template>
              <template #extra>
                <e-dropdown>
                  <button type="button" class="eic-menu-trigger" aria-label="配置显示列">
                    <EllipsisOutlined />
                  </button>
                  <template #overlay>
                    <e-menu>
                      <e-menu-item @click="showEditModelModal(model)">
                        <edit-outlined /> 编辑
                      </e-menu-item>
                      <e-menu-item @click="showDeleteConfirm(model)">
                        <delete-outlined /> 删除
                      </e-menu-item>
                    </e-menu>
                  </template>
                </e-dropdown>
              </template>
              <div class="card-content">
                <div @click="showEditModelModal(model)">
                <div class="model-info horizontal-layout">
                  <div class="info-item horizontal">
                    <span class="info-label">AI类型:</span>
                    <span class="info-value">{{ getAITypeName(model.aiType) }}</span>
                  </div>
                  <div class="info-item horizontal">
                    <span class="info-label">模型类型:</span>
                    <span class="info-value">{{ getModelTypeName(model.aiModelType) }}</span>
                  </div>
                </div>
                <div class="model-info horizontal-layout">
                  <div class="info-item horizontal">
                    <span class="info-label">模型地址:</span>
                    <span class="info-value url-value">{{ model.endPoint }}</span>
                  </div>
                  <div class="info-item horizontal">
                    <span class="info-label">部署名:</span>
                    <span class="info-value">{{ model.modelDescription }}</span>
                  </div>
                </div>
                <div class="model-info horizontal-layout" v-if="model.aiModelType === 2">
                  <div class="info-item horizontal">
                    <span class="info-label">矢量精度:</span>
                    <span class="info-value">{{ model.embeddingValueSize }}</span>
                  </div>
                </div>
                <div class="model-info horizontal-layout" v-if="model.createUser || model.updateUser">
                  <div class="info-item horizontal">
                    <span class="info-label" v-if="model.createUser">创建人:</span>
                    <span class="info-value" v-if="model.createUser">{{ model.createUser }}</span>
                    <span class="info-label" v-if="model.updateUser" style="margin-left: 16px;">更新人:</span>
                    <span class="info-value" v-if="model.updateUser">{{ model.updateUser }}</span>
                  </div>
                </div>
                </div>
                <div class="card-actions" @click.stop>
                  <e-button size="small" type="primary" class="quick-test-btn" :loading="quickTestingId === model.id" :disabled="testingConnection || (quickTestingId != null && quickTestingId !== model.id)" @click="handleQuickTest(model)">一键测试</e-button>
                </div>
              </div>
            </e-card>
          </e-col>
        </e-row>
        
        <e-empty v-if="modelList.length === 0" description="暂无模型配置数据" />
        
        <div class="pagination-container" v-if="modelList.length > 0">
          <e-pagination
            v-model:current="pagination.current"
            v-model:page-size="pagination.pageSize"
            :total="pagination.total"
            show-size-changer
            show-quick-jumper
            :show-total="(total) => `共 ${total} 条记录`"
            @change="handlePageChange"
          />
        </div>
      </div>
    </e-card>

    <!-- 添加/编辑模型配置模态框 -->
    <e-modal 
      v-model:open="modelModalVisible" 
      :title="modelModalTitle" 
      ok-text="确定"
      cancel-text="取消"
      @ok="handleModelModalOk" 
      @cancel="handleModelModalCancel"
      :confirm-loading="confirmLoading"
      width="600px"
    >
      <template #footer>
        <e-button @click="handleModelModalCancel">取消</e-button>
        <e-button @click="handleTestConnection" :loading="testingConnection" :disabled="quickTestingId != null">测试连接</e-button>
        <e-button type="primary" @click="handleModelModalOk" :loading="confirmLoading">确定</e-button>
      </template>
      <e-form :model="modelForm" :label-col="{ span: 6 }" :wrapper-col="{ span: 18 }"> 
       <e-form-item label="模型类型" v-bind="validateInfos.aiModelType">
              <e-select v-model:value="modelForm.aiModelType" placeholder="请选择模型类型">
                <e-select-option :value="1">Chat</e-select-option>
                <e-select-option :value="2">Embedding</e-select-option>
                <e-select-option :value="4">Rerank</e-select-option>
              </e-select>
            </e-form-item>
              <e-form-item label="AI类型" v-bind="validateInfos.aiType">
              <e-select v-model:value="modelForm.aiType" placeholder="请选择AI类型">
                <e-select-option :value="1">OpenAI Chat Completions（兼容接口）</e-select-option>
                <e-select-option v-if="modelForm.aiModelType===1" value="openai_responses">OpenAI Responses</e-select-option>
                <e-select-option v-if="modelForm.aiModelType===1" value="anthropic">Anthropic Messages</e-select-option>
                <e-select-option v-if="modelForm.aiModelType===1 || modelForm.aiModelType===2" :value="2">Azure</e-select-option>
                <e-select-option v-if="modelForm.aiModelType===1 || modelForm.aiModelType===2" :value="3">智谱AI</e-select-option>
                <e-select-option v-if="modelForm.aiModelType===2" :value="7">Bge Embedding</e-select-option>
                <e-select-option v-if="modelForm.aiModelType===4" :value="8">Bge Rerank</e-select-option>
                <e-select-option v-if="modelForm.aiModelType===1" :value="10">Ollama</e-select-option>
                <e-select-option v-if="modelForm.aiModelType===2" :value="11">OllamaEmbedding</e-select-option>
                <e-select-option v-if="modelForm.aiModelType===4" :value="12">Ali Rerank</e-select-option>
              </e-select>
            </e-form-item>
         <e-form-item label="模型地址" v-bind="validateInfos.endPoint">
          <e-input v-model:value="modelForm.endPoint" placeholder="请输入模型地址" />
        </e-form-item>
        <p v-if="modelForm.aiModelType === 4" class="form-hint">可临时使用与 Chat 相同的 OpenAI 兼容模型登记为 Rerank。若上游没有 /rerank 接口，系统会按对话接口探测连通性，检索时自动回退词法重排。</p> 
        <e-form-item label="模型名称" v-bind="validateInfos.modelName">
          <n-select v-model:value="modelForm.modelName" filterable tag :options="upstreamModels" placeholder="选择或手动输入模型名称" />
          <e-button :loading="fetchingModels" @click="loadUpstreamModels">获取上游模型</e-button>
        </e-form-item>
        <e-form-item label="模型秘钥">
          <n-input :value="displaySecret" :type="savedSecretVisible ? 'text' : 'password'" :placeholder="currentModel?.secretConfigured ? '已保存秘钥；留空保留，点击眼睛查看' : '请输入模型秘钥'" autocomplete="off" @update:value="updateSecret">
            <template #suffix><button type="button" class="secret-toggle" :disabled="revealingSecret" :aria-label="savedSecretVisible ? '隐藏秘钥' : '查看秘钥'" :aria-pressed="savedSecretVisible" @click="toggleSavedSecret"><EyeOutlined /></button></template>
          </n-input>
        </e-form-item>
        <p class="form-hint">Chat Completions 使用 /chat/completions，Responses 使用 /responses，Messages 使用 /messages。未修改秘钥时保留原值。</p>
        <section v-if="!currentModel" class="model-recovery">
          <n-checkbox v-model:checked="setAsDefault">设为此类默认模型</n-checkbox>
          <p v-if="pendingReferences.length">选择恢复之前清除的引用（向量模型恢复后需重新入库）：</p>
          <n-checkbox-group v-model:value="restoreReferenceIds">
            <n-checkbox v-for="item in pendingReferences" :key="item.id" :value="item.id">{{ item.name }} · {{ item.location }}</n-checkbox>
          </n-checkbox-group>
        </section>
        <e-form-item label="部署名" v-bind="validateInfos.modelDescription">
          <e-input v-model:value="modelForm.modelDescription" placeholder="请输入部署名" />
        </e-form-item>
        <e-form-item v-if="modelForm.aiModelType === 2" label="矢量精度" v-bind="validateInfos.embeddingValueSize">
          <e-input-number v-model:value="modelForm.embeddingValueSize" :min="1" :default-value="2048" placeholder="请输入矢量精度" style="width: 100%" />
        </e-form-item>
        <e-form-item v-if="modelForm.aiModelType === 1" label="提问Token" v-bind="validateInfos.maxAskPromptSize">
          <div style="display: flex; align-items: center; gap: 8px">
            <e-input-number v-model:value="modelForm.maxAskPromptSize" :min="1" :max="2000000" style="flex: 1" placeholder="请输入输入预算" />
            <e-tag
              v-for="opt in askTokenOptions"
              :key="opt.value"
              :color="modelForm.maxAskPromptSize === opt.value ? 'blue' : 'default'"
              style="cursor: pointer; margin: 0"
              @click="modelForm.maxAskPromptSize = opt.value"
            >{{ opt.label }}</e-tag>
          </div>
        </e-form-item>
        <e-form-item v-if="modelForm.aiModelType === 1" label="回答Token" v-bind="validateInfos.answerTokens">
          <div style="display: flex; align-items: center; gap: 8px">
            <e-input-number v-model:value="modelForm.answerTokens" :min="1" :max="2000000" style="flex: 1" placeholder="请输入回答最大Token数" />
            <e-tag
              v-for="opt in answerTokenOptions"
              :key="opt.value"
              :color="modelForm.answerTokens === opt.value ? 'blue' : 'default'"
              style="cursor: pointer; margin: 0"
              @click="modelForm.answerTokens = opt.value"
            >{{ opt.label }}</e-tag>
          </div>
        </e-form-item>
        <e-form-item v-if="modelForm.aiModelType === 1" label="参数协议">
          <e-select v-model:value="modelForm.chatParameterProfile">
            <e-select-option value="auto">自动映射（部署别名请显式选择）</e-select-option>
            <e-select-option value="openai_legacy">OpenAI 兼容：max_tokens + temperature</e-select-option>
            <e-select-option value="openai_completion">OpenAI 兼容：max_completion_tokens + temperature</e-select-option>
            <e-select-option value="openai_reasoning">推理模型：max_completion_tokens，不发送温度</e-select-option>
            <e-select-option value="ollama">Ollama：num_predict + num_ctx + temperature</e-select-option>
          </e-select>
        </e-form-item>
        <e-form-item v-if="modelForm.aiModelType === 1" label="联网搜索模式">
          <e-select v-model:value="modelForm.searchMode">
            <e-select-option value="disabled">关闭</e-select-option>
            <e-select-option value="independent">独立搜索（SearXNG / Tavily）</e-select-option>
            <e-select-option value="native" :disabled="modelForm.aiType !== 'openai_responses'">模型原生联网（Responses web_search）</e-select-option>
          </e-select>
        </e-form-item>
        <p v-if="modelForm.aiModelType === 1" class="form-hint">输入按 UTF-8 字节保守估算，附件和检索材料计入预算。图片仅 OCR 文字识别。独立搜索需要部署配置，原生搜索需要上游支持 web_search；保存配置不代表上游已验证。</p>
        <ModelReasoningFields v-if="modelForm.aiModelType === 1" v-model="modelForm.reasoningPolicy" :provider="modelForm.aiType" :output-limit="modelForm.answerTokens" />
      </e-form>
    </e-modal>
    <n-modal v-model:show="deleteVisible" preset="card" title="删除模型及处理引用" style="width:min(640px,calc(100vw - 32px))" :mask-closable="!deleting" :closable="!deleting">
      <p>删除 {{ deletingModel?.modelName }}，将影响以下配置：</p>
      <ul><li v-for="item in deleteImpact.references" :key="item.key">{{ item.name }} · {{ item.location }}</li></ul>
      <p v-if="!deleteImpact.references?.length">当前没有业务引用。</p>
      <n-alert v-if="deleteImpact.requiresReindex" type="warning">更换或清除向量模型会停用旧向量索引，受影响文档需要重新入库。</n-alert>
      <n-select v-model:value="replacementId" :options="replacementOptions" placeholder="选择替代模型或清除引用" />
      <p v-if="!replacementId">将清除这些引用并记录待恢复位置。以后添加同类模型时，可选择恢复。</p>
      <div class="model-dialog-actions"><e-button :disabled="deleting" @click="deleteVisible=false">取消</e-button><e-button type="primary" :loading="deleting" @click="confirmDelete">确认删除</e-button></div>
    </n-modal>
    <ModelTestPanel ref="modelTestPanel" @busy="updateTestBusy" />
  </div>
</template>

<script setup>


import { ref, reactive, onMounted, onBeforeUnmount, watch, computed, toRaw } from 'vue';
import { NInput, NSelect, NModal, NCheckbox, NCheckboxGroup, NAlert } from 'naive-ui';
import TenantModelDefaults from '@/components/ai/TenantModelDefaults.vue';
import ConversationLabelModel from '@/components/ai/ConversationLabelModel.vue';
import ModelReasoningFields from '@/components/ai/ModelReasoningFields.vue';
import ModelTestPanel from '@/components/ai/ModelTestPanel.vue';
import { experienceContext } from '@/app/experience/runtime';
import { 
  DatabaseOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EllipsisOutlined, EyeOutlined
} from '@/app/ui/icons';
import { message } from '@/app/ui/controls';
import { isMutationSuccess } from '@/utils/http';
import { Form } from '@/app/ui/controls';
import { 
  getAIModelsPageData, 
  addEditAIModel, 
  deleteAIModel, getModelDeleteImpact, getPendingModelReferences, revealModelSecret, fetchUpstreamModels
} from '@/api/ai/aiModels';

const useForm = Form.useForm;

// 模型数据
const modelList = ref([]);

// 分页配置
const pagination = reactive({
  current: 1,
  pageSize: 8,
  total: 0
});

// 模态框状态
const modelModalVisible = ref(false);
const confirmLoading = ref(false);
const testingConnection = ref(false);
const modelTestPanel = ref(null);
const modelModalTitle = ref('添加模型配置');

// 当前编辑的模型
const currentModel = ref(null);
const upstreamModels = ref([]);
const fetchingModels = ref(false);
const savedSecret = ref('');
const savedSecretVisible = ref(false);
const revealingSecret = ref(false);
const pendingReferences = ref([]);
const restoreReferenceIds = ref([]);
const setAsDefault = ref(false);
const deleteVisible = ref(false);
const deleting = ref(false);
const deletingModel = ref(null);
const deleteImpact = ref({ references: [], alternatives: [] });
const replacementId = ref(null);
const replacementOptions = computed(() => [{ label: '清除引用，稍后重新配置', value: '' },
  ...deleteImpact.value.alternatives.map(item => ({ label: item.name, value: item.id }))]);
let editorSequence = 0;
const clearEditorSecrets = () => { editorSequence++; savedSecret.value = ''; savedSecretVisible.value = false; upstreamModels.value = []; };
const toggleSavedSecret = async () => {
  if (savedSecretVisible.value) { savedSecret.value = ''; savedSecretVisible.value = false; return; }
  if (modelForm.modelKey || !currentModel.value?.secretConfigured) { savedSecretVisible.value = true; return; }
  const sequence = editorSequence;
  revealingSecret.value = true;
  try {
    const response = await revealModelSecret(currentModel.value.id);
    if (sequence !== editorSequence || !modelModalVisible.value) return;
    savedSecret.value = response.data.secret;
    savedSecretVisible.value = true;
  } catch { /* 请求层提示错误。 */ }
  finally { revealingSecret.value = false; }
};
const loadUpstreamModels = async () => {
  if (!modelForm.endPoint) return message.warning('请先填写模型地址和秘钥');
  const sequence = editorSequence;
  fetchingModels.value = true;
  try {
    const response = await fetchUpstreamModels(buildModelPayload());
    if (sequence !== editorSequence || !modelModalVisible.value) return;
    upstreamModels.value = response.data.models.map(name => ({ label: name, value: name }));
    if (!upstreamModels.value.length) message.info('上游未返回模型，请手动填写');
  } catch { /* 手动填写仍然可用。 */ }
  finally { fetchingModels.value = false; }
};

// 模型表单
const modelForm = reactive({
  id: '',
  aiType: undefined,
  aiModelType: undefined,
  endPoint: '',
  modelName: '',
  modelKey: '',
  modelDescription: '',
  embeddingValueSize: 2048,
  maxAskPromptSize: 131072,
  answerTokens: 8192,
  chatParameterProfile: 'auto',
  searchMode: 'disabled',
  reasoningPolicy: null
});
const displaySecret = computed(() => modelForm.modelKey || (savedSecretVisible.value ? savedSecret.value : ''));
const updateSecret = value => { modelForm.modelKey = value; };
onBeforeUnmount(() => { clearEditorSecrets(); modelForm.modelKey = ''; });
watch(() => modelForm.aiModelType, async type => {
  restoreReferenceIds.value = [];
  pendingReferences.value = [];
  const sequence = editorSequence;
  if (type !== 1 && ['openai_responses', 'anthropic'].includes(modelForm.aiType)) modelForm.aiType = 1;
  if (currentModel.value || !type) return;
  try {
    const response = await getPendingModelReferences(({ 1: 'chat', 2: 'embedding', 4: 'rerank' })[type]);
    if (sequence === editorSequence && type === modelForm.aiModelType && !currentModel.value) pendingReferences.value = response.data;
  } catch { /* 请求层提示错误。 */ }
});
watch(modelModalVisible, open => { if (!open) { clearEditorSecrets(); modelForm.modelKey = ''; } });
watch(() => [modelForm.endPoint, modelForm.aiType, modelForm.modelKey], () => { editorSequence++; upstreamModels.value = []; savedSecret.value = ''; savedSecretVisible.value = false; }, { flush: 'sync' });

// Token快捷赋值选项（1k = 1024 Token）
const askTokenOptions = [
  { label: '128k', value: 131072 },
  { label: '256k', value: 262144 },
  { label: '512k', value: 524288 },
  { label: '1M', value: 1048576 }
];
const answerTokenOptions = [
  { label: '4k', value: 4096 },
  { label: '16k', value: 16384 },
  { label: '32k', value: 32768 },
  { label: '128k', value: 131072 }
];

// 表单验证规则
const modelRules = reactive({
  aiType: [
    { required: true, message: '请选择AI类型' }
  ],
  aiModelType: [
    { required: true, message: '请选择模型类型' }
  ],
  endPoint: [
    { required: true, message: '请输入模型地址' }
  ],
  modelName: [
    { required: true, message: '请输入模型名称' }
  ], 
  modelDescription: [
    { required: true, message: '请输入部署名' }
  ],
  embeddingValueSize: [
    { required: false, message: '请输入矢量精度' }
  ]
});

// 表单验证
const { validate: validateModelForm, validateInfos } = useForm(modelForm, modelRules);

// 搜索关键字
const searchKeyword = ref('');

// 获取AI类型名称
const getAITypeName = (type) => {
  const types = {
    1: 'OpenAI',
    2: 'Azure',
    3: '智谱AI',
    7:'Bge Embedding',
    8:'Bge Rerank',
    10:'Ollama',
    11:'OllamaEmbedding',
    12:'Ali Rerank'
    , openai_responses: 'OpenAI Responses', anthropic: 'Anthropic Messages'
  };
  return Object.hasOwn(types, type) ? types[type] : '未知';
};

// 获取模型类型名称
const getModelTypeName = (type) => {
  const types = {
    1: 'Chat',
    2: 'Embedding',
    4:'Rerank'
  };
  return Object.hasOwn(types, type) ? types[type] : '未知';
};

// 搜索处理
const onSearch = (value) => {
  searchKeyword.value = value;
  pagination.current = 1;
  loadModelData();
};

// 加载模型数据
const loadModelData = async () => {
  try {
    const params = {
      pageNum: pagination.current,
      pageSize: pagination.pageSize,
      searchKey: searchKeyword.value
    };

    const response = await getAIModelsPageData(params);
    if (response && response.code === 200 && response.data.data) {
      modelList.value = response.data.data.map(item => ({
        ...item,
        key: item.id
      }));
      pagination.total = response.data.total;
    }
  } catch (error) {
    message.error('加载模型数据失败: ' + (error.message || '未知错误'));
  }
};

// 显示添加模型模态框
const showAddModelModal = () => {
  clearEditorSecrets(); pendingReferences.value = []; restoreReferenceIds.value = []; setAsDefault.value = false;
  modelModalTitle.value = '添加模型配置';
  currentModel.value = null;
  Object.assign(modelForm, {
    id: '',
    aiType: undefined,
    aiModelType: undefined,
    endPoint: '',
    modelName: '',
    modelKey: '',
    modelDescription: '',
    embeddingValueSize: 2048,
    maxAskPromptSize: 131072,
    answerTokens: 8192,
    chatParameterProfile: 'auto',
    searchMode: 'disabled',
    reasoningPolicy: null
  }); 
  modelModalVisible.value = true;
};

// 显示编辑模型模态框
const showEditModelModal = (record) => {
  clearEditorSecrets();
  modelModalTitle.value = '编辑模型配置';
  currentModel.value = record;
  modelForm.id = record.id || '';
  modelForm.aiType = record.aiType !== undefined ? record.aiType : undefined;
  modelForm.aiModelType = record.aiModelType !== undefined ? record.aiModelType : undefined;
  modelForm.endPoint = record.endPoint || '';
  modelForm.modelName = record.modelName || '';
  modelForm.modelKey = record.modelKey || '';
  modelForm.modelDescription = record.modelDescription || '';
  modelForm.embeddingValueSize = record.embeddingValueSize || 2048;
  modelForm.maxAskPromptSize = record.maxAskPromptSize ?? 131072;
  modelForm.answerTokens = record.answerTokens ?? 8192;
  modelForm.chatParameterProfile = record.chatParameterProfile ?? 'auto';
  modelForm.searchMode = record.searchMode ?? 'disabled';
  modelForm.reasoningPolicy = record.reasoningPolicy ? structuredClone(toRaw(record.reasoningPolicy)) : null;
  modelModalVisible.value = true;
};

// 删除模型确认
const showDeleteConfirm = async (record) => {
  try {
    const response = await getModelDeleteImpact(record.id);
    deletingModel.value = record;
    deleteImpact.value = response.data;
    replacementId.value = response.data.alternatives[0]?.id || '';
    deleteVisible.value = true;
  } catch { /* 请求层提示错误。 */ }
};
const confirmDelete = async () => {
  if (deleting.value) return;
  deleting.value = true;
  try {
    await handleDelete(deletingModel.value.id, deletingModel.value.modelName);
  } finally { deleting.value = false; }
};

// 删除模型
const handleDelete = async (id, modelName) => {
  try {
    const response = await deleteAIModel(id, deleteImpact.value.revision, replacementId.value);
    if (!isMutationSuccess(response)) {
      message.error(`模型"${modelName}"删除未生效`);
      return;
    }
    message.success(`模型"${modelName}"删除成功`);
    deleteVisible.value = false;
    loadModelData();
  } catch (error) {
    message.error('删除模型失败: ' + (error.message || '未知错误'));
  }
};

// 分页变化处理
const handlePageChange = (page, pageSize) => {
  pagination.current = page;
  pagination.pageSize = pageSize;
  loadModelData();
};

const buildModelPayload = () => {
  const payload = {
    aiType: modelForm.aiType,
    aiModelType: modelForm.aiModelType,
    endPoint: modelForm.endPoint,
    modelName: modelForm.modelName,
    modelKey: modelForm.modelKey,
    modelDescription: modelForm.modelDescription,
    maxAskPromptSize: modelForm.maxAskPromptSize,
    answerTokens: modelForm.answerTokens,
    chatParameterProfile: modelForm.chatParameterProfile,
    searchMode: modelForm.aiModelType === 1 ? modelForm.searchMode : 'disabled',
    reasoningPolicy: modelForm.aiModelType === 1 ? modelForm.reasoningPolicy : null,
    restoreReferenceIds: currentModel.value ? [] : restoreReferenceIds.value,
    setDefault: !currentModel.value && setAsDefault.value,
  };
  if (modelForm.aiModelType === 2 && modelForm.embeddingValueSize) {
    payload.embeddingValueSize = modelForm.embeddingValueSize;
  }
  if (currentModel.value) {
    payload.id = modelForm.id;
  }
  return payload;
};

const quickTestingId = ref(null);
const updateTestBusy = state => {
  testingConnection.value = state.running && state.source === 'form';
  quickTestingId.value = state.running && state.source === 'saved' ? state.modelId : null;
};
const modelKind = type => ({ 1:'chat', 2:'embedding', 4:'rerank' })[type] || String(type).toLowerCase();
const handleQuickTest = model => {
  modelTestPanel.value?.start({ id:model.id }, { id:model.id, name:model.modelName,
    kind:modelKind(model.aiModelType), source:'saved' });
};

const handleTestConnection = () => {
  if (testingConnection.value || quickTestingId.value != null) return;
  validateModelForm().then(async () => {
    await modelTestPanel.value?.start(buildModelPayload(), { id:modelForm.id,
      name:modelForm.modelName, kind:modelKind(modelForm.aiModelType), source:'form' });
  }).catch(() => {
    message.warning("请先完整填写模型配置");
  });
};

// 模型模态框确认
const handleModelModalOk = () => {
  if (confirmLoading.value) return;
  validateModelForm().then(async () => {
    confirmLoading.value = true;
    try { 
      const response = await addEditAIModel(buildModelPayload());
      if (!isMutationSuccess(response)) throw new Error('保存模型配置未生效');
      
      message.success(currentModel.value ? '模型信息更新成功' : '模型信息添加成功');
      
      modelModalVisible.value = false;
      loadModelData(); // 重新加载数据
    } catch (error) {
      message.error('保存模型失败: ' + (error.message || '未知错误'));
    } finally {
      confirmLoading.value = false;
    }
  }).catch(() => {
    message.warning('请先完整填写模型配置');
  });
};

// 模型模态框取消
const handleModelModalCancel = () => {
  modelModalVisible.value = false;
};

// 组件挂载时的初始化
onMounted(() => {
  console.log('模型配置管理页面已加载');
  loadModelData();
});
</script>

<style scoped>
.quick-test-btn {
  color: var(--eic-action-text) !important;
}
</style>

<style scoped>
.form-hint {
  margin: -8px 0 12px 25%;
  color: var(--eic-muted);
  font-size: var(--eic-font-meta);
  line-height: 1.5;
}
.secret-toggle { display:flex; align-items:center; justify-content:center; min-width:32px; min-height:32px; border:0; background:transparent; color:var(--eic-text-secondary); cursor:pointer; }
.secret-toggle:focus-visible { outline:2px solid var(--eic-primary); border-radius:4px; }
.model-recovery { padding:12px; margin-bottom:16px; border:1px solid var(--eic-line); border-radius:8px; }
.model-recovery .n-checkbox { display:flex; margin:8px 0; }
.model-dialog-actions { display:flex; justify-content:flex-end; gap:12px; margin-top:24px; }
</style>
