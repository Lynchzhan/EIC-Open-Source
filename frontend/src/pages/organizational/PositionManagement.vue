<template>
  <div class="management-container">
    <e-card class="management-card">
      
      <div class="layout-container">
        <!-- 左侧岗位树 -->
        <div class="left-panel">
          <!-- <div class="panel-header">
            <h3>组织架构</h3>
            <div class="header-actions">
              <e-button type="primary" size="small" @click="showAddPositionModal(0)">
                <template #icon>
                  <PlusOutlined />
                </template>
                添加根岗位
              </e-button>
            </div>
          </div> -->
          
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
                      <e-button type="link" size="small" @click.stop="showAddPositionModal(node.id ?? node.dataRef?.id ?? node.data?.id ?? node.key)" title="添加下级岗位">
                        <template #icon>
                          <PlusOutlined />
                        </template>
                      </e-button>
                      <e-button type="link" size="small" @click.stop="showEditPositionModalById(node.id ?? node.dataRef?.id ?? node.data?.id ?? node.key)" title="编辑岗位">
                        <template #icon>
                          <EditOutlined />
                        </template>
                      </e-button>
                      <e-popconfirm
                        title="确定要删除这个岗位吗?"
                        ok-text="确定"
                        cancel-text="取消"
                        @confirm.stop="handleDelete(node.id ?? node.dataRef?.id ?? node.data?.id ?? node.key)"
                      >
                        <e-button type="link" size="small" danger title="删除岗位">
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
            title="用户管理"
            :query-params="userQueryParams"
            :auto-load="!!selectedPositionId"
          />
        </div>
      </div>
    </e-card>
    
    <!-- 添加/编辑岗位模态框 -->
    <e-modal v-model:open="positionModalVisible" :title="positionModalTitle" ok-text="确定" cancel-text="取消" @ok="handlePositionModalOk" @cancel="handlePositionModalCancel" width="500px">
      <e-form :model="positionForm" :label-col="{ span: 6 }" :wrapper-col="{ span: 18 }">
        <e-form-item label="岗位名称" v-bind="validateInfos.name">
          <e-input v-model:value="positionForm.name" placeholder="请输入岗位名称" />
        </e-form-item>
        <e-form-item label="岗位编码" v-bind="validateInfos.code">
          <e-input v-model:value="positionForm.code" placeholder="请输入岗位编码" />
        </e-form-item>
        <e-form-item label="上级岗位">
          <e-tree-select
            v-model:value="positionForm.parentId"
            show-search
            placeholder="请选择上级岗位"
            :dropdown-style="{ maxHeight: '400px', overflow: 'auto' }
"
            :tree-data="parentPositions"
            :filterTreeNode="filterTreeNode"
            style="width: 100%"
          />
        </e-form-item>
        <e-form-item label="排序">
          <e-input-number v-model:value="positionForm.sort" :min="0" :precision="0" style="width: 100%" />
        </e-form-item>
        <e-form-item label="状态">
          <e-radio-group v-model:value="positionForm.status">
            <e-radio :value="1">启用</e-radio>
            <e-radio :value="-1">禁用</e-radio>
          </e-radio-group>
        </e-form-item>
        <e-form-item label="岗位描述">
          <e-textarea v-model:value="positionForm.description" :rows="3" placeholder="请输入岗位描述" />
        </e-form-item>
      </e-form>
    </e-modal>
  </div>
</template>

<script setup>




import { ref, reactive, onMounted, computed } from 'vue';
import { 
  ApartmentOutlined,
  PlusOutlined, 
  DeleteOutlined,
  EditOutlined
} from '@/app/ui/icons';
import { message } from '@/app/ui/controls';
import { Form } from '@/app/ui/controls';
import { getPositionTree, addEditPosition, deletePosition } from '@/api/organizational/position';
import UserManagement from '@/components/UserManagement.vue';

const useForm = Form.useForm;

// 岗位数据
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
const positionModalVisible = ref(false);
const positionModalTitle = ref('添加岗位');

// 当前编辑的岗位
const currentPosition = ref(null);

// 上级岗位选项
const parentPositions = ref([{ value: 0, title: '无上级岗位', key: 0 }]);

// 用户管理引用
const userManagementRef = ref(null);

// 选中的岗位ID
const selectedPositionId = ref(null);

// 用户查询参数
const userQueryParams = computed(() => {
  return selectedPositionId.value ? { Parameter:{positionId: selectedPositionId.value }} : {};
});

// 岗位表单
const positionForm = reactive({
  id: '',
  name: '',
  code: '',
  parentId: 0,
  sort: 0,
  status: 1,
  description: ''
});

// 表单验证规则
const positionRules = reactive({
  name: [
    { required: true, message: '请输入岗位名称' },
    { min: 2, message: '岗位名称至少2个字符' }
  ],
  code: [
    { required: true, message: '请输入岗位编码' }
  ]
});

// 表单验证
const { validate: validatePositionForm, validateInfos } = useForm(positionForm, positionRules);
 

// 树节点展开事件
const onExpand = (keys) => {
  expandedKeys.value = keys;
};

// 树节点选择事件
const onSelect = (keys) => {
  selectedKeys.value = keys;
  if (keys && keys.length > 0) {
    selectedPositionId.value = keys[0];
  } else {
    selectedPositionId.value = null;
  }
};

// 通过ID查找岗位数据
const findPositionById = (id, data) => {
  if (!data || !Array.isArray(data)) return null;
  
  for (let item of data) {
    if (item && item.id === id) {
      return item;
    }
    if (item && item.children && item.children.length > 0) {
      const found = findPositionById(id, item.children);
      if (found) return found;
    }
  }
  return null;
};

// 通过ID显示编辑岗位模态框
const showEditPositionModalById = (id) => {
  const position = findPositionById(id, dataSource.value);
  if (position) {
    showEditPositionModal(position);
  }
};

// 加载上级岗位数据
const loadParentPositions = async () => {
  try {
    const response = await getPositionTree();
    if (response && response.code == 200 && response.data) {
      // 转换数据格式以适应TreeSelect组件
      const convertToTreeData = (data) => {
        if (!data) return [];
        
        // 递归转换节点
        const traverse = (node) => {
          if (!node || Array.isArray(node) || node.id == null) return null;
          
          const convertedNode = {
            value: node.id,
            title: node.code ? `${node.name} - ${node.code}` : node.name,
            key: node.id
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
          title: '无上级岗位',
          key: 0
        }, ...roots];
      };
      
      parentPositions.value = convertToTreeData(response.data);
    }
  } catch (error) {
    console.error('加载上级岗位数据失败:', error);
    message.error('加载上级岗位数据失败: ' + (error.message || '未知错误'));
  }
};

// 转换数据为组织架构树
const convertToOrgTreeData = (data) => {
  if (!data) return [];
  
  const traverse = (node) => {
    if (!node || Array.isArray(node) || node.id == null) return null;
    
    const treeNode = {
      title: node.name,
      key: node.id,
      code: node.code || '',
      description: node.description || '',
      status: node.status ?? (node.active ? 1 : -1),
      id: node.id,
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

// 加载岗位数据
const loadPositionData = async () => {
  try {
    loading.value = true;
    const response = await getPositionTree();
    if (response && response.code == 200 && response.data) {   
      const roots = Array.isArray(response.data) ? response.data : [response.data];
      dataSource.value = roots;
      // 转换为组织架构树数据
      orgTreeData.value = convertToOrgTreeData(roots);
      
      // 默认展开根节点
      if (roots[0] && roots[0].id) {
        expandedKeys.value = [roots[0].id];
          onExpand(expandedKeys.value);
          onSelect(expandedKeys.value);
      }
    }
  } catch (error) {
    console.error('加载岗位数据失败:', error);
    message.error('加载岗位数据失败: ' + (error.message || '未知错误'));
  } finally {
    loading.value = false;
  }
};

// 显示添加岗位模态框
const showAddPositionModal = (parentId) => {
  positionModalTitle.value = parentId === 0 ? '添加根岗位' : '添加下级岗位';
  currentPosition.value = null;
  // 重置表单
  Object.assign(positionForm, {
    id: '',
    name: '',
    code: '',
    parentId: parentId,
    sort: 0,
    status: 1,
    description: ''
  });
  positionModalVisible.value = true;
};

// 显示编辑岗位模态框
const showEditPositionModal = (record) => {
  positionModalTitle.value = '编辑岗位';
  currentPosition.value = record;
  // 填充表单数据
  Object.assign(positionForm, {
    id: record.id,
    name: record.name,
    code: record.code,
    parentId: record.parentId || 0,
    sort: record.sort,
    status: record.status,
    description: record.description
  });
  positionModalVisible.value = true;
};
 
// 删除岗位
const handleDelete = async (id) => {
  try {
    await deletePosition(id);
    message.success('岗位删除成功');
    loadPositionData(); // 重新加载数据
    loadParentPositions(); // 重新加载上级岗位数据
  } catch (error) {
    console.error('删除岗位失败:', error);
    message.error('删除岗位失败: ' + (error.message || '未知错误'));
  }
};

// 岗位模态框确认
const handlePositionModalOk = () => {
  validatePositionForm().then(async () => {
    try {
     
      
      if (currentPosition.value) {
         const params = {
        id: positionForm.id,
        name: positionForm.name,
        code: positionForm.code,
        parentId: positionForm.parentId === 0 ? null : positionForm.parentId,
        sort: positionForm.sort,
        status: positionForm.status,
        description: positionForm.description
      };
      
      await addEditPosition(params);
        message.success('岗位信息更新成功');
      } else {
         const params = { 
        name: positionForm.name,
        code: positionForm.code,
        parentId: positionForm.parentId === 0 ? null : positionForm.parentId,
        sort: positionForm.sort,
        status: positionForm.status,
        description: positionForm.description
      };
      
      await addEditPosition(params);
        message.success('岗位信息添加成功');
      }
      
      positionModalVisible.value = false;
      loadPositionData(); // 重新加载数据
      loadParentPositions(); // 重新加载上级岗位数据
    } catch (error) {
      console.error('保存岗位失败:', error);
      message.error('保存岗位失败: ' + (error.message || '未知错误'));
    }
  }).catch(err => {
    console.log('表单验证失败:', err);
  });
};

// 岗位模态框取消
const handlePositionModalCancel = () => {
  positionModalVisible.value = false;
};

// TreeSelect过滤函数
const filterTreeNode = (inputValue, treeNode) => {
  return treeNode.title.toLowerCase().indexOf(inputValue.toLowerCase()) >= 0;
};

// 组件挂载时的初始化
onMounted(() => {
  loadPositionData();
  loadParentPositions(); 

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

.org-node-content:hover .org-node-actions {
  opacity: 1;
}
</style>
