<template>
  <div class="code-generator-container">
    <n-card title="业务代码生成器" :bordered="false">
      <n-alert type="info" style="margin-bottom:20px">从已登记业务实体生成 Python CRUD、数据校验、前端 API 和 Naive UI 管理页面。下载后人工合入，不会写入项目、注册路由或迁移数据库。</n-alert>
      <n-alert v-if="error" type="error" style="margin-bottom:16px">{{ error }} <n-button text @click="loadAreas">重新读取</n-button></n-alert>
      <n-form-item label="业务区域"><n-select :value="selectedArea" :options="areas.map(value => ({ label: value, value }))" :loading="loadingAreas" :disabled="!canRead || generateLoading" placeholder="选择业务区域" style="max-width:360px" @update:value="onAreaChange" /></n-form-item>
      <n-data-table :columns="columns" :data="entities" :loading="entitiesLoading" :row-key="row => row.entityName" :checked-row-keys="selectedNames" :pagination="false" :scroll-x="650" @update:checked-row-keys="selectedNames = $event" />
      <n-space class="generator-actions" align="center">
        <n-button type="primary" :loading="generateLoading" :disabled="!canBuild || !selectedNames.length || entitiesLoading" @click="generateCode">下载 {{ selectedNames.length || '' }} 个实体的代码包</n-button>
        <n-button :disabled="generateLoading || !selectedNames.length" @click="selectedNames = []">取消选择</n-button>
        <span class="generator-note">首批支持部门、岗位；不包含账号、密钥、审计和业务数据。</span>
      </n-space>
      <p v-if="!canRead" class="generator-note">没有读取代码生成目录的权限，请联系管理员。</p>
      <p v-else-if="!canBuild" class="generator-note">当前仅可查看目录；下载需要 system.codegen.write 权限。</p>
    </n-card>
  </div>
</template>
<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NAlert, NButton, NCard, NDataTable, NFormItem, NSelect, NSpace } from 'naive-ui'
import { message } from '@/app/ui/feedback'
import { GetAreaNames, GetAreaNameEntityItems, BulidCode } from '@/api/code_generator'
const areas = ref([]), selectedArea = ref(null), entities = ref([]), selectedNames = ref([])
const loadingAreas = ref(false), entitiesLoading = ref(false), generateLoading = ref(false), error = ref('')
const permissions = (() => { try { const p = JSON.parse(localStorage.getItem('UserPermissions') || '[]'); return Array.isArray(p) ? p : [] } catch { return [] } })()
const allowed = code => permissions.includes('*') || permissions.includes(code)
const canRead = allowed('system.codegen.read'), canBuild = canRead && allowed('system.codegen.write')
const columns = computed(() => [
  { type: 'selection', disabled: () => generateLoading.value || !canBuild },
  { title: '实体', key: 'entityName' }, { title: '说明', key: 'description' },
  { title: '字段', key: 'fields', render: row => row.fields.join('、') },
])
let revision = 0, alive = true
async function loadAreas() {
  if (!canRead || loadingAreas.value || generateLoading.value) return
  loadingAreas.value = true; error.value = ''
  try {
    const r = await GetAreaNames()
    if (!alive) return
    if (r.code !== 200 || r.isSuccess === false || !Array.isArray(r.data)) throw new Error('区域响应无效')
    areas.value = r.data
    await onAreaChange(areas.value[0] || null)
  } catch (e) { if (alive) error.value = e.message || '获取区域失败' }
  finally { if (alive) loadingAreas.value = false }
}
async function onAreaChange(value) {
  const ticket = ++revision
  selectedArea.value = value; selectedNames.value = []; entities.value = []; error.value = ''
  if (!value) { entitiesLoading.value = false; return }
  entitiesLoading.value = true
  try {
    const r = await GetAreaNameEntityItems(value)
    if (!alive || ticket !== revision) return
    if (r.code !== 200 || r.isSuccess === false || !Array.isArray(r.data) || r.data.some(row => !row.entityName || !Array.isArray(row.fields))) throw new Error('实体目录响应无效')
    entities.value = r.data
  } catch (e) { if (alive && ticket === revision) error.value = e.message || '获取实体失败' }
  finally { if (alive && ticket === revision) entitiesLoading.value = false }
}
async function generateCode() {
  if (!canBuild || generateLoading.value || entitiesLoading.value || !selectedNames.value.length) return
  const names = [...selectedNames.value]
  generateLoading.value = true
  let url
  try {
    const blob = await BulidCode(names)
    if (!alive) return
    if (!(blob instanceof Blob) || blob.size === 0 || !blob.type.includes('zip')) throw new Error('未收到有效代码包')
    url = URL.createObjectURL(blob)
    const link = document.createElement('a'); link.href = url; link.download = 'eic-generated-crud.zip'
    document.body.append(link); link.click(); link.remove()
    // 留出浏览器接管下载的时间，避免立即释放导致下载中断。
    const ownedUrl = url; window.setTimeout(() => URL.revokeObjectURL(ownedUrl), 60000); url = undefined
    message.success('代码包已交给浏览器下载；请先审查再合入')
  } catch (e) { if (alive) message.error(e.message || '生成失败，已保留选择') }
  finally { if (url) URL.revokeObjectURL(url); if (alive) generateLoading.value = false }
}
onMounted(loadAreas)
onBeforeUnmount(() => { alive = false; revision++ })
</script>
<style scoped>
.code-generator-container { padding:0; min-width:0; }
.generator-actions { margin-top:20px; }
.generator-note { color:var(--eic-muted); font-size:var(--eic-font-meta); }
@media(max-width:600px) { .code-generator-container { padding:0; } }
</style>
