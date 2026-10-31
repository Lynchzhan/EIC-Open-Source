import { shallowRef } from 'vue';
import { experienceRequest } from '@/api/experience';
import { fixtureRequest, adminPreview } from './fixtures';
import { message } from '@/app/ui/feedback';

// 共享正式视图，数据始终由独立体验接口提供；未知请求拒绝，绝不回退正式 API。
export const experienceContext = shallowRef(null);
export const experiencePageByNav = {
  'my-ai-chat': 'chat', 'my-ai-tasks': 'tasks', 'my-ai-agents': 'agents',
  'ai-appsmg': 'agents', 'ai-promptsmg': 'prompts', 'ai-kmssmg': 'knowledge',
  'ai-modelmg': 'models', 'ai-skilltoolmg': 'skills',
  'organizational-department':'departments', 'organizational-position':'positions',
  'user-list':'users', 'user-role':'roles', 'user-permission':'permissions',
  'system-announcement':'announcements', 'system-dic':'settings', 'system-tenant':'tenants',
  'system-experience':'experience', 'log-management':'logs', oslog:'audit',
  'system-code-generator':'codegen', 'my-message':'messages', handleUserInfo:'profile',
  observability:'observability', 'ai-recalltest':'knowledge', dashboard:'chat',
};
export function inExperience() { return location.pathname === '/experience' || location.pathname.startsWith('/experience/'); }
export function restricted() { message.info('体验账号不可操作：仅可浏览，提交与删除受限'); throw new Error('体验账号不可操作'); }
const pageData = (data, cfg) => {
  let query = {};
  try { query = typeof cfg.data === 'string' ? JSON.parse(cfg.data) : cfg.data || {}; } catch { /* 查询无效时使用首页。 */ }
  const search = String(query.keyWord || query.keyword || query.searchKey || query.name || '').toLowerCase();
  const rows = data.filter(row => (!search || String(row.name || row.modelName || row.title || row.fullName || '').toLowerCase().includes(search))
    && (!query.skillToolType || row.skillToolType === Number(query.skillToolType)));
  const size = Math.max(1, Number(query.pageSize) || 20), page = Math.max(1, Number(query.pageNum) || 1);
  return { data: rows.slice((page - 1) * size, page * size), total: rows.length, pageNum:page, pageSize:size };
};
export async function experienceAdapter(cfg) {
  const context = experienceContext.value;
  if (!context) throw new Error('体验会话尚未就绪或已结束');
  const epoch = context.session.accessVersion;
  const url = new URL(cfg.url, location.origin), path = url.pathname;
  const method = (cfg.method || 'get').toLowerCase();
  if (context.session.expires * 1000 <= Date.now()) throw new Error('体验时间已结束');
  const fixture = fixtureRequest(path, method, { ...Object.fromEntries(url.searchParams), ...cfg.params });
  const admin = method === 'get' ? adminPreview()[path] : undefined;
  if (fixture || admin !== undefined) {
    await experienceRequest(`/catalog?page=${fixture?.page || 'experience'}`, undefined, 'GET', cfg.signal);
    if (experienceContext.value !== context || epoch !== context.session?.accessVersion) throw new Error('体验权限已更新，请重新查看');
    return { data:fixture ? (fixture.paginated ? pageData(fixture.data, cfg) : fixture.data) : admin,
      status:200, statusText:'OK', headers:{}, config:cfg };
  }
  // POST 只允许明确列出的查询，不能按 Get 前缀推断其安全性。
  const reads = new Set([
    '/api/AIModels/GetPageData', '/api/AIKmss/GetMyPageData', '/api/AIKmss/GetList',
    '/api/AIApps/GetPageData', '/api/AIPrompts/GetPageData', '/api/AISkillTools/GetPageData',
    '/api/AISkillTool/GetPageData', '/api/AISkillToolManagement/GetPageData', '/api/AITasks/GetTaskList',
  ]);
  if (method !== 'get' && !(method === 'post' && reads.has(path))) restricted();
  let data;
  if (path === '/api/Permission/GetUserPermissions') data = ['*'];
  else if (path === '/api/User/GetUser') data = { id:'experience', name: '体验访客', userName: '体验访客', tenantId: 'experience', isSuperAdmin: false };
  else if (path === '/api/AIModels/Defaults') data = { embeddingModelId: null, rerankModelId: null };
  else {
    let page;
    if (path.startsWith('/api/AIModels/')) page = context.page === 'chat' ? 'chat' : 'models';
    else if (path.startsWith('/api/AIApps/')) page = context.page === 'chat' ? 'chat' : 'agents';
    else if (path.startsWith('/api/AIKmss/')) page = context.page === 'chat' ? 'chat' : 'knowledge';
    else if (path === '/api/AITasks/GetTaskList' || path === '/api/AITasks/Capabilities') page = 'tasks';
    else if (path === '/api/AIPrompts/GetPageData') page = 'prompts';
    else if (path === '/api/AISkillToolManagement/GetPageData' || /^\/api\/AISkillTools?\/GetPageData$/.test(path)) page = 'skills';
    else restricted();
    if (!(context.session.previewPages || context.session.pages).includes(page)) restricted();
    const catalog = await experienceRequest(`/catalog?page=${page}`, undefined, 'GET', cfg.signal);
    if (experienceContext.value !== context || epoch !== context.session?.accessVersion) throw new Error('体验权限已更新，请重新查看');
    const models = (catalog.models || []).map(m => ({ id: m.id, name: m.name, modelName: m.name, modelDescription: m.name,
      aiModelType: 1, aiType: '体验模型', endPoint: '已隐藏', enabled: true, revision: 1 }));
    const agents = (catalog.agents || []).map(a => ({ ...a, enabled: true, type: '知识助手', describe: '展示授权能力，工具与长期记忆关闭', chatModelId: a.modelId, chatModelID: a.modelId, permissionScope: '当前体验授权资源' }));
    const bases = (catalog.knowledge || []).map(b => {
      const documents = (catalog.documents || []).filter(d => String(d.knowledgeBaseId) === String(b.id));
      return { ...b, description: '仅展示授权资料', enabled: true, documentCount: documents.length,
        chunkCount: documents.length && documents.every(d => Number.isInteger(d.chunkCount))
          ? documents.reduce((sum, d) => sum + d.chunkCount, 0) : null,
        retrievalStrategy: 'full', maxTokensPerParagraph: 500, overlappingTokens: 50 };
    });
    const lists = {
      '/api/AIModels/GetPageData': models, '/api/AIKmss/GetMyPageData': bases, '/api/AIKmss/GetList': bases,
      '/api/AIApps/GetPageData': agents, '/api/AIPrompts/GetPageData': (catalog.prompts || []).map((p, i) => ({ ...p, id: `demo-prompt-${i}`, describe: '虚构示例', enabled: true })),
      '/api/AISkillTools/GetPageData': catalog.skills || [], '/api/AISkillTool/GetPageData': catalog.skills || [],
      '/api/AISkillToolManagement/GetPageData': (catalog.skills || []).map((s, i) => ({ ...s, id:`demo-skill-${i}`, enabled:true, describe:'只展示，不执行工具' })),
    };
    if (path in lists) data = pageData(lists[path], cfg);
    else if (path === '/api/AIModels/GetALLList') data = Number(url.searchParams.get('Type') || cfg.params?.Type || 1) === 1 ? models : [];
    else if (['/api/AIApps/GetALLList', '/api/AIApps/GetMyALLList'].includes(path)) data = agents;
    else if (path === '/api/AIApps/GetDetails') {
      data = agents.find(a => a.id === String(url.searchParams.get('Id') || cfg.params?.Id));
      if (!data) throw new Error('该资源未授权');
    } else if (path === '/api/AIKmss/GetALLList') data = bases;
    else if (path === '/api/AIKmss/GetDetails') {
      data = bases.find(b => b.id === String(url.searchParams.get('id') || cfg.params?.id));
      if (!data) throw new Error('该资源未授权');
    } else if (path === '/api/AIKmss/GetDocuments') data = (catalog.documents || []).filter(d => String(d.knowledgeBaseId) === String(url.searchParams.get('Id') || cfg.params?.Id));
    else if (path === '/api/AIKmss/GetDocument') {
      data = (catalog.documents || []).find(d => String(d.id) === String(url.searchParams.get('Id') || cfg.params?.Id));
      if (!data) throw new Error('文档未授权或不在展示范围');
    }
    else if (path === '/api/AITasks/Capabilities') data = { execution: false, handlerTypes: [], reason: '体验账号不可执行后台任务' };
    else if (path === '/api/AITasks/GetTaskList') data = (catalog.tasks || []).map((t, i) => ({ ...t, id: `demo-task-${i}`, handlerType: 'knowledge-check', status: 'schedule_held', enabled:false }));
    else restricted();
  }
  return { data, status: 200, statusText: 'OK', headers: {}, config: cfg };
}
