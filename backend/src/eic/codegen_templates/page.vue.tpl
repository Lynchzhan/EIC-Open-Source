<template>
  <n-card title="__LABEL__管理" :bordered="false">
    <n-space style="margin-bottom:16px">
      <n-input v-model:value="search" placeholder="搜索名称" @keydown.enter="searchRecords" />
      <n-button @click="searchRecords">搜索</n-button>
      <n-button v-if="canWrite" type="primary" @click="edit()">新增</n-button>
    </n-space>
    <n-alert v-if="error" type="error" style="margin-bottom:16px">{{ error }}</n-alert>
    <n-data-table :columns="columns" :data="rows" :loading="loading" :row-key="row => String(row.id)" :pagination="false" :scroll-x="650" />
    <n-pagination style="margin-top:16px" :page="page" :page-size="20" :item-count="total" @update:page="changePage" />
    <n-modal v-model:show="open" preset="card" title="__LABEL__信息" :mask-closable="!saving" :close-on-esc="!saving" :closable="!saving" style="width:min(560px,calc(100vw - 32px))">
      <n-form ref="formRef" :model="form" :rules="rules" label-placement="top">
        <n-form-item label="名称" path="name"><n-input v-model:value="form.name" :maxlength="200" /></n-form-item>
        <n-form-item label="说明"><n-input v-model:value="form.description" type="textarea" :maxlength="500" /></n-form-item>
        __PARENT_FORM__
        <n-form-item label="启用"><n-switch v-model:value="form.active" /></n-form-item>
      </n-form>
      <template #footer><n-space justify="end"><n-button :disabled="saving" @click="open = false">取消</n-button><n-button type="primary" :loading="saving" @click="save">保存</n-button></n-space></template>
    </n-modal>
  </n-card>
</template>
<script setup>
import { computed, h, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { NCard, NSpace, NInput, NButton, NAlert, NDataTable, NPagination, NModal, NForm, NFormItem, NSwitch, NPopconfirm } from 'naive-ui'
import { message } from '@/app/ui/feedback'
import { isMutationSuccess } from '@/utils/http'
import { listRecords, saveRecord, deleteRecord } from '@/api/generated/__SLUG__'
const search = ref(''), rows = ref([]), total = ref(0), page = ref(1), loading = ref(false), error = ref('')
const open = ref(false), saving = ref(false), deleting = ref(''), formRef = ref()
const form = reactive({ id: null, name: '', description: '', active: true __PARENT_DEFAULT__ })
const rules = { name: { required: true, whitespace: true, message: '请输入名称', trigger: ['input', 'blur'] } }
const canWrite = computed(() => {
  try { const p = JSON.parse(localStorage.getItem('UserPermissions') || '[]'); return Array.isArray(p) && (p.includes('*') || p.includes('system.__SLUG__.write')) }
  catch { return false }
})
let revision = 0, alive = true
const columns = [
  { title: '名称', key: 'name' }, { title: '说明', key: 'description' },
  { title: '操作', key: 'actions', render: row => canWrite.value ? h(NSpace, null, { default: () => [
    h(NButton, { size: 'small', onClick: () => edit(row) }, { default: () => '编辑' }),
    h(NPopconfirm, { onPositiveClick: () => remove(row) }, { trigger: () => h(NButton, { size: 'small', type: 'error', loading: deleting.value === String(row.id), disabled: Boolean(deleting.value) }, { default: () => '停用' }), default: () => '确认停用？有下级的部门须先调整下级。' }),
  ] }) : '只读' },
]
async function load() {
  const ticket = ++revision; loading.value = true; error.value = ''
  try {
    const r = await listRecords({ pageNum: page.value, pageSize: 20, searchKey: search.value.trim() })
    if (!alive || ticket !== revision) return
    if (!isMutationSuccess(r) || !Array.isArray(r.data?.data)) throw new Error('列表响应无效')
    rows.value = r.data.data; total.value = r.data.total
  } catch (e) { if (alive && ticket === revision) error.value = e.message || '读取失败' }
  finally { if (alive && ticket === revision) loading.value = false }
}
function searchRecords() { page.value = 1; load() }
function changePage(value) { page.value = value; load() }
function edit(row) {
  Object.assign(form, { id: row?.id == null ? null : String(row.id), name: row?.name || '', description: row?.description || '', active: row?.active ?? true __PARENT_COPY__ })
  formRef.value?.restoreValidation(); open.value = true
}
async function save() {
  if (saving.value || !canWrite.value) return
  try { await formRef.value?.validate() } catch { return }
  saving.value = true
  try {
    const payload = { ...form, name: form.name.trim() __PARENT_PAYLOAD__ }
    if (!isMutationSuccess(await saveRecord(payload))) throw new Error('保存未生效')
    if (!alive) return
    open.value = false; message.success('已保存'); await load()
  } catch (e) { if (alive) message.error(e.message || '保存失败') }
  finally { saving.value = false }
}
async function remove(row) {
  if (deleting.value || !canWrite.value) return false
  deleting.value = String(row.id)
  try {
    if (!isMutationSuccess(await deleteRecord(String(row.id)))) throw new Error('停用未生效')
    if (alive) { page.value = 1; await load() }
  } catch (e) { if (alive) message.error(e.message || '停用失败'); return false }
  finally { deleting.value = '' }
}
onMounted(load)
onBeforeUnmount(() => { alive = false; revision++ })
</script>
