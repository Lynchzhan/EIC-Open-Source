<template>
  <div class="management-container">
    <e-card class="management-card">
      <template #title>
        <div class="card-header">
          <div class="header-actions">
            <e-input-search
              class="search-input"
              placeholder="搜索提示词..."
              style="width: 250px; margin-right: 16px"
              @search="onSearch"
            />
            <e-button @click="showCatalog" :loading="catalogLoading">
              <template #icon><ImportOutlined /></template>
              内置模板
            </e-button>
            <e-button v-if="canWrite" type="primary" class="add-button" @click="showAddPromptModal">
              <template #icon>
                <PlusOutlined />
              </template>
              添加提示词
            </e-button>
          </div>
        </div>
      </template>

      <div class="cards-container">
        <e-row :gutter="[24, 24]">
          <e-col :xs="24" :sm="12" :md="12" :lg="8" :xl="6" v-for="prompt in promptList" :key="prompt.id">
            <e-card class="prompt-card" hoverable>
              <template #title>
                <div class="card-title">
                  <span class="prompt-name">{{ prompt.name }}</span>
                </div>
              </template>
              <template #extra v-if="canWrite">
                <e-dropdown>
                  <button type="button" class="eic-menu-trigger" aria-label="配置显示列">
                    <EllipsisOutlined />
                  </button>
                  <template #overlay>
                    <e-menu>
                      <e-menu-item @click="showEditPromptModal(prompt)">
                        <edit-outlined /> 编辑
                      </e-menu-item>
                      <e-menu-item @click="showDeleteConfirm(prompt)">
                        <delete-outlined /> 删除
                      </e-menu-item>
                    </e-menu>
                  </template>
                </e-dropdown>
              </template>
              <div @click="showEditPromptModal(prompt)" class="card-content">
                <div class="prompt-info">
                  <div class="prompt-section horizontal">
                    <div class="section-label">提示词:</div>
                    <div class="section-content">{{ prompt.prompt || prompt.content }}</div>
                  </div>
                  <div class="prompt-section horizontal">
                    <div class="section-label">描述:</div>
                    <div class="section-content">{{ prompt.description || prompt.describe || '暂无描述' }}</div>
                  </div>
                  <div class="prompt-section horizontal" v-if="prompt.createUser || prompt.updateUser">
                    <div class="section-label" v-if="prompt.createUser">创建人:</div>
                    <div class="section-content" v-if="prompt.createUser">{{ prompt.createUser }}</div>
                    <div class="section-label" v-if="prompt.updateUser" style="margin-left: 16px;">更新人:</div>
                    <div class="section-content" v-if="prompt.updateUser">{{ prompt.updateUser }}</div>
                  </div>
                </div>
              </div>
            </e-card>
          </e-col>
        </e-row>
        
        <e-empty v-if="promptList.length === 0" description="暂无提示词数据" />
        
        <div class="pagination-container" v-if="promptList.length > 0">
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

    <!-- 添加/编辑提示词模态框 -->
    <e-modal 
      v-model:open="promptModalVisible" 
      :title="promptModalTitle" 
      ok-text="确定"
      cancel-text="取消"
      @ok="handlePromptModalOk"
      :ok-button-props="{ disabled: !canWrite }"
      @cancel="handlePromptModalCancel"
      :confirm-loading="confirmLoading"
      width="600px"
    >
      <e-form :model="promptForm" :label-col="{ span: 6 }" :wrapper-col="{ span: 18 }">
        <e-form-item label="名称" v-bind="validateInfos.name">
          <e-input v-model:value="promptForm.name" :readonly="!canWrite" :maxlength="200" placeholder="请输入提示词名称" />
        </e-form-item>
        <e-form-item label="提示词" v-bind="validateInfos.prompt">
          <e-textarea 
            v-model:value="promptForm.prompt" 
            :readonly="!canWrite"
            :rows="20" 
            placeholder="请输入提示词"
            :maxlength="5000"
            show-count
          />
        </e-form-item>
        <e-form-item label="描述" v-bind="validateInfos.description">
          <e-textarea 
            v-model:value="promptForm.description" 
            :readonly="!canWrite"
            :rows="4" 
            placeholder="请输入描述"
            :maxlength="500"
            show-count
          />
        </e-form-item>
      </e-form>
    </e-modal>
    <e-modal v-model:open="catalogVisible" :title="`内置模板 · ${catalogPreview?.catalogVersion || ''}`"
      width="1050px" :footer="null" :mask-closable="!importLoading" :closable="!importLoading">
      <e-alert message="用户自建、已编辑、重复或删除的模板保留不变。更新仅作用于本次勾选的匹配模板。"
        type="info" show-icon />
      <e-collapse class="catalog-list" v-if="catalogPreview">
        <e-collapse-panel v-for="entry in catalogPreview.entries" :key="entry.templateId" :name="entry.templateId">
          <template #header><span>{{ entry.name }}</span></template>
          <template #extra>
            <e-checkbox :checked="selectedTemplates.includes(entry.templateId)"
              :disabled="!canWrite || !entry.selectable || importLoading" @click.stop
              @change="toggleTemplate(entry.templateId, $event.target.checked)">选择</e-checkbox>
          </template>
          <div class="catalog-entry-status"><e-tag :color="entry.selectable ? 'green' : 'default'">{{ entry.reason }}</e-tag>
            <span>{{ entry.prompt.length }} 字</span></div>
          <p>{{ entry.description }}</p>
          <div class="catalog-comparison">
            <section><h4>当前正文</h4><pre>{{ entry.before?.prompt || '尚无匹配正文' }}</pre>
              <h4>当前描述</h4><p>{{ entry.before?.description || '暂无描述' }}</p></section>
            <section><h4>{{ entry.action === 'create' ? '新增正文' : '候选正文差异' }}</h4>
              <pre class="prompt-diff"><span v-for="(line, index) in entry.diff" :key="index" :class="`diff-${line.type}`">{{ line.text }}{{ '\n' }}</span></pre>
              <h4>候选描述</h4><p>{{ entry.description }}</p></section>
          </div>
        </e-collapse-panel>
      </e-collapse>
      <div class="catalog-footer">
        <span>已选择 {{ selectedTemplates.length }} 项</span>
        <e-button :disabled="importLoading" @click="catalogVisible = false">关闭</e-button>
        <e-button v-if="canWrite" type="primary" :disabled="!selectedTemplates.length" :loading="importLoading" @click="confirmTemplateImport">
          <template #icon><ImportOutlined /></template>导入所选模板
        </e-button>
      </div>
    </e-modal>
  </div>
</template>

<script setup>

import { ref, reactive, onMounted } from "vue";
import {
  MessageOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EllipsisOutlined,
  ImportOutlined,
} from '@/app/ui/icons';
import { message, Modal } from '@/app/ui/controls';
import { Form } from '@/app/ui/controls';
import { isMutationSuccess } from "@/utils/http";
import {
  getAIPromptsPageData,
  addEditAIPrompts,
  deleteAIPrompts,
  getBuiltinPromptCatalog,
  previewBuiltinPromptImport,
  applyBuiltinPromptImport,
} from "@/api/ai/aiPrompts";
import { promptImportPayload, promptTextDiff } from '@/shared/ui/promptCatalog';

const useForm = Form.useForm;
const canWrite = ref(false);
const catalogVisible = ref(false);
const catalogLoading = ref(false);
const importLoading = ref(false);
const catalogPreview = ref(null);
const selectedTemplates = ref([]);

const showCatalog = async () => {
  catalogLoading.value = true;
  selectedTemplates.value = [];
  catalogPreview.value = null;
  try {
    const rights = await getBuiltinPromptCatalog();
    canWrite.value = rights?.data?.canImport === true;
    const response = await previewBuiltinPromptImport();
    if (!isMutationSuccess(response) || !Array.isArray(response.data?.entries)) throw new Error('模板预览未完成');
    catalogPreview.value = { ...response.data, entries: response.data.entries.map((entry) => ({
      ...entry, diff: promptTextDiff(entry.before?.prompt || '', entry.prompt),
    })) };
    catalogVisible.value = true;
  } catch (error) {
    message.error(error.message || '加载模板失败');
  } finally { catalogLoading.value = false; }
};
const toggleTemplate = (id, checked) => {
  selectedTemplates.value = checked ? [...new Set([...selectedTemplates.value, id])] : selectedTemplates.value.filter((key) => key !== id);
};
const confirmTemplateImport = () => {
  if (!canWrite.value || importLoading.value) return;
  let payload;
  try { payload = promptImportPayload(catalogPreview.value, selectedTemplates.value); }
  catch (error) { message.error(error.message); return; }
  const names = catalogPreview.value.entries.filter((entry) => payload.templateIds.includes(entry.templateId)).map((entry) => entry.name);
  Modal.confirm({ title: `确认导入 ${names.length} 项模板`, content: names.join('、'), okText: '确认导入', cancelText: '取消',
    onOk: async () => {
      importLoading.value = true;
      try {
        const response = await applyBuiltinPromptImport(payload);
        if (!isMutationSuccess(response)) throw new Error('模板导入未生效');
        message.success(`已导入 ${response.data.changes.length} 项模板`);
        catalogVisible.value = false;
        selectedTemplates.value = [];
        await loadPromptData();
      } catch (error) {
        catalogPreview.value = null;
        selectedTemplates.value = [];
        message.error(error.message || '模板导入失败，请重新预览');
        throw error;
      } finally { importLoading.value = false; }
    },
  });
};

// 提示词数据
const promptList = ref([]);

// 分页配置
const pagination = reactive({
  current: 1,
  pageSize: 8,
  total: 0,
});

// 模态框状态
const promptModalVisible = ref(false);
const confirmLoading = ref(false);
const promptModalTitle = ref("添加提示词");

// 当前编辑的提示词
const currentPrompt = ref(null);

// 提示词表单
const promptForm = reactive({
  id: "",
  name: "",
  prompt: "",
  description: "",
});

// 表单验证规则
const promptRules = reactive({
  name: [{ required: true, message: "请输入提示词名称" }],
  prompt: [
    { required: true, message: "请输入提示词" },
    { max: 5000, message: "提示词不能超过5000个字符" },
  ],
  description: [
    { required: true, message: "请输入描述" },
    { max: 500, message: "描述不能超过500个字符" },
  ],
});

// 表单验证
const { validate: validatePromptForm, validateInfos } = useForm(
  promptForm,
  promptRules
);

// 搜索关键字
const searchKeyword = ref("");

// 搜索处理
const onSearch = (value) => {
  searchKeyword.value = value;
  pagination.current = 1;
  loadPromptData();
};

// 加载提示词数据
const loadPromptData = async () => {
  try {
    const params = {
      pageNum: pagination.current,
      pageSize: pagination.pageSize,
      searchKey: searchKeyword.value,
    };

    const response = await getAIPromptsPageData(params);
    if (response && response.code === 200 && response.data.data) {
      promptList.value = response.data.data.map((item) => ({
        ...item,
        key: item.id,
      }));
      pagination.total = response.data.total;
    }
  } catch (error) {
    console.error("加载提示词数据失败:", error);
    message.error("加载提示词数据失败: " + (error.message || "未知错误"));
  }
};

// 显示添加提示词模态框
const showAddPromptModal = () => {
  if (!canWrite.value) return;
  promptModalTitle.value = "添加提示词";
  currentPrompt.value = null;
  // 重置表单
  Object.assign(promptForm, {
    id: "",
    name: "",
    prompt: "",
    description: "",
  }); 
  promptModalVisible.value = true;
};

// 显示编辑提示词模态框
const showEditPromptModal = (record) => {
  promptModalTitle.value = '编辑提示词';
  currentPrompt.value = record; 
  // 填充表单数据
  promptForm.id = record.id || '';
  promptForm.name = record.name || '';
  promptForm.prompt = record.prompt || record.content || '';
  promptForm.description = record.description || record.describe || '';
  promptModalVisible.value = true;
};

// 删除提示词确认
const showDeleteConfirm = (record) => {
  if (!canWrite.value) return;
  Modal.confirm({
    title: "确认删除",
    content: `确定要删除提示词"${record.name}"吗？`,
    okText: "确认",
    cancelText: "取消",
    onOk: () => handleDelete(record.id, record.name),
  });
};

// 删除提示词
const handleDelete = async (id, name) => {
  try {
    const response = await deleteAIPrompts(id);
    if (!isMutationSuccess(response)) {
      message.error(`提示词"${name}"删除未生效`);
      return;
    }
    message.success(`提示词"${name}"删除成功`);
    loadPromptData(); // 重新加载数据
  } catch (error) {
    console.error("删除提示词失败:", error);
    message.error("删除提示词失败: " + (error.message || "未知错误"));
  }
};

// 分页变化处理
const handlePageChange = (page, pageSize) => {
  pagination.current = page;
  pagination.pageSize = pageSize;
  loadPromptData();
};

// 提示词模态框确认
const handlePromptModalOk = () => {
  if (!canWrite.value) return;
  validatePromptForm()
    .then(async () => {
      confirmLoading.value = true;
      try {  
        const response = await addEditAIPrompts(currentPrompt.value? {
              id: promptForm.id,
              name: promptForm.name,
              prompt: promptForm.prompt,
              description: promptForm.description,
            }:{
              name: promptForm.name,
              prompt: promptForm.prompt,
              description: promptForm.description,
            }); 
        if (!isMutationSuccess(response)) throw new Error('提示词保存未生效');
        message.success(
          currentPrompt.value ? "提示词信息更新成功" : "提示词信息添加成功"
        );

        promptModalVisible.value = false;
        loadPromptData(); // 重新加载数据
      } catch (error) {
        console.error("保存提示词失败:", error);
        message.error("保存提示词失败: " + (error.message || "未知错误"));
      } finally {
        confirmLoading.value = false;
      }
    })
    .catch((err) => {
      console.log("表单验证失败:", err);
    });
};

// 提示词模态框取消
const handlePromptModalCancel = () => {
  promptModalVisible.value = false;
};

// 组件挂载时的初始化
onMounted(() => { 
  loadPromptData();
  getBuiltinPromptCatalog().then((response) => { canWrite.value = response?.data?.canImport === true; }).catch(() => { canWrite.value = false; });
});
</script>

<style scoped>
.header-actions { flex-wrap: wrap; gap: 8px; }
.catalog-list { margin-top: 16px; max-height: 62vh; overflow: auto; }
.catalog-comparison { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
.catalog-comparison section { min-width: 0; }
.catalog-comparison pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: var(--eic-font-code); line-height: 1.65; max-height: 380px; overflow: auto; border: 1px solid var(--eic-line); padding: 12px; margin: 0; }
.catalog-entry-status, .catalog-footer { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.catalog-footer { justify-content: flex-end; padding-top: 16px; }
.catalog-footer > span { margin-right: auto; }
.diff-added { color: #135f35; background: #f0fff4; }
.diff-removed { color: #982b35; background: #fff1f0; }
@media (max-width: 640px) { .catalog-comparison { grid-template-columns: minmax(0, 1fr); } }
</style>
