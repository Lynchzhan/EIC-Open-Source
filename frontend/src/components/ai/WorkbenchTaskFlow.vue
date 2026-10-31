<template>
  <section class="workbench-task-flow" aria-label="我的任务流">
    <div class="flow-heading"><div><h2>我的任务流</h2><p>当前账户的持久任务状态，不包含未接入的 Agent 任务。</p></div>
      <e-space wrap><e-button :loading="loading" @click="load">刷新任务</e-button><e-button @click="router.push('/home/my/ai-tasks')">全部任务</e-button></e-space></div>
    <e-alert v-if="error" type="error" show-icon :message="error" />
    <e-skeleton v-if="loading" active />
    <template v-else-if="!error">
      <div class="flow-filters" aria-label="任务状态筛选">
        <e-button v-for="item in groups" :key="item.key" :type="bucket === item.key ? 'primary' : 'default'" @click="bucket = item.key">
          {{ item.label }} {{ item.key === 'all' ? rows.length : counts[item.key] }}
        </e-button>
      </div>
      <e-empty v-if="!visible.length" description="该状态暂无任务" />
      <ul v-else class="flow-list">
        <li v-for="task in visible.slice(0, 5)" :key="task.key">
          <div><e-tag :color="taskStatus(task.status).color">{{ taskStatus(task.status).label }}</e-tag>
            <strong>{{ task.name }}</strong><p>下次计划 {{ taskDate(task.next) }} · {{ task.timeZone || '时区未提供' }}</p></div>
          <e-button :disabled="!task.id" @click="selected = task.id">执行记录</e-button>
        </li>
      </ul>
      <p class="flow-note">最多显示 5 项；“需关注”包含失败、队列不可用和待确认结果，不代表已建立人工审批。</p>
    </template>
    <TaskRunDrawer :open="!!selected" :task-id="selected" @close="selected = ''" />
  </section>
</template>
<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue';
import { useRouter } from 'vue-router';
import { getTaskList } from '@/api/ai/aitask';
import { taskRows, taskStatus, taskDate, taskCounts, filterTasks, taskPermissions } from '@/shared/ui/taskWorkspace';
import TaskRunDrawer from './TaskRunDrawer.vue';
const router = useRouter();
const rows = ref([]);
const loading = ref(true);
const error = ref('');
const bucket = ref('all');
const selected = ref('');
const groups = [{ key:'all', label:'全部' }, { key:'waiting', label:'待处理' },
  { key:'active', label:'运行中' }, { key:'attention', label:'需关注' }];
const counts = computed(() => taskCounts(rows.value));
const visible = computed(() => filterTasks(rows.value, '', bucket.value));
let revision = 0;
async function load() {
  const ticket = ++revision;
  rows.value = [];
  error.value = '';
  loading.value = true;
  try {
    if (!taskPermissions().read) throw new Error('没有读取任务的权限');
    const response = await getTaskList();
    if (ticket !== revision) return;
    if (response?.code !== 200 || response?.isSuccess === false) throw new Error(response?.message || '读取任务失败');
    rows.value = taskRows(response.data);
  } catch (failure) {
    if (ticket === revision) error.value = failure.message || '读取任务失败，请重试';
  } finally { if (ticket === revision) loading.value = false; }
}
onMounted(load);
onBeforeUnmount(() => { revision += 1; });
</script>
<style scoped>
.workbench-task-flow { background:var(--eic-surface); border:1px solid var(--eic-line); border-radius:14px; padding:24px; margin-bottom:20px; }
.flow-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }
h2 { margin:0 0 8px; font-size:var(--eic-font-section); } p { color:var(--eic-text-secondary); font-size:var(--eic-font-ui); }
.flow-filters { display:flex; flex-wrap:wrap; gap:8px; margin:16px 0; }
.flow-list { list-style:none; padding:0; margin:0; }
.flow-list li { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 0; border-bottom:1px solid var(--eic-line); }
.flow-list li > div { min-width:0; } strong { overflow-wrap:anywhere; }
.flow-list p { margin:8px 0 0; } .flow-note { margin-bottom:0; }
@media(max-width:600px) { .workbench-task-flow { padding:16px; } .flow-heading { flex-direction:column; }
  .flow-list li { align-items:flex-start; } }
</style>
