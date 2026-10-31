import { budgetMessages } from './budgetMessages.js';

const HINTS = {
  ...budgetMessages,
  provider_outcome_uncertain: '模型结果未确认，系统未自动重发；请先复核调用记录。',
  tool_outcome_uncertain: '工具副作用未确认，系统未自动重放；请先人工复核。',
  tool_timeout: '工具执行超时，系统未自动重试，避免重复产生副作用。',
  circuit_open: '该请求被熔断或探测门禁拦截，请稍后重试或联系管理员配置备用依赖。',
  input_budget_exceeded: '输入超过预算，请缩短问题或由管理员调整预算。',
  output_budget_exceeded: '输出达到预算，已生成的内容保留；调整预算后再发起新请求。',
  tool_budget_exceeded: '本轮 Agent 执行时限或步数预算已用尽，已停止后续调用。',
  knowledge_configuration_error: '知识库模型配置无效，请检查向量模型、重排模型及索引版本。',
  concurrency_budget_exceeded: '并发预算已用尽，请稍后重试。',
  rate_budget_exceeded: '请求速率预算已用尽，请稍后重试。',
  cost_budget_exceeded: '保守成本预留预算已用尽，请由管理员检查策略。',
  budget_not_configured: '预算配置不完整，请联系管理员。',
  provider_rate_limited: '模型服务正在限流，请稍后重试。',
  model_not_configured: '尚未配置可用模型，请联系管理员。',
  approval_required: '工具需要人工确认；批准决定与实际执行是两个独立步骤。',
};
const SOURCES = {
  system: '系统约束', history: '会话历史', current_question: '本轮问题',
  short_term: '短期记忆', session: '会话记忆', memory: '记忆',
  long_term: '长期记忆', rag: '检索证据', attachment: '附件', tool_result: '工具结果',
};
export const knownNumber = (value) =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? String(value) : '未提供';
export function reliabilityHint(code, status) {
  if (status === 'manual_review') return '运行需要人工复核，未知结果不得自动重放；checkpoint 不等于安全重试许可。';
  return code ? (Object.hasOwn(HINTS, code) ? HINTS[code]
    : '请求未完成，请保留错误标识并复核执行记录；不要重复提交未知副作用操作。') : '';
}
export function reliabilityView(trace = {}, usage = {}) {
  const budget = usage.budget || {};
  const manifest = budget.contextManifest || {};
  const sources = new Map();
  for (const row of Array.isArray(manifest.entries) ? manifest.entries : []) {
    const key = typeof row?.source === 'string' && Object.hasOwn(SOURCES, row.source) ? row.source : 'other';
    const group = sources.get(key) || { source: key, label: SOURCES[key] || '其他来源', retained: 0, dropped: 0, unknown: 0 };
    if (row?.retained === true) group.retained += 1;
    else if (row?.retained === false) group.dropped += 1;
    else group.unknown += 1;
    sources.set(key, group);
  }
  const errorCode = trace.errorCode || usage.reason || '';
  return {
    checkpoint: knownNumber(trace.checkpointSeq), node: trace.currentNode || '未提供',
    invocations: knownNumber(trace.providerInvocations ?? usage.providerInvocations),
    retries: knownNumber(usage.retryCount), inputLimit: knownNumber(budget.inputLimit),
    inputEstimate: knownNumber(budget.inputEstimate), dropped: knownNumber(budget.droppedHistoryMessages),
    toolReserve: knownNumber(manifest.toolSchemaReserve),
    degraded: usage.degraded === true ? '已降级' : usage.degraded === false ? '未降级' : '未提供',
    fallback: usage.fallbackType === 'rag_excerpt' ? '授权检索证据摘录（非模型生成）'
      : usage.fallbackType === 'rule_notice' ? '规则提示（非模型生成）' : '未提供',
    errorCode, hint: reliabilityHint(errorCode, trace.status), sources: [...sources.values()],
    hash: /^[a-f0-9]{64}$/.test(manifest.hash || '') ? manifest.hash : '',
  };
}

export function traceDetailPage(data, requestId, cursor, knownEvents) {
  if (!data || String(data.requestId) !== String(requestId) || !Array.isArray(data.events)
      || !Number.isSafeInteger(data.nextSequence) || data.nextSequence < cursor
      || typeof data.hasMore !== 'boolean' || data.events.length > 30) {
    throw new Error('Trace 分页身份或格式无效，请刷新执行记录');
  }
  let previous = cursor;
  for (const event of data.events) {
    if (!event || typeof event !== 'object' || !Number.isSafeInteger(event.sequence) || event.sequence <= previous) {
      throw new Error('Trace 事件序号未前进，请刷新执行记录');
    }
    previous = event.sequence;
  }
  if (data.nextSequence !== previous || (data.hasMore && previous === cursor)) {
    throw new Error('Trace 分页游标未前进，已停止继续读取');
  }
  if (knownEvents.length + data.events.length > 3000) throw new Error('已达到 3000 条事件读取上限，请缩小范围或联系管理员');
  if (Array.isArray(data.tools) && data.tools.some((tool) => !tool || typeof tool !== 'object'
      || typeof tool.callId !== 'string' || !tool.callId)) {
    throw new Error('Trace 工具摘要格式无效，请刷新执行记录');
  }
  const seen = new Set(knownEvents.map((event) => event.sequence));
  return { events: data.events.filter((event) => !seen.has(event.sequence)),
    tools: Array.isArray(data.tools) ? data.tools.slice(0, 100) : [], cursor: data.nextSequence, hasMore: data.hasMore };
}
