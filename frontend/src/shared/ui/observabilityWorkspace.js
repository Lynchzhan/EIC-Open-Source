const numeric = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
export const metric = (value, unit = '') => numeric(value)
  ? `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)}${unit}` : '未提供';
export const percent = (value) => numeric(value) && value <= 1 ? metric(value * 100, '%') : '未提供';
export const coverage = (item) => numeric(item?.providedCalls) && numeric(item?.population)
  ? `${item.providedCalls} / ${item.population} 次调用` : '未提供';
export const categoryLabels = {
  request:'请求', agent_run:'Agent 运行', model_call:'模型调用', tool_call:'工具调用',
  rag_retrieval:'RAG 检索', sse:'SSE', approval:'人工确认', error:'错误', budget:'预算',
};
export const statusLabels = {
  success:'成功', completed:'完成', error:'错误', failed:'失败', timeout:'超时',
  denied:'拒绝', cancelled:'取消', canceled:'取消', running:'运行中', started:'已开始',
  pending:'待处理', waiting:'等待', waiting_approval:'等待确认', disconnected:'已断连',
  reconnected:'已重连', approved:'已确认', rejected:'已拒绝',
};
export const statusLabel = (value) => Object.hasOwn(statusLabels, value)
  ? statusLabels[value] : (typeof value === 'string' && value ? value : '未提供');
export const eventLabel = (value) => Object.hasOwn(categoryLabels, value)
  ? categoryLabels[value] : (typeof value === 'string' && value ? value : '未提供');
export const eventColor = (value) => ['error','failed','timeout','denied'].includes(value)
  ? 'red' : ['success','completed','approved'].includes(value) ? 'green' : 'blue';

export function observationPermissions(identity) {
  const admin = identity?.user?.isSuperAdmin === true;
  const codes = Array.isArray(identity?.permissions) ? identity.permissions : [];
  return { read:admin || codes.includes('*') || codes.includes('ai.chat.read'), admin };
}

export function localTimeInput(date) {
  const value = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return value.toISOString().slice(0, 19);
}

export function observationQuery(draft, admin) {
  const start = new Date(draft.start);
  const end = new Date(draft.end);
  if (!draft.start || !draft.end || !Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) ||
      end <= start || end - start > 30 * 86400000) {
    throw new Error('请选择大于零且不超过 30 天的时间区间');
  }
  if (draft.scope !== 'own' && draft.scope !== 'tenant') throw new Error('查询范围无效');
  if (draft.scope === 'tenant' && !admin) throw new Error('仅管理员可查询本租户聚合');
  const result = { start:start.toISOString(), end:end.toISOString(), scope:draft.scope };
  for (const [key, maximum] of Object.entries({ workspaceId:80, agentId:80, modelId:80, toolName:200,
    category:24, status:32, stage:80, errorStage:80 })) {
    const value = String(draft[key] || '').trim();
    if (value.length > maximum) throw new Error(`筛选字段 ${key} 最多 ${maximum} 个字符`);
    if (value) result[key] = value;
  }
  if (result.category && !Object.hasOwn(categoryLabels, result.category)) throw new Error('观测事件类型无效');
  return result;
}

export function distribution(values) {
  if (!values || typeof values !== 'object' || Array.isArray(values)) return [];
  return Object.entries(values).filter(([, value]) => numeric(value))
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([label, value]) => ({ label, value }));
}

export function checkedObservation(response, kind, params) {
  if (response?.code !== 200 || response?.isSuccess === false) throw new Error(response?.message || '观测响应无效');
  const data = response.data;
  if (!data || typeof data !== 'object') throw new Error('观测响应结构无效');
  if (kind === 'health') return { ...data, langfuse:checkedLangfuseHealth(data.langfuse) };
  if (data.scope !== params.scope || data.window?.timezone !== 'UTC') throw new Error('观测响应范围不一致');
  if (kind === 'summary' && (!data.metrics?.coverage || !data.metrics?.requests || !data.metrics?.usage)) {
    throw new Error('指标响应不完整');
  }
  if (kind === 'events' || kind === 'trace') {
    if (!Array.isArray(data.data) || data.page !== params.page || data.pageSize !== params.pageSize ||
        typeof data.hasMore !== 'boolean' || data.data.length > params.pageSize ||
        (kind === 'trace' && data.traceId !== params.traceId)) throw new Error('事件分页响应无效');
  }
  return kind === 'trace' ? { ...data, langfuse:checkedLangfuseLink(data.langfuse) } : data;
}

function checkedLangfuseHealth(value) {
  const states = ['disabled', 'running', 'stopped', 'unknown'];
  return { state:value?.scope === 'current_process' && states.includes(value?.state) ? value.state : 'unknown' };
}

function checkedLangfuseLink(value) {
  if (value?.available !== true || value?.state !== 'configured' || typeof value.url !== 'string') return null;
  try {
    const url = new URL(value.url);
    const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    if (url.username || url.password || url.search || url.hash ||
        (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) ||
        !/^\/project\/[A-Za-z0-9_-]{1,128}\/traces\/[A-Za-z0-9_-]{1,200}$/.test(url.pathname)) return null;
    return { url:url.href };
  } catch { return null; }
}

export const langfuseStateLabel = (state) => ({ disabled:'已关闭', running:'运行中（不代表投递成功）',
  stopped:'未运行', unknown:'未知' }[state] || '未知');

export function eventTime(value) {
  if (!value) return '未提供';
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { hour12:false }) : '未提供';
}
