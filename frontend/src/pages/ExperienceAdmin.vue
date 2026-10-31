<template>
  <main class="experience-admin">
    <p>公开演示使用虚构数据。真实体验只开放指定模型与知识库，体验请求不发送到 Langfuse。</p>
    <p v-if="error" role="alert" class="experience-error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <section>
      <h2>入口与总额度</h2>
      <n-form label-placement="top" @submit.prevent="save">
        <n-form-item label="开放公开演示"><n-switch v-model:value="settings.enabled" /></n-form-item>
        <div class="experience-admin-grid">
          <n-form-item label="公开演示时长（分钟）"><n-input-number v-model:value="settings.minutes" :min="1" :max="1440" /></n-form-item>
          <n-form-item label="公开入口最大在线体验数"><n-input-number v-model:value="settings.maxPublicSessions" :min="1" :max="500" /></n-form-item>
          <n-form-item label="全局累计调用额度"><n-input-number v-model:value="settings.globalCalls" :min="1" :max="1000000" /></n-form-item>
          <n-form-item label="全局累计 Token 预留额度"><n-input-number v-model:value="settings.globalTokens" :min="1" :max="1000000000" /></n-form-item>
        </div>
        <p>累计额度不会因重启、撤销或新建资格而重置。Token 按保守上界预留，不自动退还；每次向量或重排请求预留 600,000 Token 并计一次调用，请按展示需求配置。</p>
        <p>公开演示全部菜单可浏览虚构数据；未获实际使用授权的页面同样保留虚构预览。</p>
        <n-form-item label="公开演示对话及示例能力范围"><n-select v-model:value="settings.pages" multiple :options="pageOptions" /></n-form-item>
        <n-form-item label="真实体验可使用页面上限"><n-select v-model:value="settings.realPages" multiple :options="pageOptions" /></n-form-item>
        <n-button type="primary" :loading="busy" @click="save">保存设置</n-button>
      </n-form>
    </section>
    <section>
      <h2>发放真实体验资格</h2>
      <p>只选择获批展示资源。未授权页面显示虚构预览；配置不能提交，密钥不可读取。附件与联网搜索默认关闭，开启后仍受资格时长和预算限制。入库、工具和长期记忆关闭。</p>
      <n-form label-placement="top" @submit.prevent="create">
        <n-form-item label="资格类型"><n-select v-model:value="form.kind" :options="[{ label: '限时链接', value: 'link' }, { label: '限时账号密码', value: 'account' }]" /></n-form-item>
        <div v-if="form.kind === 'account'" class="experience-admin-grid">
          <n-form-item label="体验账号"><n-input v-model:value="form.username" autocomplete="off" maxlength="80" /></n-form-item>
          <n-form-item label="密码（至少 12 位）"><n-input v-model:value="form.password" type="password" show-password-on="click" autocomplete="new-password" /></n-form-item>
        </div>
        <div class="experience-admin-grid">
          <n-form-item label="首次进入后的体验分钟数"><n-input-number v-model:value="form.minutes" :min="1" :max="1440" /></n-form-item>
          <n-form-item label="领取截止（创建后小时数）"><n-input-number v-model:value="form.claimHours" :min="1" :max="720" /></n-form-item>
          <n-form-item label="最长截止（创建后小时数）"><n-input-number v-model:value="form.hardHours" :min="1" :max="720" /></n-form-item>
          <n-form-item label="调用次数上限"><n-input-number v-model:value="form.calls" :min="1" :max="10000" /></n-form-item>
          <n-form-item label="Token 预留额度"><n-input-number v-model:value="form.tokens" :min="1024" :max="100000000" /></n-form-item>
        </div>
        <n-form-item label="允许模型"><n-select v-model:value="form.modelIds" multiple filterable :options="modelOptions" /></n-form-item>
        <n-form-item label="允许智能体（必须同时授权其绑定模型与知识库）"><n-select v-model:value="form.agentIds" multiple filterable :options="options('agents')" /></n-form-item>
        <n-form-item label="允许知识库（最多两个）"><n-select v-model:value="form.knowledgeIds" multiple filterable :options="options('knowledge')" /></n-form-item>
        <n-form-item label="本资格可使用页面"><n-select v-model:value="form.pages" multiple :options="pageOptions.filter(p => settings.realPages.includes(p.value))" /></n-form-item>
        <n-form-item label="允许聊天附件"><n-switch v-model:value="form.allow_chat_attachments" /></n-form-item>
        <n-form-item label="允许联网搜索"><n-switch v-model:value="form.allow_web_search" /></n-form-item>
        <p>附件复用已启用的本地解析器，图片仅 OCR。联网搜索使用获批模型配置的独立搜索或原生模式；缺少配置时后端明确拒绝。</p>
        <n-space wrap><n-button type="primary" :loading="busy" @click="create">创建资格</n-button>
          <n-button :disabled="busy" @click="openGrants">查看已创建资格</n-button></n-space>
        <p v-if="error" role="alert" class="experience-error">{{ error }}</p>
      </n-form>
    </section>
    <section>
      <h2>资格与用量</h2>
      <p>全局调用：{{ usage.calls || 0 }} · 累计 Token 预留：{{ usage.tokens || 0 }}</p>
      <p>已创建 {{ grants.length }} 个资格；在右侧抽屉统一查看当前、已到期与已撤销记录。</p>
      <n-button :disabled="busy" @click="openGrants">查看已创建资格</n-button>
    </section>
    <n-drawer v-model:show="drawer" :width="'min(980px, 100vw)'" :mask-closable="!busy" :close-on-esc="!busy">
      <n-drawer-content title="已创建资格与历史" :closable="!busy">
        <p>历史仅保留时间、状态与用量，体验正文及有效凭据在结束后自动删除。</p>
        <n-space wrap><n-input v-model:value="search" placeholder="搜索账号或资格 ID" clearable aria-label="搜索资格" />
          <n-select v-model:value="status" :options="statusOptions" style="width:160px" aria-label="筛选资格状态" />
          <n-button :loading="busy" @click="load">刷新</n-button></n-space>
        <p v-if="error" role="alert" class="experience-error">{{ error }}</p>
        <article v-for="grant in filteredGrants" :key="grant.id" class="grant-record">
          <h3>{{ grant.username || '限时链接' }} · {{ statusLabel(grant.status) }}</h3><code>{{ grant.id }}</code>
          <p>调用 {{ grant.usedCalls ?? '未知' }}/{{ grant.calls ?? '未知' }} · Token 预留 {{ grant.usedTokens ?? '未知' }}/{{ grant.tokens ?? '未知' }} · 上游已报告 Token {{ grant.reportedTokens ?? '未知' }}</p>
          <p>领取截止 {{ formatTime(grant.claimEnd) }} · 最长截止 {{ formatTime(grant.hardEnd) }} · 体验结束 {{ formatTime(grant.expires) }}</p>
          <p>最近运行 {{ grant.lastStatus || '尚未运行' }}；费用未知（无供应商价格）。</p>
          <n-space wrap>
            <n-button @click="detail = detail === grant.id ? '' : grant.id">{{ detail === grant.id ? '收起详情' : '查看详情' }}</n-button>
            <n-button :disabled="busy || ended(grant)" @click="editGrant(grant)">修改期限、额度与范围</n-button>
            <n-button v-if="grant.kind === 'link'" :disabled="busy || ended(grant) || !grant.canReveal" @click="reveal(grant)">显示 / 复制链接</n-button>
            <n-popconfirm v-if="grant.kind === 'link'" @positive-click="renew(grant)"><template #trigger>
              <n-button :disabled="busy || ended(grant)">重新签发链接</n-button></template>旧链接和旧会话立即失效，期限与累计用量不重置。</n-popconfirm>
            <n-button v-else :disabled="busy || ended(grant)" @click="resetTarget = grant; newPassword = ''; resetOpen = true">重置密码</n-button>
            <n-popconfirm @positive-click="revoke(grant.id)"><template #trigger><n-button type="error" :disabled="busy || ended(grant)">撤销资格</n-button></template>将清除临时正文与有效凭据，并取消进行中的体验请求。</n-popconfirm>
          </n-space>
          <p v-if="grant.kind === 'link' && !grant.canReveal && !ended(grant)">旧链接仅存哈希，不能还原。请重新签发，旧凭据将立即失效。</p>
          <div v-if="detail === grant.id"><p>可使用页面：{{ (grant.pages || []).map(p => names[p] || p).join('、') }}</p>
            <p>聊天附件：{{ grant.allow_chat_attachments ? '允许' : '关闭' }} · 联网搜索：{{ grant.allow_web_search ? '允许' : '关闭' }}</p>
            <p>授权模型 {{ grant.modelIds?.length || 0 }} 个 · 智能体 {{ grant.agentIds?.length || 0 }} 个 · 知识库 {{ grant.knowledgeIds?.length || 0 }} 个</p>
            <h4>资格历史</h4><p v-if="!grant.history?.length">历史资格未记录操作事件，原始用量保留。</p>
            <p v-for="(item, i) in grant.history" :key="i">{{ formatTime(item.at) }} · {{ historyLabel(item.action) }} · 已用调用 {{ item.usedCalls }} / Token {{ item.usedTokens }}</p>
          </div>
        </article>
        <p v-if="!filteredGrants.length">没有符合条件的资格。</p>
      </n-drawer-content>
    </n-drawer>
    <n-modal :show="!!credential" preset="card" title="体验凭据" class="experience-credential-modal"
      @update:show="value => { if (!value) closeCredential() }">
      <p>{{ credentialKind === 'link' ? '新链接已加密保存，可在资格管理中再次查看。' : '账号密码仅在本次显示，关闭后只能重置密码。' }}</p>
      <pre class="experience-secret">{{ revealed ? credential : '凭据已遮挡，点击显示或直接复制' }}</pre>
      <n-space wrap><n-button @click="revealed = !revealed">{{ revealed ? '隐藏' : '显示' }}</n-button>
        <n-button type="primary" @click="copyCredential">复制凭据</n-button><n-button @click="closeCredential">关闭</n-button></n-space>
      <p v-if="copyError" role="alert" class="experience-error">{{ copyError }}</p>
    </n-modal>
    <n-modal v-model:show="editOpen" preset="card" title="修改体验资格" class="experience-edit-modal" :mask-closable="!busy" :close-on-esc="!busy" :closable="!busy">
      <p>次数和 Token 均为累计总上限，用量不清零；设置低于已用值会阻止后续调用。权限与期限调整会清除旧展示内容并取消本地进行中的请求。</p>
      <n-form v-if="draft" label-placement="top" @submit.prevent="saveGrant">
        <div class="experience-admin-grid">
          <n-form-item label="首次体验分钟数"><n-input-number v-model:value="draft.minutes" :min="1" :max="1440" :disabled="!!draft.started" /></n-form-item>
          <n-form-item label="领取截止"><n-date-picker v-model:value="draft.claimEnd" type="datetime" /></n-form-item>
          <n-form-item label="最长截止"><n-date-picker v-model:value="draft.hardEnd" type="datetime" /></n-form-item>
          <n-form-item v-if="draft.started" label="体验结束"><n-date-picker v-model:value="draft.expires" type="datetime" /></n-form-item>
          <n-form-item label="累计调用上限"><n-input-number v-model:value="draft.calls" :min="1" :max="10000" /></n-form-item>
          <n-form-item label="累计 Token 预留上限"><n-input-number v-model:value="draft.tokens" :min="1024" :max="100000000" /></n-form-item>
        </div>
        <n-form-item label="可使用页面"><n-select v-model:value="draft.pages" multiple :options="pageOptions.filter(p => settings.realPages.includes(p.value))" /></n-form-item>
        <n-form-item label="允许聊天附件"><n-switch v-model:value="draft.allow_chat_attachments" /></n-form-item>
        <n-form-item label="允许联网搜索"><n-switch v-model:value="draft.allow_web_search" /></n-form-item>
        <n-form-item label="授权模型"><n-select v-model:value="draft.modelIds" multiple :options="modelOptions" /></n-form-item>
        <n-form-item label="授权智能体"><n-select v-model:value="draft.agentIds" multiple :options="options('agents')" /></n-form-item>
        <n-form-item label="授权知识库（最多两个）"><n-select v-model:value="draft.knowledgeIds" multiple :options="options('knowledge')" /></n-form-item>
        <p v-if="error" role="alert" class="experience-error">{{ error }}</p>
        <n-space><n-button :disabled="busy" @click="editOpen = false">取消</n-button><n-button type="primary" :loading="busy" @click="saveGrant">保存修改</n-button></n-space>
      </n-form>
    </n-modal>
    <n-modal v-model:show="resetOpen" preset="card" title="重置体验密码" class="experience-credential-modal" :mask-closable="!busy" :close-on-esc="!busy" :closable="!busy">
      <p>旧密码和旧会话立即失效，期限与历史用量保留。</p>
      <n-form-item label="新密码（至少 12 位）"><n-input v-model:value="newPassword" type="password" show-password-on="click" autocomplete="new-password" /></n-form-item>
      <p v-if="error" role="alert" class="experience-error">{{ error }}</p>
      <n-button type="primary" :loading="busy" :disabled="newPassword.length < 12" @click="renew(resetTarget, newPassword)">确认重置</n-button>
    </n-modal>
  </main>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue';
import { NForm, NFormItem, NInput, NInputNumber, NButton, NSelect, NSwitch, NPopconfirm, NDrawer, NDrawerContent, NModal, NSpace, NDatePicker } from 'naive-ui';
import { message } from '@/app/ui/feedback';
import http from '@/utils/http';
const busy = ref(false), error = ref(''), notice = ref(''), credential = ref('');
const drawer = ref(false), search = ref(''), status = ref('all'), detail = ref(''), editOpen = ref(false), draft = ref(null);
const revealed = ref(false), credentialKind = ref('link'), copyError = ref(''), resetOpen = ref(false), resetTarget = ref(null), newPassword = ref('');
const statusOptions = [{ value:'all', label:'全部资格' }, { value:'unclaimed', label:'待领取' }, { value:'active', label:'体验中' },
  { value:'expired', label:'已到期' }, { value:'revoked', label:'已撤销' }, { value:'ended', label:'已结束' }, { value:'corrupt', label:'记录异常，已隔离' }];
const statusLabel = value => statusOptions.find(s => s.value === value)?.label || value;
const ended = grant => !['active', 'unclaimed'].includes(grant.status);
const filteredGrants = computed(() => grants.value.filter(g => (status.value === 'all' || status.value === g.status) &&
  `${g.username || ''} ${g.id}`.toLowerCase().includes(search.value.toLowerCase())));
const historyLabel = key => ({ created:'创建资格', claimed:'首次领取', updated:'修改期限/额度/范围', revoked:'撤销资格', expired:'到期清理', ended:'主动结束',
  credential_viewed:'查看链接', password_reset:'重置密码', link_reissued:'重签链接', run_success:'调用完成', run_failed:'调用失败', run_cancelled:'调用取消' }[key] || key);
function closeCredential() { credential.value = ''; revealed.value = false; copyError.value = ''; }
function showCredential(text, kind) { credential.value = text; credentialKind.value = kind; revealed.value = false; copyError.value = ''; }
async function openGrants() { drawer.value = true; await load(); }
function editGrant(grant) {
  error.value = ''; draft.value = { ...JSON.parse(JSON.stringify(grant)), claimEnd:grant.claimEnd * 1000, hardEnd:grant.hardEnd * 1000, expires:grant.expires ? grant.expires * 1000 : null };
  editOpen.value = true;
}
async function saveGrant() {
  if (busy.value || !draft.value) return;
  busy.value = true; error.value = '';
  try {
    const g = draft.value;
    await http.put(`/api/ExperienceAdmin/Grants/${encodeURIComponent(g.id)}`, {
      revision:g.revision, minutes:g.minutes, claimEnd:g.claimEnd / 1000, hardEnd:g.hardEnd / 1000,
      expires:g.expires ? g.expires / 1000 : null, calls:g.calls, tokens:g.tokens,
      pages:g.pages, modelIds:g.modelIds, agentIds:g.agentIds, knowledgeIds:g.knowledgeIds,
      allow_chat_attachments:g.allow_chat_attachments === true, allow_web_search:g.allow_web_search === true,
    });
    editOpen.value = false; message.success('资格已更新，累计用量保留'); await load();
  } catch (e) { error.value = e.message; message.error(e.message); }
  finally { busy.value = false; }
}
async function reveal(grant) {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try { const response = await http.post(`/api/ExperienceAdmin/Grants/${encodeURIComponent(grant.id)}/credential`);
    showCredential(new URL(response.data.link, location.origin).href, 'link'); }
  catch (e) { error.value = e.message; message.error(e.message); }
  finally { busy.value = false; }
}
async function renew(grant, password = '') {
  if (busy.value || !grant) return;
  busy.value = true; error.value = '';
  try {
    const response = await http.post(`/api/ExperienceAdmin/Grants/${encodeURIComponent(grant.id)}/renew`, { revision:grant.revision, password });
    resetOpen.value = false;
    showCredential(response.data.link ? new URL(response.data.link, location.origin).href : `账号：${grant.username}\n密码：${password}`, grant.kind);
    newPassword.value = ''; message.success('新凭据已生效，旧凭据立即失效'); await load();
  } catch (e) { error.value = e.message; message.error(e.message); }
  finally { busy.value = false; }
}
const settings = ref({ enabled: false, minutes: 30, pages: ['chat'], realPages: ['chat'], globalCalls: 100, globalTokens: 10000000, maxPublicSessions: 50 });
const form = ref({ kind: 'link', username: '', password: '', minutes: 30, claimHours: 24, hardHours: 48, calls: 10, tokens: 3000000, pages: ['chat', 'knowledge', 'agents', 'models'], modelIds: [], agentIds: [], knowledgeIds: [], allow_chat_attachments:false, allow_web_search:false });
const names = ref({}), resources = ref({}), grants = ref([]), usage = ref({});
const pageOptions = computed(() => Object.entries(names.value).map(([value, label]) => ({ value, label })));
const options = key => (resources.value[key] || []).map(r => ({ value: r.id, label: r.name }));
const modelOptions = computed(() => (resources.value.models || []).filter(r => r.kind === 'chat').map(r => ({ value: r.id, label: r.name })));
const formatTime = time => time ? new Date(time * 1000).toLocaleString() : '尚未领取';
async function load() {
  busy.value = true; error.value = '';
  try {
    const results = await Promise.allSettled([http.get('/api/ExperienceAdmin/Settings'), http.get('/api/ExperienceAdmin/Resources'), http.get('/api/ExperienceAdmin/Grants')]);
    for (const result of results) if (result.status === 'rejected') throw result.reason;
    const [site, assets, list] = results.map(r => r.value.data);
    const { revision, ...config } = site.settings;
    settings.value = config; usage.value = site.usage; names.value = site.pageNames; resources.value = assets; grants.value = list;
  } catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
async function save() {
  if (busy.value) return;
  busy.value = true; error.value = ''; notice.value = '';
  try { await http.put('/api/ExperienceAdmin/Settings', settings.value); notice.value = '设置已保存'; message.success(notice.value); }
  catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
async function create() {
  if (busy.value) return;
  busy.value = true; error.value = ''; credential.value = '';
  try {
    const payload = { ...form.value, ...(form.value.kind === 'link' ? { username: '', password: '' } : {}) };
    const response = await http.post('/api/ExperienceAdmin/Grants', payload);
    showCredential(response.data.link ? new URL(response.data.link, location.origin).href : `账号：${form.value.username}\n密码：${form.value.password}`, form.value.kind);
    message.success('体验资格已创建'); form.value.password = ''; await load();
  } catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
async function revoke(id) {
  if (busy.value) return;
  busy.value = true;
  try { await http.delete(`/api/ExperienceAdmin/Grants/${encodeURIComponent(id)}`); message.success('资格已撤销，临时内容与有效凭据已清除'); await load(); }
  catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
async function copyCredential() {
  try { await navigator.clipboard.writeText(credential.value); copyError.value = ''; message.success('已复制，请安全交付凭据'); }
  catch { copyError.value = '浏览器不允许复制。请点击显示后手动选择凭据文本。'; }
}
onMounted(load);
onBeforeUnmount(() => { closeCredential(); form.value.password = ''; newPassword.value = ''; });
watch(resetOpen, open => { if (!open) newPassword.value = ''; });
</script>

<style scoped>
.experience-admin { padding:0; color:var(--eic-text); }
.experience-admin section { margin:20px 0; padding:20px; border:1px solid var(--eic-line); background:var(--eic-surface); border-radius:12px; }
.experience-admin-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px 24px; }
.experience-admin article { margin-top:16px; padding:16px; background:var(--eic-surface-muted); border-radius:8px; }
.experience-error { color:var(--eic-danger); }
.experience-admin-credential p { white-space:pre-wrap; overflow-wrap:anywhere; user-select:all; }
.grant-record { margin:16px 0; padding:18px; border:1px solid var(--eic-line); border-radius:12px; overflow-wrap:anywhere; }
.experience-credential-modal { width:min(580px,calc(100vw - 32px)); }
.experience-edit-modal { width:min(800px,calc(100vw - 32px)); max-height:calc(100dvh - 32px); overflow:auto; }
.experience-secret { white-space:pre-wrap; overflow-wrap:anywhere; padding:16px; background:var(--eic-surface-muted); user-select:all; }
</style>
