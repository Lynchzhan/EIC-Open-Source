<template>
  <e-modal
    v-model:open="visible"
    :title="modalTitle"
    ok-text="确定"
    cancel-text="取消"
    @ok="handleOk"
    @cancel="handleCancel"
    :confirm-loading="confirmLoading"
    :ok-button-props="{ disabled: !canWrite || documentLoading || !!documentError || operationBusy }"
    :cancel-button-props="{ disabled: operationBusy }"
    :closable="!operationBusy"
    :mask-closable="!operationBusy"
    :keyboard="!operationBusy"
    width="1080px"
  >
    <e-alert v-if="!canWrite" type="info" show-icon message="只读文档视图：当前账户没有知识库写入权限。" style="margin-bottom: 16px" />
    <e-alert type="info" show-icon :message="defaultModelHint" style="margin-bottom:12px" />
    <e-form :model="form" :label-col="{ xs: { span: 24 }, md: { span: 6 } }" :wrapper-col="{ xs: { span: 24 }, md: { span: 18 } }">
      <e-row :gutter="16">
        <e-col :xs="24" :md="12">
          <e-form-item label="ID">
            <e-input v-model:value="form.id" placeholder="知识库ID" disabled />
          </e-form-item>
        </e-col>
        <e-col :xs="24" :md="12">
          <e-form-item label="名称" v-bind="validateInfos.name">
            <e-input v-model:value="form.name" :disabled="!canWrite" placeholder="请输入知识库名称" />
          </e-form-item>
        </e-col>
      </e-row>
      <e-row :gutter="16">
        <e-col :xs="24" :md="12">
          <e-form-item label="段落的最大数" v-bind="validateInfos.maxTokensPerParagraph">
            <e-input-number v-model:value="form.maxTokensPerParagraph" :disabled="!canWrite" :min="1" :max="10000" style="width: 100%" />
          </e-form-item>
        </e-col>
        <e-col :xs="24" :md="12">
          <e-form-item label="行最大数" v-bind="validateInfos.maxTokensPerLine">
            <e-input-number v-model:value="form.maxTokensPerLine" :disabled="!canWrite" :min="1" :max="1000" style="width: 100%" />
          </e-form-item>
        </e-col>
      </e-row>
      <e-row :gutter="16">
        <e-col :xs="24" :md="12">
          <e-form-item label="段落重叠数" v-bind="validateInfos.overlappingTokens">
            <e-input-number v-model:value="form.overlappingTokens" :disabled="!canWrite" :min="0" :max="1000" style="width: 100%" />
          </e-form-item>
        </e-col>
      </e-row>
      <e-row :gutter="16">
        <e-col :xs="24" :md="12">
          <e-form-item label="知识库文档">
            <FileUpload
              v-if="canUpload && visible && uploadReady"
              :key="uploadBindingKey"
              ref="fileUploadRef"
              business="AIKmss"
              :keyValue="String(form.id || '')"
              sign="AIKmssDetailFlies"
              :accept="'.txt,.pdf,.md,.docx,.html,.xls,.xlsx,.jpg,.jpeg,.png,.gif,.bmp,.webp,.tif,.tiff'"
              :multiple="true"
              :max-count="30"
              :disabled="operationBusy || documentLoading"
              upload-button-text="选择文件"
              @upload-success="uploadSuccessHandler"
              @upload-error="handleUploadError"
              @delete-success="handleRemoveFile"
              @file-ids-change="handleFileIdsChange"
            />
            <span v-else>上传需同时具备知识库写入、文件读取与文件写入权限。</span>
            <div v-if="canWrite" class="document-actions">
              <e-space wrap>
                <e-button v-if="kbExists" type="primary" :loading="confirmLoading" :disabled="!pendingDocuments.length || operationBusy" @click="submitPendingDocuments">确认入库（{{ pendingDocuments.length }}）</e-button>
                <e-button :disabled="!pendingDocuments.length || operationBusy" @click="clearPendingDocuments">取消选择</e-button>
              </e-space>
              <div class="document-hint">上传和文本会先进入待执行列表，确认后由后台队列处理；默认每个知识库并发 2 个文档。</div>
            </div>
          </e-form-item>
        </e-col>
        <e-col :xs="24" :md="12">
          <e-form-item label="矢量模型" v-bind="validateInfos.aIModelsId">
            <e-select v-if="canWrite" v-model:value="form.aIModelsId" placeholder="使用租户默认向量模型（已有索引保留原绑定）" :options="modelOptions" allow-clear show-search optionFilterProp="label" />
            <span v-else>{{ form.aIModelsId ? `模型 ID ${form.aIModelsId}` : '未配置向量模型' }}</span>
          </e-form-item>
          <e-form-item label="Rerank模型" v-bind="validateInfos.aIRerankModelsId">
            <e-select v-if="canWrite" v-model:value="form.aIRerankModelsId" placeholder="使用租户默认 Rerank 模型" :options="rerankModelOptions" allow-clear show-search optionFilterProp="label" />
            <span v-else>{{ form.aIRerankModelsId ? `模型 ID ${form.aIRerankModelsId}` : '未配置重排模型' }}</span>
          </e-form-item>
        </e-col>
      </e-row>
      <e-row :gutter="16">
        <e-col v-if="canWrite" :xs="24" :md="12">
          <e-form-item label="直接添加">
            <e-button :disabled="operationBusy" @click="showAddContentModal" style="margin-right: 8px;"><EditOutlined />添加文本内容</e-button>
            <e-button disabled title="远程 URL 导入已关闭，请使用本地上传">远程导入已关闭</e-button>
          </e-form-item>
        </e-col>
        <e-col :xs="24" :md="12">
          <e-alert type="info" show-icon message="PDF/Word 展示提取文本；上传成功仅代表文件已保存，解析、向量化和索引状态以后台记录为准。" />
        </e-col>
      </e-row>
      <div class="documents-heading"><h3>文档与处理状态</h3>
        <e-space wrap><e-button v-if="canWrite" :disabled="!retryableDocuments.length || operationBusy" @click="openRetryModal">一键重试</e-button>
        <e-button :loading="documentLoading" :disabled="!kbExists" @click="pollDocumentStatus(0)">刷新状态</e-button></e-space></div>
      <e-alert v-if="documentError" type="error" show-icon :message="documentError" />
      <e-table :loading="documentLoading" :columns="detailColumns" :data-source="form.kmsDetailsList" :pagination="false" :scroll="{ x: 800 }" row-key="id">
        <template #bodyCell="{ column, record, index }">
          <template v-if="column.key === 'fileName'">
            <a v-if="canReadFiles && record.fileId && record.contentName" @click="downloadDocument(record)">{{ record.contentName }}</a>
            <span v-else>{{ record.contentName || previewText(record.content) }}</span>
          </template>
          <template v-else-if="column.key === 'fileType'">{{ record.fileType || 'Text' }}</template>
          <template v-else-if="column.key === 'status'"><e-tag :color="getStatusColor(record)">{{ getStatusText(record) }}</e-tag>
            <p v-if="documentFailure(record)" class="document-failure">{{ documentFailure(record) }}</p></template>
          <template v-else-if="column.key === 'chunkCount'">{{ record.chunkCount ?? '-' }}</template>
          <template v-else-if="column.key === 'updatedAt'">{{ formatDateTime(record.updatedAt) }}</template>
          <template v-else-if="column.key === 'action'">
            <div class="document-row-actions" role="group" :aria-label="`${record.contentName || '文档'}的操作`">
            <e-button size="small" @click="viewDetail(record)">查看</e-button>
            <e-button v-if="canWrite && canRetryDocument(record)" size="small" type="primary" ghost :loading="retryingId === record.id"
              :disabled="retryingId != null || operationBusy" @click="retryDocument(record)">确认重试</e-button>
            <e-button v-if="canWrite && record.persisted && isRunningStatus(record)" size="small" danger ghost
              :disabled="operationBusy || record.statusKey === 'cancelling'" @click="cancelDocument(record)">停止处理</e-button>
            <e-button v-if="canWrite" size="small" :disabled="operationBusy" @click="openEditDocument(record)">编辑</e-button>
            <e-button v-if="canUpload && (record.fileId || record.persisted)" size="small" :disabled="operationBusy" @click="startReplaceFile(record)">替换文件</e-button>
            <e-popconfirm v-if="canWrite" :disabled="operationBusy" title="确定要删除这条记录吗?" ok-text="确认删除" cancel-text="取消" @confirm="onDeleteDetail(record, index)">
              <e-button size="small" danger ghost class="document-delete-action" :disabled="operationBusy">删除</e-button>
            </e-popconfirm>
            </div>
          </template>
        </template>
      </e-table>
      <input ref="replaceInput" type="file" style="display: none" @change="onReplaceFileChosen" />
    </e-form>
    <e-modal v-model:open="contentModalVisible" title="添加文本内容" ok-text="提交入库" cancel-text="取消"
      :confirm-loading="mutationBusy" :closable="!mutationBusy" :mask-closable="!mutationBusy"
      :keyboard="!mutationBusy" :cancel-button-props="{ disabled: mutationBusy }"
      @ok="handleAddContent" @cancel="handleCancelContent" width="800px">
      <e-form :model="contentForm" :disabled="mutationBusy">
        <e-form-item label="内容类型">
          <e-radio-group v-model:value="contentForm.type">
            <e-radio value="text">纯文本</e-radio>
            <e-radio value="markdown">Markdown</e-radio>
            <e-radio value="html">HTML</e-radio>
          </e-radio-group>
        </e-form-item>
        <e-form-item label="内容标题"><e-input v-model:value="contentForm.contentName" placeholder="例如：系统产品手册" /></e-form-item>
        <e-form-item label="文本内容"><e-textarea v-model:value="contentForm.content" :rows="15" placeholder="请输入内容" :maxlength="50000" show-count /></e-form-item>
      </e-form>
    </e-modal>
    <e-modal v-model:open="viewModalVisible" title="文档详情" :footer="null" width="860px">
      <e-alert v-if="viewError" type="error" show-icon :message="viewError" />
      <e-spin :spinning="viewLoading">
      <e-descriptions :column="{ xs: 1, md: 2 }" size="small" style="margin-bottom: 12px;">
        <e-descriptions-item label="文件名">{{ viewRecord.contentName }}</e-descriptions-item>
        <e-descriptions-item label="类型">{{ viewRecord.fileType }}</e-descriptions-item>
        <e-descriptions-item label="状态">{{ getStatusText(viewRecord) }}</e-descriptions-item>
        <e-descriptions-item label="切片数">{{ viewRecord.chunkCount ?? '未提供' }}</e-descriptions-item>
      </e-descriptions>
      <div class="document-stages" aria-label="真实处理阶段">
        <div v-for="stage in documentStages(viewRecord)" :key="stage.key"><span>{{ stage.title }}</span><strong>{{ stage.value }}</strong></div>
      </div>
      <e-alert v-if="documentFailure(viewRecord)" type="error" show-icon :message="documentFailure(viewRecord)" style="margin: 12px 0" />
      <e-descriptions :column="{ xs: 1, md: 2 }" size="small" style="margin-bottom: 12px;">
        <e-descriptions-item label="抽取方式">{{ formatExtractionMethods(viewRecord.extractionReport) }}</e-descriptions-item>
        <e-descriptions-item label="失败页">{{ formatFailedPages(viewRecord.extractionReport) }}</e-descriptions-item>
      </e-descriptions>
      <p style="margin-bottom: 8px; color: var(--eic-muted);">提取文本（PDF 不当排版文档编辑）</p>
      <e-textarea :value="viewRecord.content || ''" :rows="10" readonly />
      <e-table style="margin-top: 16px;" size="small" :pagination="false" :scroll="{ x: 520 }" :columns="chunkColumns" :data-source="viewRecord.chunks || []" row-key="id">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'content'"><div class="chunk-preview">{{ record.content }}</div></template>
          <template v-else-if="column.key === 'pageNumbers'">{{ (record.pageNumbers || []).join('、') || '-' }}</template>
        </template>
      </e-table>
      </e-spin>
    </e-modal>
    <e-modal v-model:open="editModalVisible" title="编辑提取文本" ok-text="保存并重新入库" cancel-text="取消" :confirm-loading="editSaving" :closable="!editSaving" :mask-closable="!editSaving" :keyboard="!editSaving" :cancel-button-props="{ disabled: editSaving }" @ok="saveEditedDocument" width="860px">
      <e-form layout="vertical" :disabled="editSaving">
        <e-form-item label="标题"><e-input v-model:value="editForm.contentName" /></e-form-item>
        <e-form-item label="提取文本"><e-textarea v-model:value="editForm.content" :rows="16" /></e-form-item>
      </e-form>
    </e-modal>
    <e-modal v-model:open="retryModalVisible" title="选择需要重试的文档" ok-text="确认重试" cancel-text="取消" :confirm-loading="retrySubmitting"
      :closable="!retrySubmitting" :mask-closable="!retrySubmitting" :keyboard="!retrySubmitting"
      :cancel-button-props="{ disabled: retrySubmitting }" @ok="submitRetryDocuments" width="640px">
      <e-space style="margin-bottom: 12px;">
        <e-button size="small" :disabled="retrySubmitting" @click="selectAllRetryDocuments">全选</e-button>
        <e-button size="small" :disabled="retrySubmitting" @click="clearRetrySelection">取消全选</e-button>
        <span>已选择 {{ retrySelectedIds.length }} / {{ retryableDocuments.length }}</span>
      </e-space>
      <e-checkbox-group v-model:value="retrySelectedIds" :disabled="retrySubmitting" style="display: flex; flex-direction: column; gap: 8px;">
        <e-checkbox v-for="record in retryableDocuments" :key="record.id" :value="String(record.id)">
          {{ record.contentName || '未命名文档' }}
          <span class="retry-status">（{{ getStatusText(record) }}）</span>
          <p v-if="documentFailure(record)" class="document-failure">{{ documentFailure(record) }}</p>
        </e-checkbox>
      </e-checkbox-group>
    </e-modal>
  </e-modal>
</template>

<script setup>
/* eslint-disable no-undef */
import { ref, reactive, computed, watch, onBeforeUnmount, nextTick } from 'vue';
import { Form, message, Modal } from '@/app/ui/controls';
import { EditOutlined } from '@/app/ui/icons';
import { GetSnowflakeId } from '../../api/baseapi';
import FileUpload from '@/components/FileUpload.vue';
import { getFileById, uploadFile, setFileSharing } from '@/api/file';
import { getAIModelsALLList } from '@/api/ai/aiModels';
import http, { isMutationSuccess } from '@/utils/http';
import { canRetryDocument, documentStages, documentFailure, ingestionFeedback,
  isDocumentPending, knowledgeStatus } from '@/shared/ui/knowledgeWorkspace';
import {
  getKnowledgeDocuments,
  getKnowledgeDocument,
  updateKnowledgeDocument,
  replaceKnowledgeDocumentFile,
  deleteKnowledgeDocument,
  retryKnowledgeDocument,
  importKnowledgeFiles,
  retryKnowledgeDocuments,
  cancelKnowledgeDocument,
} from '@/api/ai/aikmss';

const emit = defineEmits(['ok', 'cancel']);
const props = defineProps({
  open: { type: Boolean, default: false },
  knowledgeBaseData: { type: Object, default: () => ({}) },
  modalType: { type: String, default: 'add' },
  canWrite: { type: Boolean, default: false },
  canReadFiles: { type: Boolean, default: false },
  canUpload: { type: Boolean, default: false },
});

const useForm = Form.useForm;
const visible = ref(false);
const confirmLoading = ref(false);
const fileUploadRef = ref(null);
const statusPollTimer = ref(null);
const documentLoading = ref(false), documentError = ref(''), retryingId = ref(null);
const mutationBusy = ref(false);
const uploadBindingKey = ref(0);
const uploadReady = ref(false);
const uploadSuccessHandler = ref(() => {});
const viewLoading = ref(false), viewError = ref('');
let documentEpoch = 0, documentSequence = 0, viewSequence = 0, editSequence = 0;
const isCurrent = (epoch) => epoch === documentEpoch && visible.value;
const checkedData = (response) => {
  if (response?.code !== 200 || response.isSuccess === false || response.data == null) {
    throw new Error(response?.msg || '接口响应未成功');
  }
  return response.data;
};
const replaceInput = ref(null);
const replaceTarget = ref(null);
const viewModalVisible = ref(false);
const viewRecord = reactive({});
const editModalVisible = ref(false);
const editSaving = ref(false);
const editForm = reactive({ id: null, contentName: '', content: '' });
const retryModalVisible = ref(false);
const retrySubmitting = ref(false);
const retrySelectedIds = ref([]);
const operationBusy = computed(() => confirmLoading.value || mutationBusy.value || retrySubmitting.value);
const form = reactive({
  id: null,
  name: '',
  maxTokensPerParagraph: 299,
  maxTokensPerLine: 99,
  overlappingTokens: 49,
  kmsDetailsList: [],
  aIModelsId: undefined,
  aIRerankModelsId: undefined,
});
const contentModalVisible = ref(false);
const contentForm = reactive({ content: '', type: 'text', contentName: '' });
const rules = reactive({
  name: [{ required: true, message: '请输入知识库名称' }],
  aIModelsId: [],
  aIRerankModelsId: [],
});
const { validate, validateInfos, resetFields } = useForm(form, rules);
const modelList = ref([]);
const rerankModelList = ref([]);
const tenantDefaults = ref({});
const defaultModelHint = computed(() => {
  const name = (list, id) => list.find(item => String(item.id) === String(id))?.modelDescription || (id ? `模型 ID ${id}（请核对是否可用）` : '未配置');
  return `租户默认：向量模型 ${name(modelList.value, tenantDefaults.value.embeddingModelId)}；Rerank ${name(rerankModelList.value, tenantDefaults.value.rerankModelId)}。清除选择表示继承默认；已有向量索引仍保留原模型。`;
});
const loadTenantDefaults = async epoch => {
  tenantDefaults.value = {};
  try {
    const result = await http.get('/api/AIModels/Defaults');
    if (isCurrent(epoch) && result.code === 200) tenantDefaults.value = result.data;
  } catch { /* 模型缺失由执行端明确阻止，不以首个选项代替默认。 */ }
};
const modelOptions = computed(() => modelList.value.map((model) => ({ label: model.modelDescription, value: model.id })));
const rerankModelOptions = computed(() => rerankModelList.value.map((model) => ({ label: model.modelDescription, value: model.id })));
const detailColumns = [
  { title: '文件名', key: 'fileName', width: '22%' },
  { title: '文件类型', key: 'fileType', width: '10%' },
  { title: '状态', key: 'status', width: '12%' },
  { title: '切片数', key: 'chunkCount', width: '10%' },
  { title: '更新时间', key: 'updatedAt', width: '18%' },
  { title: '操作', key: 'action' },
];
const chunkColumns = [
  { title: '序号', dataIndex: 'index', key: 'index', width: 70 },
  { title: '类型', dataIndex: 'kind', key: 'kind', width: 80 },
  { title: '内容', key: 'content' },
  { title: '页码', key: 'pageNumbers', width: 90 },
];
const modalTitle = computed(() => (props.modalType === 'edit' ? '知识库详情与文档' : '添加知识库'));
const kbExists = computed(() => props.modalType === 'edit' && !!form.id);
const pendingDocuments = computed(() => (form.kmsDetailsList || []).filter((item) => !item.persisted));
const isRunningStatus = (record) => ['queued', 'queued_text', 'processing', 'parsed_pending_embedding', 'cancelling'].includes(record?.statusKey || record?.status);
const retryableDocuments = computed(() => (form.kmsDetailsList || []).filter(canRetryDocument));

const unwrapList = (response) => {
  if (Array.isArray(response)) return response;
  if (response && Array.isArray(response.data)) return response.data;
  if (response && response.data && Array.isArray(response.data.data)) return response.data.data;
  return [];
};
const previewText = (content) => {
  const text = String(content || '').trim();
  if (!text) return '未命名文档';
  return text.length > 30 ? text.substring(0, 30) + '...' : text;
};
const formatDateTime = (value) => {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
};
const mapDocument = (doc) => ({
  id: doc.id,
  kmsId: doc.knowledgeBaseId || form.id || 0,
  fileId: doc.fileId || null,
  file: doc.fileId ? { id: doc.fileId, fileName: doc.sourceName || doc.contentName } : null,
  fileType: doc.fileType || getFileType(doc.sourceName || doc.contentName || ''),
  content: doc.content || '',
  contentName: doc.contentName || doc.sourceName || '文档',
  sourceName: doc.sourceName || doc.contentName || '文档',
  url: doc.url || '',
  chunkCount: doc.chunkCount,
  status: doc.status,
  statusKey: doc.statusKey,
  statusText: doc.statusText,
  errorMessage: doc.errorMessage || '',
  parseStatus: doc.parseStatus,
  embeddingStatus: doc.embeddingStatus,
  indexStatus: doc.indexStatus,
  indexError: doc.indexError || {},
  updatedAt: doc.updatedAt || '',
  persisted: doc.persisted !== false,
  chunks: doc.chunks || [],
  extractionReport: doc.extractionReport || {},
  activeIndexVersion: doc.activeIndexVersion,
  pendingIndexVersion: doc.pendingIndexVersion,
});
const clearUploadSlot = () => {
  fileUploadRef.value && fileUploadRef.value.clearUploadedFiles && fileUploadRef.value.clearUploadedFiles();
};
const stopStatusPolling = () => {
  if (statusPollTimer.value) {
    clearTimeout(statusPollTimer.value);
    statusPollTimer.value = null;
  }
};
const loadDocuments = async (epoch = documentEpoch) => {
  if (!visible.value || !form.id || props.modalType !== 'edit') return false;
  const seq = ++documentSequence, id = form.id;
  documentLoading.value = true;
  documentError.value = '';
  try {
    const response = await getKnowledgeDocuments(id);
    if (epoch !== documentEpoch || seq !== documentSequence || !visible.value) return false;
    if (!Array.isArray(checkedData(response))) throw new Error('文档列表响应无效');
    const rows = unwrapList(response);
    form.kmsDetailsList = [...(Array.isArray(rows) ? rows.map(mapDocument) : []), ...pendingDocuments.value];
    return true;
  } catch (error) {
    if (epoch === documentEpoch && seq === documentSequence && visible.value) {
      documentError.value = `文档加载失败：${error.message || '请刷新状态重试'}`;
    }
    return false;
  } finally {
    if (epoch === documentEpoch && seq === documentSequence) documentLoading.value = false;
  }
};
const pollDocumentStatus = async (attempt = 0, epoch = documentEpoch) => {
  if (!visible.value || epoch !== documentEpoch) return;
  stopStatusPolling();
  if (!await loadDocuments(epoch) || epoch !== documentEpoch || !visible.value) return;
  const pending = (form.kmsDetailsList || []).filter((item) => item.persisted && isDocumentPending(item));
  if (!pending.length) {
    return;
  }
  if (attempt >= 45) {
    message.warning('文档仍在处理中，可稍后重新打开知识库查看最新状态');
    return;
  }
  statusPollTimer.value = setTimeout(() => {
    pollDocumentStatus(attempt + 1, epoch);
  }, 2000);
};

watch(
  () => props.open,
  async (newVal) => {
    const epoch = ++documentEpoch;
    documentSequence++;
    viewSequence++;
    editSequence++;
    retryingId.value = null; mutationBusy.value = false; editSaving.value = false;
    retrySubmitting.value = false; retryModalVisible.value = false; retrySelectedIds.value = [];
    confirmLoading.value = false;
    uploadReady.value = false;
    documentLoading.value = false; documentError.value = '';
    viewModalVisible.value = false; editModalVisible.value = false; contentModalVisible.value = false;
    visible.value = newVal;
    if (!newVal) {
      stopStatusPolling();
      return;
    }
    resetFields();
    form.kmsDetailsList = [];
    if (props.canWrite) { loadModelList(epoch); loadRerankModelList(epoch); loadTenantDefaults(epoch); }
    if (props.modalType === 'edit' && props.knowledgeBaseData) {
      Object.keys(form).forEach((key) => {
        if (props.knowledgeBaseData[key] !== undefined) form[key] = props.knowledgeBaseData[key];
      });
      form.kmsDetailsList = [];
      clearUploadSlot();
      await pollDocumentStatus(0, epoch);
    } else {
      try {
        const snowflakeId = await GetSnowflakeId();
        if (epoch !== documentEpoch || !visible.value) return;
        form.id = snowflakeId.data;
      } catch (error) {
        form.id = null;
      }
      Object.assign(form, {
        name: '',
        maxTokensPerParagraph: 299,
        maxTokensPerLine: 99,
        overlappingTokens: 49,
        kmsDetailsList: [],
        aIModelsId: undefined,
        aIRerankModelsId: undefined,
      });
    }
    if (!isCurrent(epoch)) return;
    uploadBindingKey.value = epoch;
    const kbId = form.id, existsInBackend = kbExists.value;
    let queue = Promise.resolve();
    uploadSuccessHandler.value = (file) => {
      queue = queue.then(() => beforeUploadSuccess(file, epoch, kbId, existsInBackend));
    };
    uploadReady.value = true;
  }
);
watch(visible, (newVal) => {
  if (!newVal) emit('cancel');
});

const loadModelList = async (epoch) => {
  try {
    const response = await getAIModelsALLList(2);
    if (isCurrent(epoch)) modelList.value = checkedData(response);
  } catch (error) {
    console.error(error);
  }
};
const loadRerankModelList = async (epoch) => {
  try {
    const response = await getAIModelsALLList(4);
    if (isCurrent(epoch)) rerankModelList.value = checkedData(response);
  } catch (error) {
    console.error(error);
  }
};

const handleOk = async () => {
  if (!props.canWrite || operationBusy.value || documentLoading.value || documentError.value) return;
  const epoch = documentEpoch;
  try {
    if (kbExists.value && pendingDocuments.value.length) {
      message.warning('请先点击“确认入库”，提交待执行文档');
      return;
    }
    await validate();
    if (!isCurrent(epoch) || operationBusy.value) return;
    confirmLoading.value = true;
    emit('ok', {
      id: form.id,
      name: form.name,
      maxTokensPerParagraph: form.maxTokensPerParagraph,
      maxTokensPerLine: form.maxTokensPerLine,
      aIModelsId: form.aIModelsId ?? null,
      aIRerankModelsId: form.aIRerankModelsId ?? null,
      overlappingTokens: form.overlappingTokens,
      aIKmssDetailsList: form.kmsDetailsList,
    }, () => { if (isCurrent(epoch)) confirmLoading.value = false; });
  } catch (error) {
    console.log(error);
  }
};
const handleCancel = () => { if (!operationBusy.value) visible.value = false; };

const beforeUploadSuccess = async (file, epoch, kbId, existsInBackend) => {
  if (!props.canUpload || !isCurrent(epoch) || operationBusy.value) return false;
  mutationBusy.value = true;
  try {
  if (!await confirmKnowledgeFileSharing(file.fileId, epoch) || !isCurrent(epoch)) return false;
  const exists = form.kmsDetailsList.some((item) => String(item.fileId) === String(file.fileId));
  if (!exists) {
    form.kmsDetailsList.push({
      id: Date.now(),
      kmsId: form.id || 0,
      fileId: file.fileId,
      file: { fileName: file.fileName, size: file.fileSize },
      fileType: getFileType(file.fileName),
      content: '',
      contentName: file.fileName,
      url: file.url || '',
      chunkCount: 0,
      status: 0,
      statusKey: 'pending',
      statusText: '待处理',
      errorMessage: '',
      persisted: false,
    });
  }
  clearUploadSlot();
  await nextTick();
  clearUploadSlot();
  return false;
  } catch (error) {
    if (isCurrent(epoch)) message.error(error.message || '文档入库失败');
    return false;
  } finally {
    if (isCurrent(epoch)) mutationBusy.value = false;
  }
};

const clearPendingDocuments = () => {
  if (operationBusy.value) return;
  form.kmsDetailsList = form.kmsDetailsList.filter((item) => item.persisted);
  clearUploadSlot();
};

const submitPendingDocuments = async () => {
  if (!props.canWrite || !pendingDocuments.value.length || !kbExists.value || operationBusy.value) return;
  const epoch = documentEpoch;
  const selected = [...pendingDocuments.value];
  confirmLoading.value = true;
  try {
    await importKnowledgeFiles({
      knowledgeBaseId: form.id,
      documents: selected.map((item) => ({
        fileId: item.fileId || undefined,
        contentName: item.contentName,
        content: item.content || undefined,
        type: item.fileType,
      })),
    });
    if (!isCurrent(epoch)) return;
    form.kmsDetailsList = form.kmsDetailsList.filter((item) => !selected.includes(item));
    await pollDocumentStatus(0, epoch);
    if (isCurrent(epoch)) message.success('文档已提交后台入库，可继续选择其他文档');
  } catch (error) {
    if (isCurrent(epoch)) message.error(error.message || '批量入库失败');
  } finally {
    if (isCurrent(epoch)) confirmLoading.value = false;
  }
};

const openRetryModal = () => {
  if (!props.canWrite || operationBusy.value) return;
  retrySelectedIds.value = retryableDocuments.value.map((item) => String(item.id));
  retryModalVisible.value = true;
};
const selectAllRetryDocuments = () => {
  if (retrySubmitting.value) return;
  retrySelectedIds.value = retryableDocuments.value.map((item) => String(item.id));
};
const clearRetrySelection = () => { if (!retrySubmitting.value) retrySelectedIds.value = []; };
const submitRetryDocuments = async () => {
  if (!props.canWrite || operationBusy.value) return;
  const epoch = documentEpoch;
  if (!retrySelectedIds.value.length) {
    message.warning('请至少选择一个文档');
    return;
  }
  retrySubmitting.value = true;
  try {
    const result = await retryKnowledgeDocuments({ documentIds: retrySelectedIds.value, acknowledgeDuplicateCall: true });
    if (!isCurrent(epoch)) return;
    const items = Array.isArray(result?.data?.documents) ? result.data.documents : [];
    if (!items.length) throw new Error('后台未返回文档受理结果，请刷新状态后核对');
    const accepted = items.filter((item) => item.accepted === true);
    const rejected = items.filter((item) => item.accepted !== true);
    retrySelectedIds.value = rejected.map((item) => String(item.documentId));
    retryModalVisible.value = rejected.length > 0;
    await pollDocumentStatus(0, epoch);
    if (!isCurrent(epoch)) return;
    const reused = accepted.filter((item) => item.reused).length;
    if (!accepted.length) message.warning('所选文档均未受理：状态已变化或仍在执行，请核对最新状态');
    else if (rejected.length) message.warning(`已受理 ${accepted.length} 项，${rejected.length} 项未受理，请核对文档状态`);
    else message.success(`已受理 ${accepted.length} 项${reused ? `（其中 ${reused} 项沿用已有任务）` : '，将在后台处理'}`);
  } catch (error) {
    if (isCurrent(epoch)) message.error(error.message || '提交重试失败');
  } finally {
    if (isCurrent(epoch)) retrySubmitting.value = false;
  }
};

const cancelDocument = async (record) => {
  if (!props.canWrite || operationBusy.value) return;
  const epoch = documentEpoch;
  const accepted = await new Promise((resolve) => {
    Modal.confirm({
      title: '停止文档处理',
      content: '当前任务会在正在完成的请求结束后停止，是否继续？',
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    });
  });
  if (!accepted || !isCurrent(epoch)) return;
  mutationBusy.value = true;
  try {
    await cancelKnowledgeDocument(record.id);
    if (!isCurrent(epoch)) return;
    await pollDocumentStatus(0, epoch);
    if (isCurrent(epoch)) message.success('已请求停止文档处理');
  } catch (error) {
    if (isCurrent(epoch)) message.error(error.message || '停止文档处理失败');
  } finally {
    if (isCurrent(epoch)) mutationBusy.value = false;
  }
};

const showAddContentModal = () => {
  if (!props.canWrite || operationBusy.value) return;
  contentForm.content = '';
  contentForm.type = 'text';
  contentForm.contentName = '';
  contentModalVisible.value = true;
};
const handleAddContent = async () => {
  if (!props.canWrite || operationBusy.value) return;
  if (!contentForm.content.trim()) { message.warning('请输入内容'); return; }
  if (!contentForm.contentName.trim()) { message.warning('请输入内容标题名称'); return; }
  if (kbExists.value) {
    form.kmsDetailsList.push({
      id: Date.now(), kmsId: form.id, fileId: null, file: null, fileType: contentForm.type,
      content: contentForm.content, contentName: contentForm.contentName, url: '', chunkCount: 0,
      status: 0, statusKey: 'pending', statusText: '待确认', errorMessage: '', persisted: false,
    });
    contentModalVisible.value = false;
    message.info('文本已加入待入库列表');
    return;
  }
  form.kmsDetailsList.push({
    id: Date.now(),
    kmsId: form.id || 0,
    fileId: null,
    file: null,
    fileType: contentForm.type,
    content: contentForm.content,
    contentName: contentForm.contentName,
    url: '',
    chunkCount: 0,
    status: 0,
    statusKey: 'pending',
    statusText: '待处理',
    errorMessage: '',
    persisted: false,
  });
  contentModalVisible.value = false;
};
const handleCancelContent = () => { if (!mutationBusy.value) contentModalVisible.value = false; };

const onDeleteDetail = async (record, index) => {
  if (!props.canWrite || operationBusy.value) return;
  if (record && record.persisted && record.id) {
    const epoch = documentEpoch;
    mutationBusy.value = true;
    try {
      if (!isMutationSuccess(await deleteKnowledgeDocument(record.id))) throw new Error('文档删除未生效');
      if (!isCurrent(epoch)) return;
      await loadDocuments(epoch);
      if (isCurrent(epoch)) message.success('文档已删除');
    } catch (error) {
      if (isCurrent(epoch)) message.error(error.message || '删除文档失败');
    } finally {
      if (isCurrent(epoch)) mutationBusy.value = false;
    }
    return;
  }
  form.kmsDetailsList.splice(index, 1);
};

const viewDetail = async (record) => {
  const seq = ++viewSequence, epoch = documentEpoch;
  Object.keys(viewRecord).forEach((key) => delete viewRecord[key]);
  Object.assign(viewRecord, { ...record, chunks: record.chunks || [], content: record.content || '' });
  viewError.value = ''; viewLoading.value = false;
  viewModalVisible.value = true;
  if (record.persisted && record.id) {
    try {
      viewLoading.value = true;
      const response = await getKnowledgeDocument(record.id);
      if (epoch !== documentEpoch || seq !== viewSequence || !viewModalVisible.value) return;
      const detail = checkedData(response);
      Object.assign(viewRecord, mapDocument(detail), { chunks: detail.chunks || [] });
    } catch (error) {
      if (epoch === documentEpoch && seq === viewSequence) viewError.value = error.message || '加载文档详情失败';
    } finally {
      if (epoch === documentEpoch && seq === viewSequence) viewLoading.value = false;
    }
  }
};

const openEditDocument = async (record) => {
  if (!props.canWrite || operationBusy.value) return;
  const epoch = documentEpoch, seq = ++editSequence;
  let content = record.content || '';
  if (record.persisted && record.id && !content) {
    try {
      const response = await getKnowledgeDocument(record.id);
      if (!isCurrent(epoch) || seq !== editSequence) return;
      const detail = checkedData(response);
      content = detail.content || '';
    } catch (error) {
      if (isCurrent(epoch) && seq === editSequence) message.error(error.message || '加载提取文本失败');
      return;
    }
  }
  editForm.id = record.id;
  editForm.contentName = record.contentName || record.sourceName || '';
  editForm.content = content;
  editModalVisible.value = true;
};

const saveEditedDocument = async () => {
  if (!props.canWrite || editSaving.value || operationBusy.value) return;
  if (!String(editForm.content || '').trim()) { message.warning('文档内容不能为空'); return; }
  const target = form.kmsDetailsList.find((item) => String(item.id) === String(editForm.id));
  if (kbExists.value && target && target.persisted) {
    const epoch = documentEpoch;
    editSaving.value = true;
    mutationBusy.value = true;
    try {
      const result = await updateKnowledgeDocument({ id: editForm.id, contentName: editForm.contentName, content: editForm.content });
      if (!isCurrent(epoch)) return;
      const feedback = ingestionFeedback(result);
      editModalVisible.value = false;
      await pollDocumentStatus(0, epoch);
      if (isCurrent(epoch)) message.info(feedback);
    } catch (error) {
      if (isCurrent(epoch)) message.error(error.message || '保存失败');
    } finally {
      if (isCurrent(epoch)) { editSaving.value = false; mutationBusy.value = false; }
    }
    return;
  }
  if (target) {
    target.content = editForm.content;
    target.contentName = editForm.contentName;
  }
  editModalVisible.value = false;
};

const startReplaceFile = (record) => {
  if (!props.canUpload || operationBusy.value) return;
  replaceTarget.value = record;
  if (replaceInput.value) replaceInput.value.value = '';
  replaceInput.value && replaceInput.value.click();
};

const confirmKnowledgeFileSharing = async (fileId, epoch) => {
  const accepted = await new Promise((resolve) => {
    Modal.confirm({
      title: '确认共享知识库文件',
      content: '导入知识库会向同租户具有知识库读取权限的用户共享文档内容；原文件共享后可由具有文件读取权限的用户下载。取消后文件仍仅本人可见。',
      okText: '确认共享', cancelText: '取消',
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    });
  });
  if (!accepted || !isCurrent(epoch)) return false;
  try {
    checkedData(await setFileSharing(fileId, 'tenant'));
    return isCurrent(epoch);
  } catch (error) {
    message.error(error.message || '共享文件失败');
    return false;
  }
};

const onReplaceFileChosen = async (event) => {
  if (!props.canUpload || operationBusy.value) return;
  const epoch = documentEpoch, kbId = form.id;
  const file = event.target.files && event.target.files[0];
  const record = replaceTarget.value;
  if (!file || !record) return;
  mutationBusy.value = true;
  try {
    const uploadResult = await uploadFile('AIKmss', String(kbId || ''), 'AIKmssDetailFlies', file);
    if (!isCurrent(epoch)) return;
    const payload = checkedData(uploadResult);
    const fileId = (payload && (payload.id || payload.fileId)) || (payload && payload.data && payload.data.id);
    if (!fileId || String(fileId) === '[object Object]') throw new Error('文件 ID 无效');
    if (!await confirmKnowledgeFileSharing(fileId, epoch) || !isCurrent(epoch)) return;
    if (kbExists.value && record.persisted && record.id) {
      const result = await replaceKnowledgeDocumentFile(record.id, fileId);
      if (!isCurrent(epoch)) return;
      const feedback = ingestionFeedback(result);
      await pollDocumentStatus(0, epoch);
      if (isCurrent(epoch)) message.info(feedback);
    } else {
      record.fileId = fileId;
      record.contentName = file.name;
      record.fileType = getFileType(file.name);
      record.file = { id: fileId, fileName: file.name, size: file.size };
      record.status = 0;
      record.statusText = '待处理';
    }
  } catch (error) {
    if (isCurrent(epoch)) message.error(error.message || '替换文件失败');
  } finally {
    if (isCurrent(epoch)) {
      mutationBusy.value = false;
      replaceTarget.value = null;
      if (replaceInput.value) replaceInput.value.value = '';
    }
  }
};

const getFileType = (fileName) => {
  const ext = String(fileName || '').split('.').pop();
  const lower = (ext || '').toLowerCase();
  if (['pdf'].includes(lower)) return 'PDF';
  if (['docx'].includes(lower)) return 'Word';
  if (['xls', 'xlsx'].includes(lower)) return 'Excel';
  if (['txt'].includes(lower)) return 'Text';
  if (['md'].includes(lower)) return 'Markdown';
  if (['html', 'htm'].includes(lower)) return 'Html';
  if (['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tif', 'tiff'].includes(lower)) return 'Image';
  return 'Other';
};
const getStatusColor = (record) => knowledgeStatus(record).color;
const getStatusText = (record) => knowledgeStatus(record).label;
const handleRemoveFile = async (file) => {
  const fileId = file.fileId || file.id;
  const index = form.kmsDetailsList.findIndex((item) => String(item.fileId) === String(fileId));
  if (index === -1) return;
  await onDeleteDetail(form.kmsDetailsList[index], index);
};
const handleFileIdsChange = () => {};
const downloadDocument = async (record) => {
  const epoch = documentEpoch;
  if (!props.canReadFiles || !record.fileId || !isCurrent(epoch)) return;
  try { await getFileById(record.fileId, record.contentName); }
  catch (error) { if (isCurrent(epoch)) message.error(`文件下载失败：${error.message || '请检查权限和网络'}`); }
};
const handleUploadError = () => { message.error('文件上传失败，请检查权限和网络后重试'); };
const EXTRACTION_METHOD_LABELS = {
  text_layer: '文字层',
  scan_ocr: '扫描 OCR',
  embedded_image_ocr: '内嵌图 OCR',
  table_markdown: '表格 Markdown',
  manual_edit: '人工修订',
};
const formatExtractionMethods = (report) => {
  const methods = Array.isArray(report?.methods) ? report.methods.filter((item) => typeof item === 'string') : [];
  if (!methods.length) return '未记录';
  return methods.map((item) => Object.hasOwn(EXTRACTION_METHOD_LABELS, item) ? EXTRACTION_METHOD_LABELS[item] : item).join('、');
};
const formatFailedPages = (report) => {
  const pages = (report && report.failedPages) || [];
  return Array.isArray(report?.failedPages) ? (pages.join('、') || '无') : '未提供';
};
const retryDocument = (record) => {
  if (!props.canWrite || operationBusy.value || retryingId.value != null || !canRetryDocument(record)) return;
  const epoch = documentEpoch;
  Modal.confirm({
    title: '确认重新处理文档',
    content: '确认后在后台处理，可继续选择其他文档或关闭弹窗。超时、传输中断或结果不确定时不会自动重发。重新处理可能产生模型费用。',
    okText: '确认风险并重试', cancelText: '取消',
    onOk: async () => {
       if (!props.canWrite || !isCurrent(epoch) || operationBusy.value || retryingId.value != null) return;
      retryingId.value = record.id;
       mutationBusy.value = true;
      try {
        const result = await retryKnowledgeDocument(record.id);
         if (!isCurrent(epoch)) return;
        message.info(ingestionFeedback(result));
        void pollDocumentStatus(0, epoch);
      } catch (error) {
         if (isCurrent(epoch)) message.error(error.message || '文档重试失败，请刷新状态后核对');
      } finally {
         if (isCurrent(epoch)) { retryingId.value = null; mutationBusy.value = false; }
      }
    },
  });
};
onBeforeUnmount(() => { documentEpoch++; documentSequence++; viewSequence++; stopStatusPolling(); });
defineExpose({ resetFields, loadDocuments, pollDocumentStatus });
</script>

<style scoped>
.document-row-actions { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:4px 0; }
.document-row-actions :deep(.n-button) { min-height:30px; padding-inline:10px; font-size:var(--eic-font-button); }
.document-delete-action { margin-left:4px; }
.documents-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 16px 0; }
.documents-heading h3 { margin: 0; }
.document-failure { font-size: var(--eic-font-meta); color: var(--eic-danger); overflow-wrap: anywhere; margin: 6px 0 0; }
.document-stages { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }
.document-stages > div { padding: 12px; border: 1px solid var(--eic-border); border-radius: 10px; background: var(--eic-bg); }
.document-stages span { display: block; color: var(--eic-text-secondary); font-size: var(--eic-font-meta); margin-bottom: 8px; }
.document-stages strong { font-size: var(--eic-font-ui); overflow-wrap: anywhere; }
@media (max-width: 767px) { .document-stages { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.chunk-preview {
  max-height: 72px;
  overflow: auto;
  white-space: pre-wrap;
}
.document-actions {
  margin-top: 8px;
}
.document-hint {
  margin-top: 6px;
  color: var(--eic-muted);
  font-size: var(--eic-font-meta);
}
.retry-status {
  color: var(--eic-muted);
}
</style>
