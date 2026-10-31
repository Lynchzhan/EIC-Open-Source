<template>
  <div class="management-container">
    <section class="management-surface">
        <div class="card-header">
          <div class="header-actions">
            <e-input-search
              class="search-input"
              placeholder="搜索技能工具..."
              style="width: 250px; margin-right: 16px"
              @search="onSearch"
            />
            <e-button v-if="filterType !== '3' && canWrite" type="primary" class="add-button" @click="showAddModal">
              <template #icon>
                <PlusOutlined />
              </template>
              添加技能工具
            </e-button>
          </div>
        </div>
      <e-tabs v-model:value="filterType" size="large" class="resource-tabs" @update:value="onFilterChange">
        <e-tab-pane key="1" name="1" tab="Tool" />
        <e-tab-pane key="2" name="2" tab="Skill" />
        <e-tab-pane key="3" name="3" tab="MCP" />
      </e-tabs>
      <SkillExecutionPanel ref="executionPanel" :type="Number(filterType)" @changed="loadData" @capabilities="value => canWrite = Boolean(value.canWrite)" />
      <e-alert v-if="filterType === '3'" type="info" show-icon message="MCP 实际服务连接、启停及工具授权名单请使用上方 MCP 配置；下方旧条目仅为配置元数据，不授予自动调用权限。" />
      <div class="cards-container">
        <e-row :gutter="[24, 24]">
          <e-col :xs="24" :sm="12" :md="12" :lg="8" :xl="6" v-for="item in dataList" :key="item.id">
            <e-card class="model-card" hoverable>
              <template #title>
                <div class="card-title">
                  <span class="model-name">{{ item.name }}</span>
                  <e-tag :color="item.activeStatus === 1 ? 'green' : 'red'" style="margin-left: 8px">
                    {{ item.activeStatus === 1 ? '启用' : '禁用' }}
                  </e-tag>
                  <e-tag v-if="item.isSystem" color="blue" style="margin-left: 4px">系统内置</e-tag>
                  <e-tag v-if="item.executionMode === 'declarative-v1'">声明式 Skill</e-tag>
                  <e-tag v-else-if="!item.builtinKey">配置元数据</e-tag>
                </div>
              </template>
              <template #extra>
                <e-dropdown>
                  <button type="button" class="eic-menu-trigger" aria-label="配置显示列">
                    <EllipsisOutlined />
                  </button>
                  <template #overlay>
                    <e-menu>
                      <e-menu-item @click="showViewModal(item)">
                        <eye-outlined /> 预览
                      </e-menu-item>
                      <e-menu-item @click="showEditModal(item)" :disabled="item.isSystem || !canWrite">
                        <edit-outlined /> 编辑
                      </e-menu-item>
                      <e-menu-item @click="showDeleteConfirm(item)" :disabled="item.isSystem || !canWrite">
                        <delete-outlined /> 删除
                      </e-menu-item>
                    </e-menu>
                  </template>
                </e-dropdown>
              </template>
              <div @click="showEditModal(item)" class="card-content">
                <div class="model-info horizontal-layout">
                  <div class="info-item horizontal">
                    <span class="info-label">类型:</span>
                    <span class="info-value">{{ getSkillToolTypeName(item.skillToolType) }}</span>
                  </div>
                  <div class="info-item horizontal">
                    <span class="info-label">方法:</span>
                    <span class="info-value">{{ item.classMethod }}</span>
                  </div>
                </div>
                <div class="model-info horizontal-layout">
                  <div class="info-item horizontal">
                    <span class="info-label">描述:</span>
                    <span class="info-value">{{ item.description }}</span>
                  </div>
                </div>
                <div class="model-info horizontal-layout" v-if="item.createUser || item.updateUser">
                  <div class="info-item horizontal">
                    <span class="info-label" v-if="item.createUser">创建人:</span>
                    <span class="info-value" v-if="item.createUser">{{ item.createUser }}</span>
                    <span class="info-label" v-if="item.updateUser" style="margin-left: 16px;">更新人:</span>
                    <span class="info-value" v-if="item.updateUser">{{ item.updateUser }}</span>
                  </div>
                </div>
              </div>
            </e-card>
          </e-col>
        </e-row>

        <e-empty v-if="dataList.length === 0" description="暂无技能工具数据" />

        <div class="pagination-container" v-if="dataList.length > 0">
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
    </section>

    <e-modal
      v-model:open="modalVisible"
      :title="modalTitle"
      @ok="handleModalOk"
      @cancel="handleModalCancel"
      :confirm-loading="confirmLoading"
      width="600px"
    >
      <e-form :model="form" :label-col="{ span: 6 }" :wrapper-col="{ span: 18 }">
        <e-form-item label="名称" v-bind="validateInfos.name">
          <e-input v-model:value="form.name" placeholder="请输入名称" />
        </e-form-item>
        <e-form-item label="技能工具类型" v-bind="validateInfos.skillToolType">
          <e-select v-model:value="form.skillToolType" placeholder="请选择技能工具类型">
            <e-select-option :value="1">Tool</e-select-option>
            <e-select-option :value="2">Skill</e-select-option>
            <e-select-option :value="3" disabled>MCP（请使用上方连接配置）</e-select-option>
          </e-select>
        </e-form-item>
        <e-form-item label="方法" v-bind="validateInfos.classMethod">
          <e-input v-model:value="form.classMethod" placeholder="请输入方法" />
        </e-form-item>
        <e-form-item v-if="form.skillToolType === 2" label="技能文件" v-bind="validateInfos.skillFile">
          <e-alert type="info" show-icon message="上传 declarative-v1 声明包：ZIP 根目录必须为 SKILL.md，可选 manifest.yaml；无外层目录、脚本或附件。最多 128 KiB，每份文档最多 64 KiB。执行模式由服务解析文件确认。" />
          <FileUpload
            v-if="modalVisible"
            :key="uploadEpoch"
            :upload-epoch="uploadEpoch"
            business="AISkillToolManagement"
            :keyValue="form.id"
            sign="SkillZip"
            accept=".zip"
            :multiple="false"
            :maxCount="1"
            :initialFiles="form.skillFile ? [form.skillFile] : []"
            uploadButtonText="上传声明式 Skill ZIP"
            @upload-success="onFileUploadSuccess"
            @upload-error="onFileUploadError"
            @zip-updated="onZipUpdated"
          />
          <p>SKILL.md 使用 YAML front matter 声明 name、description、version、enabled、allowed_tools、permissions，可声明 input_schema 和 steps。包内 permissions 不授予权限，底层工具仍需单独授权；仅指令包不生成模型回答。</p>
          <p>保存后可在上方声明式 Skill 行输入 JSON 参数并运行或启停。多步失败可能已有副作用，不保证跨工具原子事务。</p>
        </e-form-item>
        <e-form-item v-if="form.skillToolType === 3" label="Mcp地址" v-bind="validateInfos.mcpUrl">
          <e-input v-model:value="form.mcpUrl" placeholder="请输入Mcp地址" />
        </e-form-item>
        <e-form-item v-if="form.skillToolType === 3" label="Mcp类型" v-bind="validateInfos.mcpType">
          <e-select v-model:value="form.mcpType" placeholder="请选择Mcp类型">
            <e-select-option value="https">https</e-select-option>
            <e-select-option value="sse">sse</e-select-option>
            <e-select-option value="stdio">stdio</e-select-option>
          </e-select>
        </e-form-item>
        <e-form-item v-if="form.skillToolType === 3" label="McpHeaders" v-bind="validateInfos.mcpHeaders">
          <e-textarea v-model:value="form.mcpHeaders" :rows="3" placeholder="键值对Json格式，例如: {&quot;Authorization&quot;: &quot;Bearer xxx&quot;}" />
        </e-form-item>
        <e-form-item v-if="form.skillToolType === 3 && form.mcpType === 'stdio'" label="McpCommand" v-bind="validateInfos.mcpCommand">
          <e-input v-model:value="form.mcpCommand" placeholder="请输入McpCommand" />
        </e-form-item>
        <e-form-item v-if="form.skillToolType === 3 && form.mcpType === 'stdio'" label="McpArguments">
          <e-input v-model:value="form.mcpArguments" placeholder=",分隔，例如: arg1,arg2" />
        </e-form-item>
        <e-form-item v-if="form.skillToolType === 3 && form.mcpType === 'stdio'" label="McpEnvironment">
          <e-textarea v-model:value="form.mcpEnvironment" :rows="3" placeholder="键值对Json格式" />
        </e-form-item>
        <e-form-item label="描述">
          <e-textarea v-model:value="form.description" :rows="4" placeholder="请输入描述" :maxlength="500" show-count />
        </e-form-item>
        <e-form-item label="启用状态" v-bind="validateInfos.activeStatus">
          <e-select v-model:value="form.activeStatus" placeholder="请选择启用状态">
            <e-select-option :value="1">启用</e-select-option>
            <e-select-option :value="0">禁用</e-select-option>
          </e-select>
        </e-form-item>
      </e-form>
    </e-modal>

    <e-modal
      v-model:open="viewModalVisible"
      title="预览技能工具"
      :footer="null"
      width="600px"
    >
      <e-descriptions :column="1" bordered>
        <e-descriptions-item label="名称">{{ viewItem?.name }}</e-descriptions-item>
        <e-descriptions-item label="类型">{{ getSkillToolTypeName(viewItem?.skillToolType) }}</e-descriptions-item>
        <e-descriptions-item label="方法">{{ viewItem?.classMethod }}</e-descriptions-item>
        <e-descriptions-item label="描述">{{ viewItem?.description }}</e-descriptions-item>
        <e-descriptions-item label="启用状态">
          <e-tag :color="viewItem?.activeStatus === 1 ? 'green' : 'red'">
            {{ viewItem?.activeStatus === 1 ? '启用' : '禁用' }}
          </e-tag>
        </e-descriptions-item>
        <e-descriptions-item label="系统内置">
          <e-tag :color="viewItem?.isSystem ? 'blue' : 'default'">
            {{ viewItem?.isSystem ? '是' : '否' }}
          </e-tag>
        </e-descriptions-item>
        <e-descriptions-item label="技能文件">
          <a v-if="viewItem?.skillFile" href="#" @click.prevent="downloadSkillFile(viewItem.skillFile)">
            {{ viewItem?.skillFile.name }}
          </a>
          <span v-else>无</span>
        </e-descriptions-item>
        <e-descriptions-item label="Mcp地址" v-if="viewItem?.skillToolType === 3">{{ viewItem?.mcpUrl || '无' }}</e-descriptions-item>
        <e-descriptions-item label="Mcp类型" v-if="viewItem?.skillToolType === 3">{{ viewItem?.mcpType || '无' }}</e-descriptions-item>
        <e-descriptions-item label="McpHeaders" v-if="viewItem?.skillToolType === 3">{{ viewItem?.mcpHeaders || '无' }}</e-descriptions-item>
        <e-descriptions-item label="McpCommand" v-if="viewItem?.skillToolType === 3 && viewItem?.mcpType === 'stdio'">{{ viewItem?.mcpCommand || '无' }}</e-descriptions-item>
        <e-descriptions-item label="McpArguments" v-if="viewItem?.skillToolType === 3 && viewItem?.mcpType === 'stdio'">{{ viewItem?.mcpArguments || '无' }}</e-descriptions-item>
        <e-descriptions-item label="McpEnvironment" v-if="viewItem?.skillToolType === 3 && viewItem?.mcpType === 'stdio'">{{ viewItem?.mcpEnvironment || '无' }}</e-descriptions-item>
      </e-descriptions>
    </e-modal>
  </div>
</template>

<script setup>

import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from "vue";
import {
  ToolOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EllipsisOutlined,
  EyeOutlined,
} from '@/app/ui/icons';
import { message, Modal } from '@/app/ui/controls';
import { Form } from '@/app/ui/controls';
import { isMutationSuccess } from "@/utils/http";
import {
  getAISkillToolManagementPageData, 
  addEditAISkillToolManagement,
  deleteAISkillToolManagement,
} from "@/api/ai/aiskilltoolManagement";
import { GetSnowflakeId } from "@/api/baseapi";
import FileUpload from "@/components/FileUpload.vue";
import SkillExecutionPanel from "@/components/SkillExecutionPanel.vue";
import { loadPrivateFile, savePrivateBlob } from '@/utils/privateFiles';
import { getFileInfoById } from "@/api/file";

const useForm = Form.useForm;

const dataList = ref([]);
const canWrite = ref(false);
const executionPanel = ref(null);

const pagination = reactive({
  current: 1,
  pageSize: 8,
  total: 0,
});

const modalVisible = ref(false);
const confirmLoading = ref(false);
const modalTitle = ref("添加技能工具");
const uploadEpoch = ref(0);
let disposed = false;
watch(modalVisible, (open) => {
  if (!open) uploadEpoch.value++;
}, { flush: "sync" });
onBeforeUnmount(() => {
  disposed = true;
  uploadEpoch.value++;
});

const currentRecord = ref(null);

const viewModalVisible = ref(false);
const viewItem = ref(null);

const form = reactive({
  id: "",
  name: "",
  classMethod: "",
  description: "",
  activeStatus: 1,
  skillToolType: undefined,
  skillFile: null,
  mcpUrl: "",
  mcpType: "",
  mcpHeaders: "",
  mcpCommand: "",
  mcpArguments: "",
  mcpEnvironment: "",
});

const validateSkillFile = (rule, value) => {
  if (form.skillToolType !== 2) {
    return Promise.resolve();
  }
  if (!value || !value.id) {
    return Promise.reject("请上传技能压缩包");
  }
  return Promise.resolve();
};

// 方法名仍要求英文；显示名称允许中文
const validateMethodName = (rule, value) => {
  if (form.skillToolType !== 1) return Promise.resolve();
  if (!value) return Promise.resolve();
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(value)) {
    return Promise.reject("方法名需使用英文、数字或下划线");
  }
  return Promise.resolve();
};

const validateSkillFileName = (rule, value) => {
  if (form.skillToolType !== 2) return Promise.resolve();
  if (!value) return Promise.resolve();
  if (!form.name) return Promise.resolve();
  if (!/\.zip$/i.test(value.name || '')) return Promise.reject('请上传 ZIP 声明包');
  if (value.size > 131072) return Promise.reject('声明包不得超过 128 KiB');
  return Promise.resolve();
};

const rules = computed(() => ({
  name: [
    { required: true, message: "请输入名称" },
    
  ],
  classMethod: form.skillToolType === 1 ? [{ required: true, message: "请输入方法" }, { validator: validateMethodName, trigger: "change" }] : [],
  skillToolType: [{ required: true, message: "请选择技能工具类型" }],
  activeStatus: [{ required: true, message: "请选择启用状态" }],
  skillFile: form.skillToolType === 2 ? [
    { required: true, validator: validateSkillFile, trigger: "change" },
    { validator: validateSkillFileName, trigger: "change" },
  ] : [],
  mcpUrl: (form.skillToolType === 3 && form.mcpType !== 'stdio') ? [{ required: true, message: "请输入Mcp地址" }] : [],
  mcpType: form.skillToolType === 3 ? [{ required: true, message: "请选择Mcp类型" }] : [],
  mcpCommand: (form.skillToolType === 3 && form.mcpType === 'stdio') ? [{ required: true, message: "请输入McpCommand" }] : [],
}));

const { validate: validateForm, validateInfos, clearValidate } = useForm(form, rules);

watch(() => form.skillToolType, () => {
  uploadEpoch.value++;
  clearValidate(["classMethod", "skillFile", "mcpUrl", "mcpType", "mcpHeaders", "mcpCommand", "mcpArguments", "mcpEnvironment"]);
}, { flush: "sync" });

// Mcp类型切换时清空stdio相关验证
watch(() => form.mcpType, () => {
  clearValidate(["mcpCommand", "mcpArguments", "mcpEnvironment"]);
});

// 名称变化时重新校验技能文件
watch(() => form.name, () => {
  if (form.skillToolType === 2 && form.skillFile) {
    clearValidate("skillFile");
  }
});

const searchKeyword = ref("");
const filterType = ref('2');

const getSkillToolTypeName = (type) => {
  const types = { 1: "Tool", 2: "Skill", 3: "MCP" };
  return Object.hasOwn(types, type) ? types[type] : "未知";
};

const onSearch = (value) => {
  searchKeyword.value = value;
  pagination.current = 1;
  loadData();
};

const onFilterChange = () => {
  pagination.current = 1;
  loadData();
};

let loadVersion = 0;
const loadData = async () => {
  const version = ++loadVersion;
  try {
    const params = {
      pageNum: pagination.current,
      pageSize: pagination.pageSize,
      searchKey: searchKeyword.value,
      parameter: Number(filterType.value || 0),
      skillToolType: Number(filterType.value || 0),
    };

    const response = await getAISkillToolManagementPageData(params);
    if (version !== loadVersion) return;
    if (response && response.code === 200 && response.data.data) {
      dataList.value = response.data.data.map((item) => ({
        ...item,
        key: item.id,
      }));
      pagination.total = response.data.total;
      if (dataList.value.length === 0 && pagination.current > 1) {
        pagination.current -= 1;
        loadData();
      }
    }
  } catch (error) {
    if (version !== loadVersion) return;
    dataList.value = [];
    pagination.total = 0;
    console.error("加载数据失败:", error);
    message.error("加载数据失败: " + (error.message || "未知错误"));
  }
};

const showAddModal = async () => {
  if (!canWrite.value || filterType.value === '3') return;
  uploadEpoch.value++;
  modalVisible.value = false;
  const openingEpoch = uploadEpoch.value;
  modalTitle.value = "添加技能工具";
  currentRecord.value = null;
  try {
    const snowflakeId = await GetSnowflakeId();
    if (disposed || openingEpoch !== uploadEpoch.value) return;
    Object.assign(form, {
      id: snowflakeId.data,
      name: "",
      classMethod: "",
      description: "",
      activeStatus: 1,
      skillToolType: Number(filterType.value),
      skillFile: null,
      mcpUrl: "",
      mcpType: "",
      mcpHeaders: "",
      mcpCommand: "",
      mcpArguments: "",
      mcpEnvironment: "",
    });
  } catch (error) {
    if (disposed || openingEpoch !== uploadEpoch.value) return;
    console.error("获取ID失败:", error);
    Object.assign(form, {
      id: "",
      name: "",
      classMethod: "",
      description: "",
      activeStatus: 1,
      skillToolType: Number(filterType.value),
      skillFile: null,
      mcpUrl: "",
      mcpType: "",
      mcpHeaders: "",
      mcpCommand: "",
      mcpArguments: "",
      mcpEnvironment: "",
    });
  }
  modalVisible.value = true;
};

const acceptUpload = (data) => {
  const context = data?.uploadContext;
  return !disposed && modalVisible.value && form.skillToolType === 2
    && context?.uploadEpoch === uploadEpoch.value
    && context.business === "AISkillToolManagement" && context.sign === "SkillZip"
    && String(context.keyValue) === String(form.id);
};

const onFileUploadSuccess = (data) => {
  if (!acceptUpload(data)) return;
  form.skillFile = {
    id: data.fileId,
    name: data.fileName,
    path: data.path || "",
    url: data.url || "",
    size: data.fileSize || 0
  };
  message.success("技能文件上传成功");
};

const onFileUploadError = (data) => {
  if (!acceptUpload(data)) return;
  console.error("文件上传失败:", data.error);
  message.error("文件上传失败: " + (data.error?.message || "未知错误"));
};

const onZipUpdated = (data) => {
  if (!acceptUpload(data)) return;
  form.skillFile = {
    id: data.file.id,
    name: data.file.name,
    path: data.file.path || "",
    url: data.file.url || "",
    size: data.file.size || 0
  };
  message.success("技能文件更新成功");
};

const showViewModal = (record) => {
  viewItem.value = record;
  viewModalVisible.value = true;
};

const showEditModal = (record) => {
  if (record.isSystem || !canWrite.value) return;
  uploadEpoch.value++;
  modalTitle.value = "编辑技能工具";
  currentRecord.value = record;
  form.id = record.id || "";
  form.name = record.name || "";
  form.classMethod = record.classMethod || "";
  form.description = record.description || "";
  form.activeStatus = record.activeStatus !== undefined ? record.activeStatus : 1;
  form.skillToolType = record.skillToolType !== undefined ? record.skillToolType : undefined;
  form.skillFile = record.skillFile || null;
  form.mcpUrl = record.mcpUrl || "";
  form.mcpType = record.mcpType || "";
  form.mcpHeaders = record.mcpHeaders || "";
  form.mcpCommand = record.mcpCommand || "";
  form.mcpArguments = record.mcpArguments || "";
  form.mcpEnvironment = record.mcpEnvironment || "";
  modalVisible.value = true;
};

const showDeleteConfirm = (record) => {
  if (record.isSystem) {
    message.warning("系统内置工具不允许删除");
    return;
  }
  Modal.confirm({
    title: "确认删除",
    content: `确定要删除技能工具"${record.name}"吗？`,
    okText: "确认",
    cancelText: "取消",
    onOk: () => handleDelete(record.id, record.name),
  });
};

const handleDelete = async (id, name) => {
  try {
    const response = await deleteAISkillToolManagement(id);
    if (!isMutationSuccess(response)) {
      message.error(`技能工具"${name}"删除未生效`);
      return;
    }
    message.success(`技能工具"${name}"删除成功`);
    loadData();
  } catch (error) {
    console.error("删除失败:", error);
    message.error("删除失败: " + (error.message || "未知错误"));
  }
};

const handlePageChange = (page, pageSize) => {
  pagination.current = page;
  pagination.pageSize = pageSize;
  loadData();
};

const handleModalOk = () => {
  if (!canWrite.value || confirmLoading.value) return;
  validateForm()
    .then(async () => {
      confirmLoading.value = true;
      try {
        let shareSkillFile = false;
        if (form.skillFile?.id) {
          const fileInfo = await getFileInfoById(form.skillFile.id);
          if (fileInfo.data?.accessMode === "private") {
            const sharingAccepted = await new Promise((resolve) => {
              Modal.confirm({
                title: "确认共享技能文件",
                content: "此技能属于租户共享资源。保存将使该文件可被同租户具有文件读取权限的用户读取，文件删除仍仅限所有者。",
                onOk: () => resolve(true),
                onCancel: () => resolve(false),
              });
            });
            if (!sharingAccepted) return;
            shareSkillFile = true;
          }
        }
        const response = await addEditAISkillToolManagement(currentRecord.value ? {
          id: form.id,
          name: form.name,
          classMethod: form.classMethod,
          description: form.description,
          activeStatus: form.activeStatus,
          skillToolType: form.skillToolType,
          shareSkillFile,
          fileId: form.skillFile?.id || null,
          skillFile: form.skillFile ? { id: form.skillFile.id } : null,
          mcpUrl: form.mcpUrl,
          mcpType: form.mcpType,
          mcpHeaders: form.mcpHeaders,
          mcpCommand: form.mcpCommand,
          mcpArguments: form.mcpArguments,
          mcpEnvironment: form.mcpEnvironment,
        } : {
          id: form.id,
          name: form.name,
          classMethod: form.classMethod,
          description: form.description,
          activeStatus: form.activeStatus,
          skillToolType: form.skillToolType,
          shareSkillFile,
          fileId: form.skillFile?.id || null,
          skillFile: form.skillFile ? { id: form.skillFile.id } : null,
          mcpUrl: form.mcpUrl,
          mcpType: form.mcpType,
          mcpHeaders: form.mcpHeaders,
          mcpCommand: form.mcpCommand,
          mcpArguments: form.mcpArguments,
          mcpEnvironment: form.mcpEnvironment,
        });

        if (!isMutationSuccess(response)) throw new Error('技能保存未生效');
        message.success(currentRecord.value ? "更新成功" : "添加成功");
        modalVisible.value = false;
        loadData();
        executionPanel.value?.refresh();
      } catch (error) {
        console.error("保存失败:", error);
        message.error("保存失败: " + (error.message || "未知错误"));
      } finally {
        confirmLoading.value = false;
      }
    })
    .catch((err) => {
      console.log("表单验证失败:", err);
    });
};

const handleModalCancel = () => {
  modalVisible.value = false;
};

onMounted(() => {
  loadData();
});
</script>

<style scoped>
.management-surface { padding: 20px; min-width: 0; }
.header-title h2 { font-size: var(--eic-font-section); margin: 0; }
.model-card { border-radius: 8px; }
.model-name { overflow-wrap: anywhere; }
@media (max-width: 600px) {
  .management-surface { padding: 12px; }
  .card-header { align-items: flex-start; flex-direction: column; gap: 12px; }
  .header-actions { display: flex; flex-wrap: wrap; gap: 8px; width: 100%; }
  .header-actions .search-input { width: 100% !important; margin-right: 0 !important; }
  .card-title { flex-wrap: wrap; }
}
</style>
