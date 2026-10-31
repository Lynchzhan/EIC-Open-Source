<template>
  <ConsoleLayout v-if="state.session" :experience="state.session" :experience-nav="nav" @experience-navigate="selectPage" @experience-leave="leave">
    <div class="experience-console" :class="{ 'is-chat': page === 'chat' }">
      <aside class="experience-banner" aria-label="体验状态"><div>
        <strong>{{ state.session.mode === 'public' ? '公开演示 · 虚构数据' : '限时真实体验' }}</strong>
        <span role="timer">剩余 {{ remaining }}</span><span>{{ state.session.mode === 'public' ? '回复为模拟，不调用模型' : '仅授权资源，配置与工具操作受限' }}</span></div>
        <n-button size="small" @click="leave">结束体验</n-button>
        <p v-if="state.session.quota">累计调用 {{ state.session.quota.usedCalls }}/{{ state.session.quota.calls }} · Token 预留 {{ state.session.quota.usedTokens }}/{{ state.session.quota.tokens }}</p>
      </aside>
      <p v-if="error" class="experience-error" role="alert">{{ error }}</p>
      <p v-if="state.session.mode === 'real' && !state.session.usablePages?.includes(page)" role="status">虚构展示预览：本页未获实际使用授权，数据均为虚构示例。</p>
      <div v-if="page === 'chat'" class="experience-selectors">
        <label>模型<n-select v-model:value="state.modelId" :options="modelOptions" :disabled="state.busy" /></label>
        <label>智能体<n-select :value="state.agentId" :options="agentOptions" :disabled="state.busy" @update:value="state.selectAgent" /></label>
        <label>知识库<n-select v-model:value="state.knowledgeId" :options="knowledgeOptions" :disabled="state.busy" /></label>
      </div>
      <p v-if="loading" role="status" class="experience-loading">正在读取体验资源…</p>
      <section v-else-if="!catalogError" class="experience-surfaces" :class="{ 'is-chat':page === 'chat' }"><component :is="currentView" :key="`${nav}:${generation}`" /></section>
      <n-button v-else @click="selectPage(nav)">重新加载体验资源</n-button>
    </div>
  </ConsoleLayout>
  <main v-else class="experience-entry">
    <h1>进入 EIC 工作空间</h1><p>公开演示使用虚构数据；真实体验需管理员发放的限时资格。</p>
    <p v-if="error" role="alert" class="experience-error">{{ error }}</p>
    <n-space wrap><n-button type="primary" :loading="loading" @click="enter({ mode:'public' })">体验演示</n-button>
      <n-button :disabled="loading || ending" @click="accessOpen = true">限时体验</n-button><n-button @click="router.push('/login')">返回登录</n-button></n-space>
    <ExperienceAccessDialog v-model:show="accessOpen" @entered="restore" />
  </main>
</template>
<script setup>
import { computed, defineAsyncComponent, reactive, ref, onMounted, onBeforeUnmount, nextTick } from 'vue';
import { useRouter } from 'vue-router';
import { NButton, NSelect, NSpace } from 'naive-ui';
import ConsoleLayout from '@/app/layouts/ConsoleLayout.vue';
import ExperienceAccessDialog from '@/components/ExperienceAccessDialog.vue';
import { experienceRequest } from '@/api/experience';
import { experienceContext, experiencePageByNav } from '@/app/experience/runtime';
import { message } from '@/app/ui/feedback';
import ExperiencePreview from '@/app/experience/ExperiencePreview.vue';
import '@/app/styles/experience.css';
const views = {
  'my-ai-chat':defineAsyncComponent(() => import('@/pages/ai/MyAIChat.vue')),
  'my-ai-tasks':defineAsyncComponent(() => import('@/pages/ai/MyAITasks.vue')),
  'my-ai-agents':defineAsyncComponent(() => import('@/pages/ai/MyAgentList.vue')),
  'ai-appsmg':defineAsyncComponent(() => import('@/pages/ai/AgentManagement.vue')),
  'ai-kmssmg':defineAsyncComponent(() => import('@/pages/ai/KnowledgeBaseManagement.vue')),
  'ai-modelmg':defineAsyncComponent(() => import('@/pages/ai/ModelManagement.vue')),
  'ai-promptsmg':defineAsyncComponent(() => import('@/pages/ai/PromptManagement.vue')),
  'ai-skilltoolmg':defineAsyncComponent(() => import('@/pages/ai/SkillToolManagement.vue')),
  'organizational-department':defineAsyncComponent(() => import('@/pages/organizational/DepartmentManagement.vue')),
  'organizational-position':defineAsyncComponent(() => import('@/pages/organizational/PositionManagement.vue')),
  'user-list':defineAsyncComponent(() => import('@/pages/UserList.vue')),
  'user-role':defineAsyncComponent(() => import('@/pages/UserRole.vue')),
  'user-permission':defineAsyncComponent(() => import('@/pages/PermissionMg.vue')),
  'system-announcement':defineAsyncComponent(() => import('@/pages/SystemAnnouncement.vue')),
  'system-dic':defineAsyncComponent(() => import('@/pages/DicMg.vue')),
  'system-tenant':defineAsyncComponent(() => import('@/pages/TenantManagement.vue')),
  'system-experience':defineAsyncComponent(() => import('@/pages/ExperienceAdmin.vue')),
  'log-management':defineAsyncComponent(() => import('@/pages/HttpLog.vue')),
  oslog:defineAsyncComponent(() => import('@/pages/OSLog.vue')),
  'system-code-generator':ExperiencePreview,
  'my-message':defineAsyncComponent(() => import('@/pages/MyMessages.vue')),
  handleUserInfo:ExperiencePreview, observability:ExperiencePreview,
  'ai-recalltest':defineAsyncComponent(() => import('@/pages/ai/KnowledgeBaseManagement.vue')),
};
const router = useRouter(), nav = ref('my-ai-chat'), error = ref(''), loading = ref(false), accessOpen = ref(false);
const now = ref(Date.now()), generation = ref(0);
const catalogError = ref(false);
let controller, timer, disposed = false, checking = false, sequence = 0, lifecycle = 0, observer, pendingEntry;
const ending = ref(false);
const state = reactive({ session:null, catalog:{}, messages:[], busy:false, stage:'', failed:false,
  page:'chat', attachments:[], webSearch:false,
  async uploadAttachment(file) {
    if (!state.session?.allow_chat_attachments) return message.info('该体验资格未开放聊天附件');
    const limits = state.session.attachment_limits || { max_bytes:8000, max_files:5 };
    if (file.size > limits.max_bytes || state.attachments.length >= limits.max_files) throw new Error('附件超出本次体验大小或数量限制');
    const epoch = state.session.accessVersion;
    const item = await experienceRequest('/attachments', file, 'POST', undefined, { 'X-Experience-Filename':encodeURIComponent(file.name) });
    if (state.session?.accessVersion === epoch) state.attachments.push(item);
    return item;
  },
  async removeAttachment(id) {
    await experienceRequest(`/attachments/${encodeURIComponent(id)}`, undefined, 'DELETE');
    state.attachments = state.attachments.filter(item => item.id !== id);
  },
  modelId:'', agentId:'', knowledgeId:'', abort:() => controller?.abort(),
  selectAgent(id) {
    if (state.busy) return;
    state.agentId = id || '';
    const agent = state.catalog.agents?.find(a => a.id === id);
    if (agent?.modelId) state.modelId = agent.modelId;
  }, send,
});
const page = computed(() => experiencePageByNav[nav.value]), currentView = computed(() => views[nav.value]);
const options = key => (state.catalog[key] || []).map(item => ({ value:item.id, label:item.name }));
const modelOptions = computed(() => options('models'));
const agentOptions = computed(() => [{ value:'', label:'通用对话' }, ...options('agents')]);
const knowledgeOptions = computed(() => [{ value:'', label:'不检索' }, ...options('knowledge')]);
const remaining = computed(() => {
  const seconds = Math.max(0, Math.ceil(((state.session?.expires || 0) * 1000 - now.value) / 1000));
  return `${Math.floor(seconds / 60)}分${seconds % 60}秒`;
});
async function selectPage(key) {
  if (!(state.session?.previewPages || state.session?.pages || []).includes(experiencePageByNav[key])) return;
  cancelActiveRun(); nav.value = key; state.page = experiencePageByNav[key];
  const seq = ++sequence; loading.value = true; catalogError.value = false; error.value = ''; state.catalog = {};
  try {
    const catalog = await experienceRequest(`/catalog?page=${page.value}`);
    if (seq !== sequence || disposed || !state.session) return;
    state.catalog = catalog;
    if (!catalog.models?.some(m => m.id === state.modelId)) state.modelId = catalog.models?.[0]?.id || '';
    if (!catalog.agents?.some(a => a.id === state.agentId)) state.agentId = '';
    if (!catalog.knowledge?.some(b => b.id === state.knowledgeId)) state.knowledgeId = '';
    generation.value++;
  } catch (e) { if (seq === sequence && !disposed) {
    if (e.status === 401) clear();
    error.value = e.message; catalogError.value = true;
  } }
  finally { if (seq === sequence) { loading.value = false; await nextTick(); markRestricted(); } }
}
async function refresh(epoch = lifecycle) {
  const seq = sequence;
  const data = await experienceRequest('/session');
  if (disposed || ending.value || epoch !== lifecycle || seq !== sequence) return;
  state.session = data; state.messages = data.messages || []; experienceContext.value = state;
  state.attachments = data.attachments || [];
  await selectPage(state.session.pages.includes(page.value) ? nav.value : 'my-ai-chat');
}
async function restore() {
  if (disposed || ending.value || loading.value) return;
  const epoch = lifecycle;
  loading.value = true; error.value = '';
  try { await refresh(epoch); }
  catch (e) { if (!disposed && epoch === lifecycle) { clear(); error.value = e.message; } }
  finally { if (!disposed && epoch === lifecycle) loading.value = false; }
}
async function enter(payload) {
  if (disposed || ending.value || loading.value) return;
  const epoch = ++lifecycle;
  loading.value = true; error.value = '';
  const entry = experienceRequest('/enter', payload, 'POST'); pendingEntry = entry;
  try { await entry; if (!disposed && !ending.value && epoch === lifecycle) await refresh(epoch); }
  catch (e) { if (!disposed && epoch === lifecycle) error.value = e.message; }
  finally {
    if (pendingEntry === entry) pendingEntry = null;
    if (!disposed && epoch === lifecycle) loading.value = false;
  }
}
function cancelActiveRun() {
  const previous = controller; controller = null;
  previous?.abort(); state.busy = false;
}
function clear() {
  cancelActiveRun(); ++sequence; ++lifecycle; generation.value++;
  state.messages = []; state.catalog = {}; state.session = null; state.stage = ''; state.busy = false;
  state.failed = false; loading.value = false; catalogError.value = false;
  state.attachments = []; state.webSearch = false;
  state.modelId = ''; state.agentId = ''; state.knowledgeId = ''; experienceContext.value = null;
}
async function leave() {
  if (ending.value) return;
  ending.value = true; clear();
  // 等待进入请求的 Set-Cookie 完成，再注销，避免晚到的进入响应恢复会话。
  try { await pendingEntry?.catch(() => {}); await experienceRequest('/leave', {}, 'POST'); }
  catch { /* 无论网络状态如何，先清除本地正文。 */ }
  finally { ending.value = false; }
}
async function check() {
  if (checking || !state.session || disposed || ending.value) return;
  const activeSession = state.session;
  checking = true;
  try {
    const data = await experienceRequest('/session');
    if (state.session !== activeSession || disposed) return;
    const changed = data.accessVersion !== state.session.accessVersion;
    state.session = data;
    if (changed) {
      cancelActiveRun(); state.messages = []; state.catalog = {}; generation.value++;
      state.attachments = []; state.webSearch = false;
      await selectPage(data.pages.includes(page.value) ? nav.value : 'my-ai-chat');
      message.info('体验权限已更新，旧内容已清除');
    }
  } catch (e) {
    if (state.session !== activeSession || disposed) return;
    if (e.status === 401 || e.status === 403 || state.session.expires * 1000 <= Date.now()) clear();
    error.value = e.status ? e.message : '体验状态暂时无法同步，正在重新连接；不会因此撤销资格。';
  }
  finally { checking = false; }
}
async function send(text, options = {}) {
  if (state.busy || !state.session || !text.trim()) return;
  if (text.length > 2000) return message.warning('体验消息最多 2000 字');
  const epoch = state.session.accessVersion;
  state.failed = false; state.busy = true; state.stage = '正在连接'; error.value = '';
  const answer = { role:'assistant', content:'' };
  state.messages.push({ role:'user', content:text }, answer);
  const active = new AbortController(); controller = active;
  try {
    const response = await experienceRequest('/chat', { message:text, modelId:state.modelId,
      agentId:state.agentId, knowledgeIds:state.knowledgeId ? [state.knowledgeId] : [],
      attachment_ids:options.attachment_ids || state.attachments.map(item => item.id),
      web_search:options.web_search ?? state.webSearch }, 'POST', active.signal);
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = '', completed = false;
    function event(line) {
      if (!line.trim() || controller !== active || active.signal.aborted || disposed || state.session?.accessVersion !== epoch) return;
      const item = JSON.parse(line);
      if (item.type === 'error') throw new Error(item.text);
      if (item.type === 'stage') state.stage = item.text;
      if (item.type === 'delta') { answer.content += item.text; state.messages = [...state.messages]; }
      if (item.type === 'done') {
        completed = true; state.stage = '已完成';
        if (item.sources?.length) answer.content += '\n\n' + item.sources.map(s => `> ${s.name}：${s.excerpt}`).join('\n\n');
        if (item.webSources?.length) answer.content += '\n\n网页参考：\n' + item.webSources.map(s => {
          const url = new URL(s.url);
          return ['http:', 'https:'].includes(url.protocol) ? `- [${String(s.name).replace(/[\[\]<>]/g, '')}](<${url.href.replace(/[<>]/g, '')}>)` : '';
        }).filter(Boolean).join('\n');
        state.messages = [...state.messages];
      }
    }
    try {
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream:!done });
        const lines = buffer.split('\n'); buffer = lines.pop(); lines.forEach(event);
        if (done) { if (buffer) event(buffer); break; }
      }
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
    if (!completed && !active.signal.aborted) throw new Error('连接中断，回复可能不完整');
  } catch (e) {
    if (controller === active && !disposed) {
      state.failed = true; error.value = e.name === 'AbortError' ? '已停止生成' : e.message; state.stage = '本次运行已结束';
    }
  } finally {
    if (controller === active) { state.busy = false; controller = null; await check(); }
  }
}
const restrictedAction = /删除|清除|保存|提交|重试|运行|执行|暂停|上传|导入|导出|下载|凭据|密钥|秘钥|重建|摘要|纠正|遗忘|确认|归档|恢复|测试|语音|电话|打开试用|启用|停用|撤销|签发|复制链接|显示.*链接|重置密码|生成代码|重新加载权限|同步权限|刷新权限/;
function actionLabel(el) { return `${el.textContent || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`; }
function isRestricted(el) {
  if (!state.session || el.closest('.console-sidebar, .console-topbar, .experience-banner, .experience-selectors, .experience-entry')) return false;
  if (el.closest('[data-experience-chat-attachment]') && page.value === 'chat'
      && state.session.mode === 'real' && state.session.allow_chat_attachments
      && state.session.usablePages?.includes('chat')) return false;
  // 允许打开新建、编辑表单；写请求仍由体验 adapter 的白名单拒绝。
  if (/^(添加|新增|新建|创建|编辑|修改)/.test((el.textContent || '').trim()) && !/保存|提交|确认/.test(actionLabel(el))) return false;
  return restrictedAction.test(actionLabel(el)) && !/重试记录|重新加载|重新读取|刷新/.test(actionLabel(el));
}
function guard(event) {
  const target = event.target.closest?.('button, [role="menuitem"], input[type="file"], a[download]');
  if (target && isRestricted(target)) { event.preventDefault(); event.stopImmediatePropagation(); message.info('体验账号不可操作'); }
}
function guardSubmit(event) {
  if (state.session && !event.target.closest('.experience-entry')) {
    event.preventDefault(); event.stopImmediatePropagation(); message.info('体验账号不可操作：提交受限');
  }
}
function markRestricted() {
  if (!state.session) return;
  for (const el of document.querySelectorAll('button, [role="menuitem"]')) {
    if (isRestricted(el)) {
      if (!el.dataset.experienceRestricted) el.dataset.experienceAria = el.getAttribute('aria-disabled') ?? '';
      el.setAttribute('aria-disabled', 'true'); el.dataset.experienceRestricted = '1';
    } else if (el.dataset.experienceRestricted) {
      if (el.dataset.experienceAria) el.setAttribute('aria-disabled', el.dataset.experienceAria);
      else el.removeAttribute('aria-disabled');
      delete el.dataset.experienceRestricted; delete el.dataset.experienceAria;
    }
  }
}
function onHide() {
  // 刷新和浏览器页面缓存只清理内存，显式“结束体验”才撤销资格。
  clear();
}
function onShow(event) { if (event.persisted) restore(); }
onMounted(async () => {
  document.addEventListener('click', guard, true); window.addEventListener('pagehide', onHide);
  document.addEventListener('submit', guardSubmit, true);
  window.addEventListener('pageshow', onShow);
  observer = new MutationObserver(markRestricted); observer.observe(document.body, { childList:true, subtree:true });
  timer = setInterval(() => { now.value = Date.now(); if (state.session) check(); }, 2000);
  const epoch = lifecycle;
  const ticket = new URLSearchParams(location.hash.slice(1)).get('ticket');
  if (ticket) { history.replaceState(null, '', location.pathname); await enter({ mode:'link', credential:ticket }); return; }
  try { await refresh(epoch); }
  catch {
    if (disposed || epoch !== lifecycle || ending.value) return;
    if (new URLSearchParams(location.search).get('mode') === 'public') await enter({ mode:'public' });
    else accessOpen.value = new URLSearchParams(location.search).get('mode') === 'account';
  }
});
onBeforeUnmount(() => { disposed = true; clear(); clearInterval(timer); observer?.disconnect();
  document.removeEventListener('click', guard, true); window.removeEventListener('pagehide', onHide); window.removeEventListener('pageshow', onShow); });
onBeforeUnmount(() => document.removeEventListener('submit', guardSubmit, true));
</script>
