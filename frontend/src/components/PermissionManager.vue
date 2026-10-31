<template>
  <div class="permission-manager">
    <e-spin :spinning="loading">
      <e-alert 
        message="权限配置" 
        description="请选择需要分配给该角色的权限，然后点击保存。" 
        type="info" 
        show-icon 
        style="margin-bottom: 16px;"
      />
      
      <div class="permission-tree-container">
        <e-tree
          v-model:checkedKeys="checkedKeys"
          :tree-data="treeData"
          checkable
          :disabled="!canSave"
          :expandedKeys="expandedKeys"
          :autoExpandParent="autoExpandParent"
          @expand="onExpand"
          @check="onCheck"
        >
          <template #title="{ title, permissionType }">
            <span>
              <span v-if="permissionType === 1">📁 </span>
              <span v-else-if="permissionType === 2">⚙️ </span>
              <span v-else-if="permissionType === 3">📊 </span>
              <span v-else-if="permissionType === 4">🔗 </span>
              {{ title }}
            </span>
          </template>
        </e-tree>
      </div>
      
      <div class="permission-actions" style="margin-top: 16px; text-align: right;">
        <e-button @click="cancel">取消</e-button>
        <e-button type="primary" :disabled="!canSave" :loading="saving" @click="save" style="margin-left: 8px;">保存</e-button>
      </div>
    </e-spin>
  </div>
</template>

<script setup>
/* eslint-disable no-undef */
import { ref, computed, onBeforeUnmount, watch } from 'vue'
import { message } from '@/app/ui/controls'
import { getAllAreaPermissions, editAllAreaPermissions } from '@/api/permission'

// 定义组件属性
const props = defineProps({
  roleId: {
    type: String,
    required: true
  }
})

// 定义组件事件
const emit = defineEmits(['save-success', 'cancel'])

// 加载状态
const loading = ref(false)
const saving = ref(false)
const loadedRoleId = ref(null)
let revision = 0
const canSave = computed(() => Boolean(props.roleId) &&
  loadedRoleId.value === props.roleId && !loading.value && !saving.value)
const ownsRequest = (requestRevision, roleId) =>
  requestRevision === revision && roleId === props.roleId

const resetContext = () => {
  revision += 1
  loadedRoleId.value = null
  treeData.value = []
  checkedKeys.value = []
  expandedKeys.value = []
  autoExpandParent.value = true
  loading.value = false
  saving.value = false
}

// 树形控件相关数据
const treeData = ref([])
const checkedKeys = ref([])
const expandedKeys = ref([])
const autoExpandParent = ref(true)

// 权限类型映射
const permissionTypeMap = {
  '菜单权限': 1,
  '功能权限': 2,
  '数据权限': 3,
  '接口权限': 4
}

// 将API返回的数据转换为树形结构
const convertToTreeData = (data) => {
  const result = []
  
  // 用于存储已选中的权限ID
  const checkedIds = []
  
  // 遍历权限类型
  const groups = Array.isArray(data?.dtos) ? data.dtos : []
  groups.forEach(permissionTypeGroup => {
    const typeTitle = permissionTypeGroup.permissionType || permissionTypeGroup.PermissionType || ''
    const permissionType = permissionTypeMap[typeTitle] || 0
    
    // 为每个权限类型创建根节点
    const typeNode = {
      title: typeTitle,
      key: `type-${typeTitle}`,
      permissionType: permissionType,
      children: []
    }
    
    // 遍历区域
    const areas = Array.isArray(permissionTypeGroup.dtos) ? permissionTypeGroup.dtos : []
    areas.forEach(area => {
      const areaNode = {
        title: area.areaName,
        key: typeNode.key+`area-${area.area}`,
        permissionType: permissionType,
        children: []
      }
      
      // 遍历模块
      const modules = Array.isArray(area.modules) ? area.modules : []
      modules.forEach(module => {
        const moduleNode = {
          title: module.moduleName || module.ModuleName,
          key: typeNode.key+`module-${area.area}-${module.module || module.Module}`,
          permissionType: permissionType,
          children: []
        }
        
        // 遍历操作
        const actions = Array.isArray(module.actions) ? module.actions : []
        actions.forEach(action => {
          const actionNode = {
            title: action.actionName,
            key: action.id,
            permissionType: permissionType
          }
          
          // 如果该权限已被选中，则添加到checkedIds数组中
          if (action.isPermission) {
            checkedIds.push(action.id)
          }
          
          moduleNode.children.push(actionNode)
        })
        
        areaNode.children.push(moduleNode)
      })
      
      typeNode.children.push(areaNode)
    })
    
    result.push(typeNode)
  })
  
  return { tree: result, checkedIds: checkedIds }
}

// 展开节点
const onExpand = (expandedKeysValue) => {
  expandedKeys.value = expandedKeysValue
  autoExpandParent.value = false
}

// 选中节点
const onCheck = (checkedKeysValue) => {
  checkedKeys.value = checkedKeysValue
}

// 取消操作
const cancel = () => {
  resetContext()
  emit('cancel')
}

// 保存权限配置
const save = async () => {
  if (!canSave.value) return
  const roleId = props.roleId
  const requestRevision = revision
  saving.value = true
  try {
    // 过滤掉非叶子节点（只保留具体的操作权限ID）
    const permissionIds = checkedKeys.value.filter(key => 
      typeof key === 'string' &&
      !key.startsWith('type-') &&
      !key.includes('area-') &&
      !key.includes('module-')
    )
    
    const params = {
      roleId,
      permissionIds: permissionIds
    }
    
    const response = await editAllAreaPermissions(params)
    if (!ownsRequest(requestRevision, roleId)) return
    if (response.code === 200) {
      message.success('权限配置保存成功')
      emit('save-success', roleId)
    } else {
      message.error('权限配置保存失败: ' + (response.data?.msg || '未知错误'))
    }
  } catch (error) {
    if (!ownsRequest(requestRevision, roleId)) return
    console.error('保存权限配置失败:', error)
    message.error('保存权限配置失败: ' + (error.message || '未知错误'))
  } finally {
    if (ownsRequest(requestRevision, roleId)) saving.value = false
  }
}

// 加载权限数据
const loadPermissions = async (roleId) => {
  resetContext()
  const requestRevision = revision
  if (!roleId) return
  loading.value = true
  try {
    const response = await getAllAreaPermissions(roleId)
    if (!ownsRequest(requestRevision, roleId)) return
    if (response.code === 200) {
      // 转换数据结构为树形结构
      const convertedData = convertToTreeData(response.data)
      treeData.value = convertedData.tree
      
      // 获取已选中的权限ID
      checkedKeys.value = convertedData.checkedIds || []
      
      // 默认展开所有根节点
      expandedKeys.value = treeData.value.map(item => item.key)
      loadedRoleId.value = roleId
    } else {
      message.error('获取权限数据失败: ' + (response.data?.msg || '未知错误'))
    }
  } catch (error) {
    if (!ownsRequest(requestRevision, roleId)) return
    loadedRoleId.value = null
    treeData.value = []
    checkedKeys.value = []
    expandedKeys.value = []
    console.error('获取权限数据失败:', error)
    message.error('获取权限数据失败: ' + (error.message || '未知错误'))
  } finally {
    if (ownsRequest(requestRevision, roleId)) loading.value = false
  }
}

// 监听角色ID变化
watch(() => props.roleId, (newRoleId) => {
  loadPermissions(newRoleId)
}, { immediate: true, flush: 'sync' })

onBeforeUnmount(resetContext)
</script>

<style scoped>
.permission-manager {
  min-height: 300px;
}

.permission-tree-container {
  border: 1px solid #f0f0f0;
  border-radius: 4px;
  padding: 12px;
  max-height: 400px;
  overflow-y: auto;
}

.permission-actions {
  padding-top: 16px;
  border-top: 1px solid #f0f0f0;
  margin-top: 16px;
}
</style>
