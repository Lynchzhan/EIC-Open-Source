<template>
  <div class="management-container">
    <e-card class="management-card">
      
      <div class="layout-container">
        <!-- 左侧部门树 -->
        <div class="left-panel">  
          <div class="org-chart-container">
            <e-spin :spinning="loading">
              <e-directory-tree
                :tree-data="orgTreeData"
                :expandedKeys="expandedKeys"
                :selectedKeys="selectedKeys"
                @expand="onExpand"
                @select="onSelect"
              >
                <template #title="node">
                  <div class="org-node-content">
                    <div class="org-node-info">
                      <span class="org-node-name">{{ node.title }}</span>
                      <span class="org-node-code" v-if="node.code">{{ node.code }}</span>
                      <span class="org-node-desc" v-if="node.description || node.dataRef?.description || node.data?.description">{{ node.description || node.dataRef?.description || node.data?.description }}</span>
                    </div>
                    <div class="org-node-status">
                      <e-tag :color="(node.status ?? node.dataRef?.status ?? node.data?.status) === 1 ? 'green' : 'red'" class="org-status-tag">
                        {{ (node.status ?? node.dataRef?.status ?? node.data?.status) === 1 ? '启用' : '禁用' }}
                      </e-tag>
                    </div>
                    <div class="org-node-actions">
                      <e-button type="link" size="small" @click.stop="showAddDepartmentModal(node.id ?? node.dataRef?.id ?? node.data?.id ?? node.key)" title="添加下级部门">
                        <template #icon>
                          <PlusOutlined />
                        </template>
                      </e-button>
                      <e-button type="link" size="small" @click.stop="showEditDepartmentModalById(node.id ?? node.dataRef?.id ?? node.data?.id ?? node.key)" title="编辑部门">
                        <template #icon>
                          <EditOutlined />
                        </template>
                      </e-button>
                      <e-popconfirm
                        title="确定要删除这个部门吗?"
                        ok-text="确定"
                        cancel-text="取消"
                        @confirm.stop="handleDelete(node.id ?? node.dataRef?.id ?? node.data?.id ?? node.key)"
                      >
                        <e-button type="link" size="small" danger title="删除部门">
                          <template #icon>
                            <DeleteOutlined />
                          </template>
                        </e-button>
                      </e-popconfirm>
                    </div>
                  </div>
                </template>
              </e-directory-tree>
            </e-spin>
          </div>
        </div>
        
        <!-- 右侧用户管理 -->
        <div class="right-panel">
          <UserManagement 
          ref="userManagementRef"
            title="用户"
            :query-params="userQueryParams"
            :auto-load="!!selectedDepartmentId"
          />
        </div>
      </div>
    </e-card>
    
    <!-- 添加/编辑部门模态框 -->
    <e-modal v-model:open="departmentModalVisible" :title="departmentModalTitle" ok-text="确定" cancel-text="取消" @ok="handleDepartmentModalOk" @cancel="handleDepartmentModalCancel" width="500px">
      <e-form :model="departmentForm" :label-col="{ span: 6 }" :wrapper-col="{ span: 18 }">
        <e-form-item label="部门名称" v-bind="validateInfos.name">
          <e-input v-model:value="departmentForm.name" placeholder="请输入部门名称" />
        </e-form-item>
        <e-form-item label="部门编码" v-bind="validateInfos.code">
          <e-input v-model:value="departmentForm.code" placeholder="请输入部门编码" />
        </e-form-item>
        <e-form-item label="上级部门">
          <e-tree-select
            v-model:value="departmentForm.parentId"
            show-search
            placeholder="请选择上级部门"
            :dropdown-style="{ maxHeight: '400px', overflow: 'auto' }
"
            :tree-data="parentDepartments"
            :filterTreeNode="filterTreeNode"
            :getPopupContainer="(trigger) => trigger.parentNode || document.body"
            style="width: 100%"
          />
        </e-form-item>
        <e-form-item label="排序">
          <e-input-number v-model:value="departmentForm.sort" :min="0" :precision="0" style="width: 100%" />
        </e-form-item>
        <e-form-item label="状态">
          <e-radio-group v-model:value="departmentForm.status">
            <e-radio :value="1">启用</e-radio>
            <e-radio :value="-1">禁用</e-radio>
          </e-radio-group>
        </e-form-item>
        <e-form-item label="部门描述">
          <e-textarea v-model:value="departmentForm.description" :rows="3" placeholder="请输入部门描述" />
        </e-form-item>
      </e-form>
    </e-modal>
  </div>
</template> 
<script setup>
/* eslint-disable no-undef */





import { ref, reactive, onMounted, computed } from 'vue';
import { 
  ApartmentOutlined,
  PlusOutlined, 
  DeleteOutlined,
  EditOutlined
} from '@/app/ui/icons';
import { message } from '@/app/ui/controls';
import { Form } from '@/app/ui/controls';
import { getDepartmentTree, addEditDepartment, deleteDepartment } from '@/api/organizational/department';
import UserManagement from '@/components/UserManagement.vue';

const useForm = Form.useForm;
 
// 部门数据
const dataSource = ref([]);
// 组织架构树数据
const orgTreeData = ref([]);
// 树节点展开的key
const expandedKeys = ref([]);
// 树节点选中的key
const selectedKeys = ref([]);
// 加载状态
const loading = ref(false);

// 模态框状态
const departmentModalVisible = ref(false);
const departmentModalTitle = ref('添加部门');

// 当前编辑的部门
const currentDepartment = ref(null);

// 上级部门选项
const parentDepartments = ref([{ value: 0, title: '无上级部门', key: 0 }]);

// 用户管理引用
const userManagementRef = ref();
// 选中的部门ID
const selectedDepartmentId = ref(null);

// 用户查询参数
const userQueryParams = computed(() => {
  return selectedDepartmentId.value ? { Parameter:{departmentId: selectedDepartmentId.value }} : {};
});

// 部门表单
const departmentForm = reactive({
  id: '',
  name: '',
  code: '',
  parentId: 0,
  sort: 0,
  status: 1,
  description: ''
});

// 表单验证规则
const departmentRules = reactive({
  name: [
    { required: true, message: '请输入部门名称' },
    { min: 2, message: '部门名称至少2个字符' }
  ],
  code: [
    { required: true, message: '请输入部门编码' }
  ]
});

// 表单验证
const { validate: validateDepartmentForm, validateInfos } = useForm(departmentForm, departmentRules);
 

// 树节点展开事件
const onExpand = (keys) => {
  expandedKeys.value = keys;
};

// 树节点选择事件
const onSelect = (keys) => {
  selectedKeys.value = keys;
  if (keys && keys.length > 0) {
    selectedDepartmentId.value = keys[0];
  } else {
    selectedDepartmentId.value = null;
  }
};

// 通过ID查找部门数据
const findDepartmentById = (id, data) => {
  if (!data || !Array.isArray(data)) return null;
  
  for (let item of data) {
    if (item && item.id === id) {
      return item;
    }
    if (item && item.children && item.children.length > 0) {
      const found = findDepartmentById(id, item.children);
      if (found) return found;
    }
  }
  return null;
};

// 通过ID显示编辑部门模态框
const showEditDepartmentModalById = (id) => {
  const department = findDepartmentById(id, dataSource.value);
  if (department) {
    showEditDepartmentModal(department);
  }
};

// 加载上级部门数据
const loadParentDepartments = async () => {
  try {
    const response = await getDepartmentTree();
    if (response && response.code == 200 && response.data) {
      // 转换数据格式以适应TreeSelect组件
      const convertToTreeData = (data) => {
        if (!data) return [];
        
        // 递归转换节点
        const traverse = (node) => {
          if (!node || Array.isArray(node) || node.id == null) return null;
          
          const convertedNode = {
            value: String(node.id),
            title: node.code ? `${node.name} - ${node.code}` : node.name,
            key: String(node.id)
          };
          
          // 处理子节点
          if (node.children && node.children.length > 0) {
            convertedNode.children = node.children.map(child => traverse(child)).filter(child => child !== null);
          }
          
          return convertedNode;
        };
        
        const roots = (Array.isArray(data) ? data : [data]).map(traverse).filter(Boolean);
        return [{
          value: 0,
          title: '无上级部门',
          key: 0
        }, ...roots];
      };
      
      parentDepartments.value = convertToTreeData(response.data);
    }
  } catch (error) {
    console.error('加载上级部门数据失败:', error);
    message.error('加载上级部门数据失败: ' + (error.message || '未知错误'));
  }
};

// 转换数据为组织架构树
const convertToOrgTreeData = (data) => {
  if (!data) return [];
  
  const traverse = (node) => {
    if (!node || Array.isArray(node) || node.id == null) return null;
    
    const treeNode = {
      title: node.name,
      key: String(node.id),
      code: node.code || '',
      description: node.description || '',
      status: node.status ?? (node.active ? 1 : -1),
      id: String(node.id),
      children: []
    };
    
    if (node.children && node.children.length > 0) {
      treeNode.children = node.children.map(child => traverse(child)).filter(child => child !== null);
    }
    
    return treeNode;
  };
  
  const roots = Array.isArray(data) ? data : [data];
  return roots.map(traverse).filter(Boolean);
};

// 加载部门数据
const loadDepartmentData = async () => {
  try {
    loading.value = true;
    const response = await getDepartmentTree();
    if (response && response.code == 200 && response.data) {   
      const roots = Array.isArray(response.data) ? response.data : [response.data];
      dataSource.value = roots;
      // 转换为组织架构树数据
      orgTreeData.value = convertToOrgTreeData(roots);
      const collectKeys = (nodes) => (nodes || []).flatMap((node) => [String(node.key ?? node.id), ...collectKeys(node.children)]);
      expandedKeys.value = collectKeys(orgTreeData.value);
      if (expandedKeys.value.length) {
        onExpand(expandedKeys.value);
        onSelect([expandedKeys.value[0]]);
      }
    }
  } catch (error) {
    console.error('加载部门数据失败:', error);
    message.error('加载部门数据失败: ' + (error.message || '未知错误'));
  } finally {
    loading.value = false;
  }
};

// 显示添加部门模态框
const showAddDepartmentModal = (parentId) => {
  departmentModalTitle.value = parentId === 0 ? '添加根部门' : '添加下级部门';
  currentDepartment.value = null;
  // 重置表单
  Object.assign(departmentForm, {
    id: '',
    name: '',
    code: '',
    parentId: parentId,
    sort: 0,
    status: 1,
    description: ''
  });
  departmentModalVisible.value = true;
};

// 显示编辑部门模态框
const showEditDepartmentModal = (record) => {
  departmentModalTitle.value = '编辑部门';
  currentDepartment.value = record;
  // 填充表单数据
  Object.assign(departmentForm, {
    id: record.id,
    name: record.name,
    code: record.code,
    parentId: record.parentId || 0,
    sort: record.sort,
    status: record.status,
    description: record.description
  });
  departmentModalVisible.value = true;
};
 
// 删除部门
const handleDelete = async (id) => {
  try {
    await deleteDepartment(id);
    message.success('部门删除成功');
    loadDepartmentData(); // 重新加载数据
    loadParentDepartments(); // 重新加载上级部门数据
  } catch (error) {
    console.error('删除部门失败:', error);
    message.error('删除部门失败: ' + (error.message || '未知错误'));
  }
};

// 部门模态框确认
const handleDepartmentModalOk = () => {
  validateDepartmentForm().then(async () => {
    try {
     
      
      if (currentDepartment.value) {
         const params = {
        id: departmentForm.id,
        name: departmentForm.name,
        code: departmentForm.code,
        parentId: departmentForm.parentId === 0 ? null : departmentForm.parentId,
        sort: departmentForm.sort,
        status: departmentForm.status,
        description: departmentForm.description
      };
      
      await addEditDepartment(params);
        message.success('部门信息更新成功');
      } else {
         const params = { 
        name: departmentForm.name,
        code: departmentForm.code,
        parentId: departmentForm.parentId === 0 ? null : departmentForm.parentId,
        sort: departmentForm.sort,
        status: departmentForm.status,
        description: departmentForm.description
      };
      
      await addEditDepartment(params);
        message.success('部门信息添加成功');
      }
      
      departmentModalVisible.value = false;
      loadDepartmentData(); // 重新加载数据
      loadParentDepartments(); // 重新加载上级部门数据
    } catch (error) {
      console.error('保存部门失败:', error);
      message.error('保存部门失败: ' + (error.message || '未知错误'));
    }
  }).catch(err => {
    console.log('表单验证失败:', err);
  });
};

// 部门模态框取消
const handleDepartmentModalCancel = () => {
  departmentModalVisible.value = false;
};

// TreeSelect过滤函数
const filterTreeNode = (inputValue, treeNode) => {
  return treeNode.title.toLowerCase().indexOf(inputValue.toLowerCase()) >= 0;
};

// 组件挂载时的初始化
onMounted(() => {
  loadDepartmentData();
  loadParentDepartments();  
});
</script> 
<style scoped>  
.layout-container {
  display: flex;
  height: calc(100vh - 200px);
  gap: 20px;
  background: transparent;
}

.left-panel {
  flex: 0 0 380px;
  min-width: 320px;
  max-width: 480px;
  display: flex;
  flex-direction: column; 
  overflow: auto;
  color: var(--eic-text);
}

.right-panel {
  flex: 8;
  overflow-y: auto;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.panel-header h3 {
  margin: 0;
  color: var(--eic-text);
  font-size: var(--eic-font-section);
  font-weight: 500;
}

.org-chart-container {
  flex: 1;
  padding: 10px 0;
  border-radius: 8px;
  min-height: 500px;
  display: flex;
  flex-direction: column;
}

.org-node-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 8px 0;
}

.org-node-info {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 12px;
}

.org-node-name {
  font-weight: 500;
  color: var(--eic-text);
}

.org-node-code {
  color: var(--eic-muted);
  font-size: var(--eic-font-meta);
  background: rgba(0, 0, 0, 0.04);
  padding: 2px 6px;
  border-radius: 4px;
}

.org-node-desc {
  color: var(--eic-muted);
  font-size: var(--eic-font-meta);
}

.org-node-status {
  margin-right: 16px;
}

.org-status-tag {
  font-size: var(--eic-font-meta);
  border-radius: 8px;
}

.org-node-actions {
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.3s;
}

.org-node-content:hover .org-node-actions,
.org-node-content:focus-within .org-node-actions {
  opacity: 1;
}
@media (max-width: 900px) {
  .layout-container { flex-direction: column; height: auto; }
  .left-panel { flex: auto; min-width: 0; max-width: none; }
  .org-chart-container { min-height: 220px; }
  .org-node-actions { opacity: 1; }
}
@media (hover: none) { .org-node-actions { opacity: 1; } }
</style>
