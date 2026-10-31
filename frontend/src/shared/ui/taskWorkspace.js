const statuses = {
  ready: ['待执行', 'waiting'], pending: ['待派发', 'waiting'], queued: ['已排队', 'waiting'],
  queue_unavailable: ['队列暂不可用', 'attention'], running: ['执行中', 'active'],
  retrying: ['等待重试', 'waiting'], success: ['已完成', 'done'], failed: ['执行失败', 'attention'],
  cancelled: ['已取消', 'inactive'], uncertain: ['结果待确认', 'attention'],
  schedule_held: ['计划已暂停', 'attention'], missed: ['已错过计划时间', 'attention'],
  permission_revoked: ['权限已撤销', 'attention'], disabled: ['已禁用', 'inactive'],
  locked: ['等待资源锁', 'waiting'], missing: ['任务不存在', 'attention'],
  already_claimed: ['正在处理', 'active'],
  approval_required: ['等待人工审批', 'attention'], budget_exceeded: ['预算已用尽', 'attention'],
};

export const taskStatus = (status) => {
  const [label, bucket] = Object.hasOwn(statuses, status) ? statuses[status] : ['状态未提供', 'attention'];
  return { label, bucket, color: { active: 'processing', done: 'success', attention: 'warning' }[bucket] || 'default' };
};

export function taskPermissions() {
  try {
    const permissions = JSON.parse(localStorage.getItem('UserPermissions') || '[]');
    const user = JSON.parse(localStorage.getItem('user') || 'null');
    const has = (code) => user?.isSuperAdmin === true ||
      (Array.isArray(permissions) && (permissions.includes('*') || permissions.includes(code)));
    return { read: has('task.read'), write: has('task.write'),
      execute: has('task.write'), knowledge: has('knowledge.read'), agent: has('ai.resource.read') && has('ai.chat.write') };
  } catch {
    return { read: false, write: false, execute: false };
  }
}

export function taskRows(data) {
  const rows = data?.list ?? data?.items ?? data;
  if (!Array.isArray(rows)) throw new Error('任务列表响应无效，请重试');
  return rows.map((item, index) => {
    if (item && typeof item === 'object' && !Array.isArray(item)) return {
      ...item, id: item.id == null ? '' : String(item.id),
      key: item.id == null ? `unbound-${index}` : String(item.id),
      name: String(item.name || '未命名任务'), status: String(item.status || ''),
      enabled: item.enabled === true, canExecute: item.canExecute === true,
      last: item.last ?? item.lastRunAt ?? null, next: item.next ?? null,
    };
    if (typeof item !== 'string') throw new Error('任务条目响应无效，请重试');
    // 旧文本契约只能阅读，缺失绑定 ID 时不开放写入或记录查询。
    const values = Object.fromEntries(String(item).split(' | ').map((part) => {
      const colon = part.indexOf(':');
      return [part.slice(0, colon), part.slice(colon + 1)];
    }));
    return { id: '', key: `legacy-${index}`, name: values.name || '旧格式任务',
      cron: values.Cron, next: values.Next, last: values.Last, timeZone: values.TimeZone,
      status: '', enabled: false, canExecute: false };
  });
}

export function filterTasks(rows, query = '', bucket = 'all') {
  const needle = String(query).trim().toLocaleLowerCase();
  return rows.filter((row) => (!needle || [row.name, row.id, row.handlerType].some(
    (value) => String(value ?? '').toLocaleLowerCase().includes(needle))) &&
    (bucket === 'all' || taskStatus(row.status).bucket === bucket));
}

export function taskCounts(rows) {
  return rows.reduce((counts, row) => {
    counts[taskStatus(row.status).bucket] += 1;
    return counts;
  }, { waiting: 0, active: 0, attention: 0, done: 0, inactive: 0 });
}

export function taskDate(value) {
  if (value == null || value === '') return '未提供';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间格式未识别' : date.toLocaleString('zh-CN', { hour12: false });
}

export function taskDetail(data, id) {
  if (!data || String(data.taskId) !== id || !Array.isArray(data.runs) ||
    data.runs.some((run) => !run || typeof run !== 'object' || run.id == null)) {
    throw new Error('执行记录响应无效，请重试');
  }
  return data;
}

// 只呈现约定的公开结果字段，不倾倒未知 payload、密钥或内部工具输出。
export function taskReport(run) {
  const result = run?.result;
  const report = result?.report;
  if (result?.handlerType === 'agent' && result.schemaVersion === 1) {
    const text = (value) => typeof value === 'string' || typeof value === 'number' ? taskError(value) : '未提供';
    return [['运行状态', taskStatus(result.status).label], ['回答', typeof result.answer === 'string' ? taskError(result.answer, 32000) + (result.answer.length > 32000 ? '\n[仅展示前 32000 字符]' : '') : '未提供'],
      ['停止原因', taskStopReason(result.stopReason)], ['执行图版本', text(result.graphVersion)],
      ['工具总调用数（含 wrapper/子步骤）', Number.isInteger(result.toolCallCount) && result.toolCallCount >= 0 && result.toolCallCount <= 24 ? String(result.toolCallCount) : '未提供'],
      ['重试安全性', result.safeToRetry === false ? '不可安全重试，请先核对已发生动作' : '未提供安全重试保证']];
  }
  if (result?.handlerType !== 'knowledge-check' || !report || report.readOnly !== true) return [];
  const text = (value) => value == null ? '未提供' : String(value);
  const count = (value) => Number.isInteger(value) && value >= 0 ? String(value) : '未提供';
  return [['知识库 ID', text(report.knowledgeBaseId)], ['索引状态', text(report.indexStatus)],
    ['文档数', count(report.documentCount)], ['检查时间', taskDate(report.checkedAt)], ['动作性质', '只读检查']];
}

export function taskStopReason(value) {
  const labels = { approval_required:'需要人工审批；当前任务页不提供审批恢复',
    max_steps:'达到最大执行步数', budget_exceeded:'预算已用尽', cancelled:'已取消',
    completed:'正常结束', success:'正常结束', unknown:'结果不确定，先核对已发生动作' };
  return Object.hasOwn(labels, value) ? labels[value] : taskError(value) || '未提供';
}

export function taskError(value, limit = 1000) {
  return String(value || '').replace(
    /((?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi, '$1[已脱敏]')
    .replace(/Bearer\s+[A-Za-z0-9._~+/-]+/gi, 'Bearer [已脱敏]').slice(0, limit);
}
