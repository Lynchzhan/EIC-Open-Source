<template>
  <e-card v-if="isAdmin" class="registration-codes" title="注册邀请管理">
    <template #extra>
      <e-space wrap>
        <e-button :loading="loading" @click="loadCodes">刷新</e-button>
        <e-button type="primary" :loading="creating" @click="generate">生成注册码</e-button>
      </e-space>
    </template>
    <e-alert type="info" show-icon message="仅 admin 可以生成。注册码 24 小时内有效，只能使用一次；注册成功后仍需管理员审核。" />
    <e-table :columns="columns" :data-source="codes" row-key="id" :loading="loading"
      :pagination="{ pageSize: 5 }" :scroll="{ x: 720 }" size="small" class="codes-table">
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'status'">
          <e-tag :color="record.status === 'available' ? 'green' : 'default'">{{ labels[record.status] || record.status }}</e-tag>
        </template>
        <template v-else-if="column.key === 'createdAt'">{{ formatTime(record.createdAt) }}</template>
        <template v-else-if="column.key === 'expiresAt'">{{ formatTime(record.expiresAt) }}</template>
        <template v-else-if="column.key === 'action'">
          <e-popconfirm v-if="record.status === 'available'" title="撤销后该注册码将立即失效，确认撤销？"
            ok-text="撤销" cancel-text="取消" @confirm="revoke(record.id)">
            <e-button type="link" danger>撤销</e-button>
          </e-popconfirm>
          <span v-else>不可使用</span>
        </template>
      </template>
    </e-table>
    <e-modal :open="Boolean(generated)" title="注册码已生成" :footer="null" @cancel="generated = null">
      <e-alert type="warning" show-icon message="明文仅本次显示，关闭后不能再次查看，请先复制并交给受邀用户。" />
      <e-form layout="vertical" class="code-result">
        <e-form-item label="注册码">
          <e-input :value="generated?.registrationCode || ''" readonly aria-label="生成的注册码" />
        </e-form-item>
        <p>有效期至：{{ formatTime(generated?.expiresAt) }}（本地时间）</p>
        <e-space>
          <e-button type="primary" @click="copyCode">复制注册码</e-button>
          <e-button @click="generated = null">已保存，关闭</e-button>
        </e-space>
      </e-form>
    </e-modal>
  </e-card>
</template>

<script setup>
import { ref, onMounted } from 'vue';
import { message } from '@/app/ui/controls';
import { getTokenUser, createRegistrationCode, getRegistrationCodes, revokeRegistrationCode } from '../api/userapi';

const isAdmin = ref(false);
const codes = ref([]);
const loading = ref(false);
const creating = ref(false);
const generated = ref(null);
const labels = { available: '可使用', used: '已使用', expired: '已过期', revoked: '已撤销' };
const columns = [
  { title: '编号', dataIndex: 'id', key: 'id', ellipsis: true },
  { title: '生成时间', key: 'createdAt' },
  { title: '到期时间', key: 'expiresAt' },
  { title: '状态', key: 'status' },
  { title: '操作', key: 'action' },
];
const formatTime = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-';
const loadCodes = async () => {
  loading.value = true;
  try { codes.value = (await getRegistrationCodes()).data || []; }
  catch { /* 请求拦截器显示错误，避免覆盖已取得的结果。 */ }
  finally { loading.value = false; }
};
const generate = async () => {
  creating.value = true;
  try { generated.value = (await createRegistrationCode()).data; await loadCodes(); }
  catch { /* 请求拦截器显示错误。 */ }
  finally { creating.value = false; }
};
const revoke = async (id) => {
  try { await revokeRegistrationCode(id); message.success('注册码已撤销'); await loadCodes(); }
  catch { /* 请求拦截器显示错误。 */ }
};
const copyCode = async () => {
  try { await navigator.clipboard.writeText(generated.value.registrationCode); message.success('已复制'); }
  catch { message.warning('浏览器未开放剪贴板，请选中上方注册码手动复制'); }
};
onMounted(async () => {
  try {
    const user = (await getTokenUser()).data;
    isAdmin.value = user?.name === 'admin' && user?.isSuperAdmin === true;
    if (isAdmin.value) await loadCodes();
  } catch { /* 用户信息失效时不显示管理功能。 */ }
});
</script>

<style scoped>
.registration-codes { margin-bottom: 20px; }
.codes-table { margin-top: 16px; }
.code-result { margin-top: 20px; }
.code-result :deep(input) { font-family: monospace; }
</style>
