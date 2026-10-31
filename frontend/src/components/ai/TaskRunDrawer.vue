<template>
  <e-drawer :open="open" width="min(760px, 100vw)" title="任务执行记录" class="task-run-drawer" @close="emit('close')">
    <e-alert v-if="error" type="error" show-icon :message="error" />
    <e-skeleton v-if="loading" active />
    <template v-else-if="detail">
      <div class="run-heading">
        <div><h2>{{ detail.name }}</h2><p>任务 ID {{ detail.taskId }} · 仅当前账户可读的最近 20 条记录</p></div>
        <e-button @click="load" :loading="loading">刷新记录</e-button>
      </div>
      <e-tag :color="taskStatus(detail.status).color">{{ taskStatus(detail.status).label }}</e-tag>
      <e-alert class="run-boundary" type="info" show-icon message="仅展示服务返回的持久记录及公开结果。排队不等于完成，取消不撤回已发生动作；结果不确定时先核对副作用。人工审批需通过服务支持的流程处理，本页不提供审批恢复。" />
      <e-empty v-if="!detail.runs.length" description="尚无持久执行记录" />
      <ol v-else class="run-timeline">
        <li v-for="run in detail.runs" :key="String(run.id)" class="run-entry">
          <div class="run-title"><h3>记录 {{ run.id }}</h3>
            <e-tag :color="taskStatus(run.status).color">{{ taskStatus(run.status).label }}</e-tag></div>
          <dl class="run-facts">
            <div><dt>创建时间</dt><dd>{{ taskDate(run.createdAt) }}</dd></div>
            <div><dt>结束时间</dt><dd>{{ taskDate(run.finishedAt) }}</dd></div>
            <div><dt>尝试编号</dt><dd>{{ run.attempt ?? '未提供' }}</dd></div>
            <div><dt>队列 Job ID</dt><dd>{{ run.jobId || '未提供' }}</dd></div>
          </dl>
          <p v-if="run.errorMessage" class="run-error">错误信息：{{ taskError(run.errorMessage) }}</p>
          <dl v-if="taskReport(run).length" class="run-facts">
            <div v-for="[label, value] in taskReport(run)" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></div>
          </dl>
          <p v-else class="run-note">公开结果未提供或格式未识别；不推断工具、引用或模型执行结果。</p>
        </li>
      </ol>
    </template>
    <e-button v-if="error && !loading" @click="load">重试读取记录</e-button>
  </e-drawer>
</template>
<script setup>
import { ref, watch, onBeforeUnmount } from 'vue';
import { getTaskStatus } from '@/api/ai/aitask';
import { taskPermissions, taskStatus, taskDate, taskReport, taskError, taskDetail } from '@/shared/ui/taskWorkspace';
const props = defineProps({ open: Boolean, taskId: { type: String, default: '' } });
const emit = defineEmits(['close']);
const detail = ref(null);
const loading = ref(false);
const error = ref('');
let revision = 0;
async function load() {
  const ticket = ++revision;
  detail.value = null;
  error.value = '';
  if (!props.open || !props.taskId) { loading.value = false; return; }
  if (!taskPermissions().read) { error.value = '没有读取任务记录的权限'; loading.value = false; return; }
  loading.value = true;
  const id = props.taskId;
  try {
    const response = await getTaskStatus(id);
    if (ticket !== revision) return;
    const data = response?.data;
    if (response?.code !== 200 || response?.isSuccess === false) throw new Error('执行记录响应无效，请重试');
    detail.value = taskDetail(data, id);
  } catch (failure) {
    if (ticket === revision) error.value = failure.message || '读取执行记录失败，请重试';
  } finally {
    if (ticket === revision) loading.value = false;
  }
}
watch(() => [props.open, props.taskId], load, { immediate: true });
onBeforeUnmount(() => { revision += 1; });
</script>
<style scoped>
.run-heading, .run-title { display:flex; gap:12px; align-items:flex-start; justify-content:space-between; }
h2, h3 { margin:0 0 8px; overflow-wrap:anywhere; }
.run-heading p, .run-note, dt { color:var(--eic-text-secondary); font-size:var(--eic-font-meta); }
.run-boundary { margin:16px 0 24px; }
.run-timeline { padding:0; list-style:none; }
.run-entry { padding:20px 0; border-bottom:1px solid var(--eic-line); }
.run-facts { display:grid; grid-template-columns:1fr 1fr; gap:12px 20px; margin:12px 0; }
dd { margin:4px 0 0; overflow-wrap:anywhere; white-space:pre-wrap; }
.run-error { color:var(--status-error, #b42318); background:var(--status-error-bg, #fff1f0); padding:12px; border-radius:8px; overflow-wrap:anywhere; }
@media (max-width:600px) { .run-facts { grid-template-columns:1fr; } }
</style>
