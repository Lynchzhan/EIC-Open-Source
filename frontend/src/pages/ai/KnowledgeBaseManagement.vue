<template>
  <div class="management-container v4-page">
    <header class="knowledge-intro eic-page-toolbar">
      <p>文档入库、处理状态与引用来源</p>
      <e-button :loading="loading" :disabled="modalVisible || configDrawerVisible || savingStrategyId != null" @click="loadKnowledgeBaseList">刷新列表</e-button>
    </header>
    <e-alert v-if="loadError" class="list-error" type="error" show-icon :message="loadError" />
    <e-alert v-if="!loading && !canRead" type="warning" show-icon message="暂无知识库读取权限" />
    <e-alert v-else-if="!loading && !canWrite" type="info" show-icon class="list-error"
      message="当前为只读视图，可查看文档与召回结果；配置、上传和重试需要知识库写入权限。" />
    <e-card v-if="canRead || loading" class="management-card">
      <template #title>
        <div class="card-header">
          <div class="header-actions">
            <e-input-search
              class="search-input"
              placeholder="搜索知识库..."
              v-model:value="searchParams.name"
              @search="handleSearch"
            />
            <e-button v-if="canWrite" type="primary" class="add-button" @click="showAddModal">
              <template #icon>
                <PlusOutlined />
              </template>
              新增知识库
            </e-button>
          </div>
        </div>
      </template>

      <e-alert v-if="loadError" class="list-error" type="error" show-icon :message="loadError">
        <template #action><e-button size="small" :loading="loading" @click="loadKnowledgeBaseList">重新加载</e-button></template>
      </e-alert>
      <e-spin :spinning="loading" tip="正在加载知识库...">
      <div class="cards-container" :aria-busy="loading">
        <e-row :gutter="[24, 24]">
          <e-col 
            :xs="24" 
            :sm="12" 
            :md="12" 
            :lg="8" 
            :xl="6" 
            v-for="kb in knowledgeBaseList" 
            :key="kb.id"
          >
            <e-card class="knowledge-base-card" hoverable>
              <template #title>
                <div class="card-title">
                  <span class="kb-name">{{ kb.name }}</span>
                </div>
              </template>
              <template #extra>
                <e-dropdown v-if="canWrite">
                  <button type="button" class="eic-menu-trigger" aria-label="配置显示列">
                    <EllipsisOutlined />
                  </button>
                  <template #overlay>
                    <e-menu>
                      <e-menu-item @click="handleEdit(kb)">
                        <EditOutlined /> 编辑
                      </e-menu-item>
                      <e-menu-item @click="handleDelete(kb)">
                        <DeleteOutlined /> 删除
                      </e-menu-item> 
                    </e-menu>
                  </template>
                </e-dropdown>
              </template>
              
              <div @click="handleEdit(kb)" class="card-content">
                <p class="kb-desc">{{ kb.description || kb.describe || "暂无描述" }}</p>
                <div class="kb-strategy" @click.stop>
                  <e-segmented
                    :value="kb.retrievalStrategy || 'full'"
                    :options="strategyOptions"
                    :disabled="!canWrite || loading || savingStrategyId != null || configDrawerVisible || modalVisible"
                    @change="(value) => changeStrategy(kb, value)"
                  />
                  <e-button v-if="canWrite" type="link" size="small" @click="openConfigDrawer(kb)">参数</e-button>
                  <e-button type="link" size="small" @click="handleEdit(kb)">文档详情</e-button>
                  <e-button type="link" size="small" @click="openRecallPage(kb)">检索测试</e-button>
                </div>
                <div class="kb-stats">
                  <div class="kb-stat">
                    <span class="kb-stat-value" :title="String(kb.documentCount ?? '未提供')">{{ kb.documentCount ?? '未提供' }}</span>
                    <span class="kb-stat-label">文档</span>
                  </div>
                  <div class="kb-stat">
                    <span class="kb-stat-value" :title="String(kb.chunkCount ?? '未提供')">{{ kb.chunkCount ?? '未提供' }}</span>
                    <span class="kb-stat-label">切片</span>
                  </div>
                  <div class="kb-stat">
                    <e-tag :color="getStatusColor(kb.statusKey || kb.indexStatus || kb.status)">{{ getStatusText(kb.statusKey || kb.indexStatus || kb.status) }}</e-tag>
                    <span class="kb-stat-label">状态</span>
                  </div>
                </div>
                <div class="kb-meta">
                  <span>分段 {{ kb.maxTokensPerParagraph ?? "-" }} / 重叠 {{ kb.overlappingTokens ?? "-" }}</span>
                  <span>{{ formatKbTime(kb.updateTime || kb.updatedAt) }}</span>
                </div>
              </div>
            </e-card>
          </e-col>
        </e-row>
        
        <e-empty v-if="!loading && !loadError && knowledgeBaseList.length === 0" description="暂无知识库数据" />
        
        <div class="pagination-container" v-if="knowledgeBaseList.length > 0">
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
      </e-spin>
    </e-card>

    <KnowledgeSearch :open="searchVisible" :knowledge-base="searchTarget" :can-read="canRead" @close="searchVisible = false" />
    <!-- 知识库添加/编辑模态框 -->
    <KnowledgeBaseAddEdit
      :open="modalVisible"
      :knowledgeBaseData="currentRecord"
      :modalType="modalType"
      :can-write="canWrite"
      :can-read-files="canReadFiles"
      :can-upload="canUpload"
      @ok="handleModalOk"
      @cancel="handleModalCancel"
    />

    <e-drawer
      v-model:open="configDrawerVisible"
      title="检索参数"
      width="min(100vw, 460px)"
      :destroy-on-close="true"
      :closable="!configSaving"
      :mask-closable="!configSaving"
      :keyboard="!configSaving"
    >
      <e-form layout="vertical" v-if="configForm" :disabled="configSaving">
        <e-form-item label="TopK">
          <e-input-number v-model:value="configForm.topK" :min="1" :max="50" style="width: 100%" />
        </e-form-item>
        <e-form-item label="阈值版本">
          <e-select v-model:value="configForm.thresholdVersion" :options="[{value: 1, label: '旧阈值，混合检索待校准'}, {value: 2, label: '独立分数空间 v2'}]" />
        </e-form-item>
        <e-alert v-if="configForm.thresholdVersion === 1" type="warning" show-icon message="旧阈值未自动转换；请完成校准后显式选择 v2。正旧阈值会阻断混合检索。" />
        <template v-if="configForm.thresholdVersion === 2">
          <e-form-item label="Dense cosine 阈值"><e-input-number v-model:value="configForm.denseThreshold" :min="-1" :max="1" :step="0.05" /></e-form-item>
          <e-form-item label="BM25 阈值"><e-input-number v-model:value="configForm.sparseThreshold" :min="0" :step="0.1" /></e-form-item>
          <e-form-item label="RRF 阈值"><e-input-number v-model:value="configForm.fusionThreshold" :min="0" :max="2" :step="0.001" /></e-form-item>
          <e-form-item label="Rerank relevance 阈值"><e-input-number v-model:value="configForm.rerankThreshold" :min="0" :max="1" :step="0.05" /></e-form-item>
        </template>
        <e-form-item label="向量召回数">
          <e-input-number v-model:value="configForm.vectorTopK" :min="1" :max="100" style="width: 100%" />
        </e-form-item>
        <e-form-item label="BM25 召回数">
          <e-input-number v-model:value="configForm.bm25TopK" :min="1" :max="100" style="width: 100%" />
        </e-form-item>
        <e-form-item label="RRF k">
          <e-input-number v-model:value="configForm.rrfK" :min="1" :max="200" style="width: 100%" />
        </e-form-item>
        <e-form-item label="最终条数">
          <e-input-number v-model:value="configForm.finalLimit" :min="1" :max="50" style="width: 100%" />
        </e-form-item>
        <e-form-item label="融合候选预算">
          <e-input-number v-model:value="configForm.candidateLimit" :min="1" :max="200" style="width: 100%" />
        </e-form-item>
        <e-form-item label="重排输出预算">
          <e-input-number v-model:value="configForm.rerankTopN" :min="1" :max="100" style="width: 100%" />
        </e-form-item>
        <e-form-item label="父块回填">
          <e-switch v-model:checked="configForm.parentFill" />
        </e-form-item>
        <e-form-item label="父块大小">
          <e-input-number v-model:value="configForm.parentSize" :min="1" :max="8000" style="width: 100%" />
        </e-form-item>
        <e-form-item label="子块大小">
          <e-input-number v-model:value="configForm.childSize" :min="1" :max="4000" style="width: 100%" />
        </e-form-item>
        <e-form-item label="子块重叠">
          <e-input-number v-model:value="configForm.childOverlap" :min="0" :max="1000" style="width: 100%" />
        </e-form-item>
        <e-divider>入库与向量化</e-divider>
        <e-form-item label="文档并发数" extra="每个知识库同时处理的文档数，范围 1-10，默认 2">
          <e-input-number v-model:value="configForm.indexConcurrency" :min="1" :max="10" style="width: 100%" />
        </e-form-item>
        <e-form-item label="最大切片数" extra="单个文档最多生成的切片数，0 表示不限">
          <e-input-number v-model:value="configForm.maxChunksPerDocument" :min="0" :max="1000000" style="width: 100%" />
        </e-form-item>
        <e-form-item label="向量化额度" extra="单个文档最多送入向量模型的切片数，0 表示不限">
          <e-input-number v-model:value="configForm.embeddingQuota" :min="0" :max="1000000" style="width: 100%" />
        </e-form-item>
        <e-form-item label="向量批大小">
          <e-input-number v-model:value="configForm.embeddingBatchSize" :min="1" :max="256" style="width: 100%" />
        </e-form-item>
        <e-form-item label="每批 Token 上限" extra="0 表示不按应用层 Token 限制，Provider 自身限制仍有效">
          <e-input-number v-model:value="configForm.embeddingBatchTokens" :min="0" :max="1000000" style="width: 100%" />
        </e-form-item>
        <e-form-item label="向量请求重试次数">
          <e-input-number v-model:value="configForm.embeddingRetryCount" :min="0" :max="10" style="width: 100%" />
        </e-form-item>
        <e-form-item label="向量请求超时（秒）">
          <e-input-number v-model:value="configForm.embeddingRequestTimeout" :min="5" :max="600" style="width: 100%" />
        </e-form-item>
        <e-alert type="info" show-icon message="TopK、RRF、阈值会立即用于检索；父块/子块/重叠需要重新入库后才生效。" />
      </e-form>
      <template #footer>
        <e-button type="primary" :loading="configSaving" @click="saveConfig">保存参数</e-button>
      </template>
    </e-drawer>

 
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue';
import KnowledgeSearch from '@/components/ai/KnowledgeSearch.vue';
import { 
  BookOutlined,
  PlusOutlined, 
  EditOutlined,
  DeleteOutlined,
  EllipsisOutlined, 
} from '@/app/ui/icons';
import { message, Modal } from '@/app/ui/controls';
import { isMutationSuccess } from '@/utils/http';
import {
  getAIKmssPageData,
  getAIKmssDetails,
  addEditAIKmss,
  deleteAIKmss,
  saveKnowledgeRetrieval,
} from '@/api/ai/aikmss';
import KnowledgeBaseAddEdit from '@/components/ai/KnowledgeBaseAddEdit.vue';
import { getUserPermissions } from '@/api/baseapi';
import { hasAgentPermission } from '@/shared/ui/agentWorkspace';
import { knowledgeStatus } from '@/shared/ui/knowledgeWorkspace';

// 搜索参数
const searchParams = reactive({
  name: ''
});

// 分页信息
const pagination = reactive({
  current: 1,
  pageSize: 8, // 与PromptManagement一致
  total: 0
});

// 加载状态
const loading = ref(true);
const loadError = ref('');
const permissions = ref([]);
const canRead = computed(() => hasAgentPermission(permissions.value, 'knowledge.read'));
const canWrite = computed(() => hasAgentPermission(permissions.value, 'knowledge.write'));
const canReadFiles = computed(() => hasAgentPermission(permissions.value, 'file.read'));
const canUpload = computed(() => canWrite.value && canReadFiles.value && hasAgentPermission(permissions.value, 'file.write'));
const savingStrategyId = ref(null);
let alive = true, listSequence = 0, detailSequence = 0, configSequence = 0;
const responseData = (response) => {
  if (response?.code !== 200 || response.isSuccess === false || response.data == null) {
    throw new Error(response?.msg || '接口响应未成功');
  }
  return response.data;
};

// 知识库列表
const knowledgeBaseList = ref([]);

// 模态框相关
const modalVisible = ref(false); 
const modalType = ref('add'); // 'add' 或 'edit'
const currentRecord = ref({});
const strategyOptions = [
  { label: '基础', value: 'basic' },
  { label: '进阶', value: 'advanced' },
  { label: '完整', value: 'full' },
];
const configDrawerVisible = ref(false);
const configSaving = ref(false);
const configTarget = ref(null);
const configForm = ref(null);
const searchVisible = ref(false), searchTarget = ref(null);
const openRecallPage = (kb) => {
  if (!canRead.value) return;
  searchTarget.value = kb;
  searchVisible.value = true;
};


// 获取知识库列表
const loadKnowledgeBaseList = async () => {
  if (modalVisible.value || configSaving.value || savingStrategyId.value != null) return;
  const seq = ++listSequence;
  detailSequence++;
  loading.value = true;
  loadError.value = '';
  permissions.value = [];
  knowledgeBaseList.value = [];
  pagination.total = 0;
  try {
    const rows = responseData(await getUserPermissions());
    if (!alive || seq !== listSequence) return;
    if (!Array.isArray(rows)) throw new Error('权限响应无效');
    permissions.value = rows;
    if (!canRead.value) { knowledgeBaseList.value = []; return; }
    const params = {
      search: searchParams.name,
      page: pagination.current,
      limit: pagination.pageSize
    };
    
    const response = await getAIKmssPageData(params);
    if (!alive || seq !== listSequence) return;
    const data = responseData(response);
    if (!Array.isArray(data.data)) throw new Error('知识库列表响应无效');
    knowledgeBaseList.value = data.data;
    pagination.total = data.total ?? 0;
  } catch (error) {
    if (alive && seq === listSequence) {
      knowledgeBaseList.value = [];
      permissions.value = [];
      configDrawerVisible.value = false;
      loadError.value = `知识库列表加载失败：${error.message || '请检查网络和权限后重新加载'}`;
    }
  } finally {
    if (alive && seq === listSequence) loading.value = false;
  }
};


const defaultConfig = (kb) => ({
  topK: kb?.retrievalConfig?.topK || 8,
  scoreThreshold: kb?.retrievalConfig?.scoreThreshold || 0,
  thresholdVersion: kb?.retrievalConfig?.thresholdVersion || (kb?.retrievalConfig?.scoreThreshold > 0 ? 1 : 2),
  denseThreshold: kb?.retrievalConfig?.denseThreshold ?? 0,
  sparseThreshold: kb?.retrievalConfig?.sparseThreshold ?? 0,
  fusionThreshold: kb?.retrievalConfig?.fusionThreshold ?? 0,
  rerankThreshold: kb?.retrievalConfig?.rerankThreshold ?? 0,
  candidateLimit: kb?.retrievalConfig?.candidateLimit || 48,
  vectorTopK: kb?.retrievalConfig?.vectorTopK || 24,
  bm25TopK: kb?.retrievalConfig?.bm25TopK || 24,
  rrfK: kb?.retrievalConfig?.rrfK || 60,
  finalLimit: kb?.retrievalConfig?.finalLimit || 8,
  rerankTopN: kb?.retrievalConfig?.rerankTopN || 8,
  parentFill: kb?.retrievalConfig?.parentFill !== false,
  parentSize: kb?.retrievalConfig?.parentSize || 2400,
  childSize: kb?.retrievalConfig?.childSize || kb?.maxTokensPerParagraph || 900,
  childOverlap: kb?.retrievalConfig?.childOverlap ?? kb?.overlappingTokens ?? 120,
  indexConcurrency: kb?.retrievalConfig?.indexConcurrency || 2,
  maxChunksPerDocument: kb?.retrievalConfig?.maxChunksPerDocument ?? 0,
  embeddingQuota: kb?.retrievalConfig?.embeddingQuota ?? 0,
  embeddingBatchSize: kb?.retrievalConfig?.embeddingBatchSize || 32,
  embeddingBatchTokens: kb?.retrievalConfig?.embeddingBatchTokens ?? 0,
  embeddingRetryCount: kb?.retrievalConfig?.embeddingRetryCount ?? 2,
  embeddingRequestTimeout: kb?.retrievalConfig?.embeddingRequestTimeout || 60,
});

const changeStrategy = async (kb, strategy) => {
  if (!canWrite.value || loading.value || savingStrategyId.value != null || configDrawerVisible.value || modalVisible.value) return;
  savingStrategyId.value = kb.id;
  try {
    responseData(await saveKnowledgeRetrieval({
      id: kb.id,
      retrievalStrategy: strategy,
      retrievalConfig: kb.retrievalConfig || defaultConfig(kb),
    }));
    if (!alive) return;
    kb.retrievalStrategy = strategy;
    message.success('检索策略已保存');
  } catch (error) {
    message.error(error.message || '保存检索策略失败');
  } finally {
    savingStrategyId.value = null;
  }
};

const openConfigDrawer = (kb) => {
  if (!canWrite.value || loading.value || configSaving.value || modalVisible.value || savingStrategyId.value != null) return;
  configSequence++;
  configTarget.value = kb;
  configForm.value = defaultConfig(kb);
  configDrawerVisible.value = true;
};

const saveConfig = async () => {
  if (!canWrite.value || !configTarget.value || configSaving.value) return;
  const seq = configSequence, target = configTarget.value, config = { ...configForm.value };
  configSaving.value = true;
  try {
    const result = await saveKnowledgeRetrieval({
      id: target.id,
      retrievalStrategy: target.retrievalStrategy || 'full',
      retrievalConfig: config,
    });
    responseData(result);
    if (!alive || seq !== configSequence || target !== configTarget.value) return;
    target.retrievalConfig = result.data?.retrievalConfig || config;
    message.success('检索参数已保存');
    configDrawerVisible.value = false;
    configSaving.value = false;
    await loadKnowledgeBaseList();
  } catch (error) {
    if (alive && seq === configSequence) message.error(error.message || '保存检索参数失败');
  } finally {
    if (alive && seq === configSequence) configSaving.value = false;
  }
};

// 搜索
const handleSearch = () => {
  pagination.current = 1;
  loadKnowledgeBaseList();
};

// 分页变化
const handlePageChange = (page, pageSize) => {
  pagination.current = page;
  pagination.pageSize = pageSize;
  loadKnowledgeBaseList();
};

// 显示添加模态框
const showAddModal = () => {
  if (!canWrite.value || loading.value || modalVisible.value || configDrawerVisible.value) return;
  detailSequence++;
  modalType.value = 'add';
  currentRecord.value = {};
  modalVisible.value = true;
};

// 显示编辑模态框
const handleEdit = async (record) => {
  if (!canRead.value || loading.value || modalVisible.value || configDrawerVisible.value) return;
  const seq = ++detailSequence;
  try {
    const data = responseData(await getAIKmssDetails(record.id));
    if (!alive || seq !== detailSequence) return;
    modalType.value = 'edit';
    currentRecord.value = data;
    modalVisible.value = true;
  } catch (error) {
    if (alive && seq === detailSequence) message.error(error.message || '知识库详情加载失败');
  }
};

// 删除确认
const handleDelete = (record) => {
  if (!canWrite.value) return;
  Modal.confirm({
    title: '确认删除',
    content: `确定要删除知识库"${record.name}"吗？`,
    okText: '确认',
    cancelText: '取消',
    onOk: () => deleteKnowledgeBase(record.id, record.name),
  });
};

// 删除知识库
const deleteKnowledgeBase = async (id, name) => {
  if (!canWrite.value) return;
  try {
    const result = await deleteAIKmss(id);
    if (!isMutationSuccess(result)) {
      message.error(`知识库"${name}"删除未生效`);
      return;
    }
    message.success(`知识库"${name}"删除成功`);
    loadKnowledgeBaseList();
  } catch (error) {
    console.error('删除知识库失败:', error);
    message.error('删除知识库失败: ' + (error.message || '未知错误'));
  }
};
 
 

// 处理模态框确定
const handleModalOk = async (data, closeLoadingCallback) => {
  try {
    if (!canWrite.value) return;
    const result = await addEditAIKmss(data);
    if (isMutationSuccess(result)) {
      message.success(
        currentRecord.value && currentRecord.value.id 
          ? "知识库信息更新成功" 
          : "知识库信息添加成功"
      );
      modalVisible.value = false;
      loadKnowledgeBaseList();
    } else {
      message.error('知识库保存未生效，请保留内容后重试');
    }
  } catch (error) {
    console.error('保存知识库失败:', error);
    message.error('保存知识库失败: ' + (error.message || '未知错误'));
  } finally {
    // 确保无论成功还是失败都调用回调函数来关闭加载状态
    if (typeof closeLoadingCallback === 'function') {
      closeLoadingCallback();
    }
  }
};

// 处理模态框取消
const handleModalCancel = () => {
  detailSequence++;
  modalVisible.value = false;
};
 
 
// 获取状态颜色
const getStatusColor = (status) => knowledgeStatus(status).color;

const formatKbTime = (value) => {
  if (!value) return "尚未更新";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
};

const getStatusText = (status) => knowledgeStatus(status).label;

// 初始化
onMounted(() => {
  loadKnowledgeBaseList();
});
onBeforeUnmount(() => { alive = false; listSequence++; detailSequence++; configSequence++; });
</script>

<style scoped>
.knowledge-intro { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:10px 16px; margin-bottom:12px; }
.knowledge-intro p { margin:0; color:var(--eic-text-secondary); font-size:var(--eic-font-meta); line-height:1.7; }
.header-actions { flex-wrap: wrap; gap: 12px; }
.search-input { width: 250px; }
@media (max-width: 767px) {
  .card-header { align-items: stretch; flex-direction: column; }
  .header-actions, .search-input { width: 100%; }
  .kb-strategy { flex-wrap: wrap; }
}
.management-container {
  padding: 0;
  background-color: transparent;
}

.management-card {
  border-radius: 8px;
  overflow: hidden;
  background: var(--eic-surface);
  border: 1px solid var(--eic-line);
  color: var(--eic-text);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-title {
  display: flex;
  align-items: center;
  font-size: var(--eic-font-section);
  font-weight: 600;
  color: var(--eic-text);
}

.title-icon {
  margin-right: 8px;
  font-size: 18px;
  color: var(--eic-primary);
}

.header-actions {
  display: flex;
  align-items: center;
}

.search-input {
  margin-right: 16px;
}

.knowledge-base-card {
  min-height: 240px;
  display: flex;
  flex-direction: column;
}

.card-title {
  word-break: break-all;
  font-weight: 600;
}

.kb-name {
  font-weight: 600;
  color: var(--eic-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}

.kb-info {
  flex-grow: 1;
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
  gap: 8px;
  width: 100%;
  overflow: hidden;
}

.kb-section {
  margin-bottom: 0;
}

.kb-section.horizontal {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  flex-shrink: 0;
}

.section-label {
  font-weight: 500;
  color: var(--eic-text-secondary);
  white-space: nowrap;
  text-align: left;
  min-width: 50px;
  font-size: var(--eic-font-ui);
}

.section-content {
  color: var(--eic-text);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  text-align: left;
  font-size: var(--eic-font-ui);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  white-space: nowrap;
  margin: 0;
  padding: 0;
}

.card-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.kb-desc {
  min-height: 44px;
  margin: 0;
  color: var(--eic-text-secondary);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.kb-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  padding: 10px 0;
  border-top: 1px solid #f0f0f0;
  border-bottom: 1px solid #f0f0f0;
}
.kb-stat {
  display: flex;
  min-width: 0;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}
.kb-stat:last-child {
  grid-column: 1 / -1;
}
.kb-stat-value {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 18px;
  font-weight: 600;
  color: var(--eic-primary);
}
.kb-stat-label {
  font-size: var(--eic-font-meta);
  color: var(--eic-muted);
}
.kb-meta {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: var(--eic-font-meta);
  color: var(--eic-muted);
}


.kb-strategy {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
.recall-content {
  max-height: 72px;
  overflow: auto;
  white-space: pre-wrap;
}
.eval-summary {
  margin-top: 12px;
  padding: 12px;
  background: var(--eic-surface-muted);
  border-radius: 6px;
}

.pagination-container {
  margin-top: 24px;
  display: flex;
  justify-content: flex-end;
}
</style>
