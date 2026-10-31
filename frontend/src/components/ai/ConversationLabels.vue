<template>
  <n-button size="small" :loading="loading" @click="openHistory">补全历史标题</n-button>
  <n-modal v-model:show="visible" preset="card" :title="mode === 'history' ? '补全历史标题' : '管理会话标题'" style="width:min(560px,94vw)">
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="mode === 'history'">
      <p>仅列出已有完整问答且仍使用默认标题的会话。后台逐项生成，会产生模型调用。</p>
      <n-space><n-button @click="selected = eligible.map(v => v.id)">全选</n-button><n-button @click="selected = []">取消全选</n-button></n-space>
      <n-checkbox-group v-model:value="selected" class="label-history-list">
        <n-checkbox v-for="item in eligible" :key="item.id" :value="item.id">{{ item.title || '新对话' }} · {{ item.preview || item.id }}</n-checkbox>
      </n-checkbox-group>
      <p v-if="!eligible.length">暂无可补全的历史会话。</p>
    </template>
    <template v-else>
      <n-input v-model:value="title" maxlength="120" placeholder="会话标题" aria-label="会话标题" />
      <p>{{ target?.summary || '暂无会话摘要' }}</p>
      <n-button :disabled="busy" @click="regenerate">重新生成标题和摘要</n-button>
      <p>重新生成使用当前对话内容；手动命名后不会被自动生成覆盖。</p>
    </template>
    <template #footer><n-space justify="end">
      <n-button :disabled="busy" @click="visible = false">取消</n-button>
      <n-button type="primary" :loading="busy" :disabled="mode === 'history' ? !selected.length : !title.trim()" @click="mode === 'history' ? submitHistory() : rename()">{{ mode === 'history' ? '确认执行' : '保存标题' }}</n-button>
    </n-space></template>
  </n-modal>
</template>
<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue';
import { NButton, NModal, NSpace, NCheckbox, NCheckboxGroup, NInput } from 'naive-ui';
import { dialog, message } from '@/app/ui/feedback';
import http, { isMutationSuccess } from '@/utils/http';
const emit = defineEmits(['updated']);
const labels = ref([]), selected = ref([]), visible = ref(false), busy = ref(false), loading = ref(false);
const mode = ref('history'), target = ref(null), title = ref(''), error = ref('');
let timer, alive = true, sequence = 0, settlingUntil = 0;
const pending = item => ['pending', 'running', 'provider_started', 'provider_done'].includes(item.status);
const eligible = computed(() => labels.value.filter(item => item.eligible && !pending(item)));
const refresh = async () => {
  clearTimeout(timer);
  const request = ++sequence;
  const result = await http.get('/api/AIChats/Labels');
  if (!alive || request !== sequence) return;
  if (result.code !== 200) throw new Error(result.msg || '标题状态读取失败');
  labels.value = result.data || [];
  emit('updated', labels.value);
  if (labels.value.some(pending) || Date.now() < settlingUntil) timer = setTimeout(() => refresh().catch(() => { error.value = '后台状态暂时不可用，请重新打开标题管理刷新'; }), 3000);
};
const refreshAfterTurn = () => { settlingUntil = Date.now() + 30000; return refresh(); };
const openHistory = async () => {
  mode.value = 'history'; selected.value = []; error.value = ''; visible.value = true; loading.value = true;
  try { await refresh(); } catch (exc) { error.value = exc.message; } finally { loading.value = false; }
};
const open = async item => {
  try { await refresh(); } catch { /* 弹窗仍允许手动改名。 */ }
  mode.value = 'single'; target.value = labels.value.find(v => v.id === String(item.id)) || { ...item, id: String(item.id) };
  title.value = item.title; error.value = ''; visible.value = true;
};
const submit = async payload => {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try {
    const result = await http.post('/api/AIChats/Labels', payload);
    if (result.code !== 200) throw new Error(result.msg || '生成任务提交失败');
    message.success(`已提交 ${result.data.jobIds.length} 个后台任务`); visible.value = false;
    await refresh();
  } catch (exc) { error.value = exc.message || '提交失败'; } finally { busy.value = false; }
};
const submitHistory = () => submit({ ids: selected.value, history: true });
const regenerate = () => {
  const execute = () => submit({ ids: [target.value.id], expectedVersion: target.value.version, confirmOverwrite: true });
  if (target.value.manual) dialog.warning({ title: '覆盖手动标题？', content: '将根据对话重新生成标题和摘要。', positiveText: '确认重新生成', negativeText: '取消', onPositiveClick: execute });
  else execute();
};
const rename = async () => {
  busy.value = true; error.value = '';
  try {
    const result = await http.put('/api/AIChats/Label', { id: target.value.id, title: title.value.trim() });
    if (!isMutationSuccess(result)) throw new Error(result.msg || '保存失败');
    message.success('会话标题已保存'); visible.value = false; await refresh();
  } catch (exc) { error.value = exc.message; } finally { busy.value = false; }
};
onMounted(() => refresh().catch(() => {}));
onBeforeUnmount(() => { alive = false; clearTimeout(timer); });
defineExpose({ open, refresh, refreshAfterTurn });
</script>
<style scoped>
.label-history-list { display:flex; flex-direction:column; gap:12px; max-height:45vh; overflow:auto; padding:16px 0; }
</style>
