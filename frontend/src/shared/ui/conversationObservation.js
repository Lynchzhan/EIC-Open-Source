export function checkedSession(response, params) {
  const data = response?.data;
  if (response?.code !== 200 || response?.isSuccess === false || !data ||
      data.scope !== params.scope || data.chatId !== String(params.chatId) ||
      !data.summary || !Array.isArray(data.data) || data.page !== params.page ||
      data.pageSize !== params.pageSize || data.data.length > params.pageSize ||
      typeof data.hasMore !== 'boolean') throw new Error('会话统计响应无效');
  return data;
}

export function checkedSessions(response, params) {
  const data = response?.data;
  if (response?.code !== 200 || response?.isSuccess === false || data?.scope !== params.scope ||
      !Array.isArray(data?.data) || data.page !== params.page || data.pageSize !== params.pageSize ||
      data.data.length > params.pageSize || typeof data.hasMore !== 'boolean') throw new Error('会话列表响应无效');
  return data;
}

export function tokenValue(item) {
  if (typeof item?.value !== 'number' || !Number.isFinite(item.value) || item.value < 0) return '未采集';
  return `${new Intl.NumberFormat('zh-CN').format(item.value)}${item.complete ? '' : '（部分）'}`;
}
export function seconds(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits:2 }).format(value / 1000)} 秒` : '未采集';
}

export function showTurnUsage(messages, message) {
  if (!message.requestId) return false;
  const replies = messages.filter(item => item.requestId === message.requestId && item.isSend === false);
  return replies.length ? replies.at(-1).id === message.id : message.isSend === true;
}

export function needsStatisticsSettlement(messages, turns, now = Date.now()) {
  return messages.some(message => {
    const age = now - Date.parse(message.createdAt);
    return message.isSend === true && message.requestId &&
      ['completed', 'failed', 'cancelled', 'canceled'].includes(message.status) &&
      age >= 0 && age < 60000 && !turns[message.requestId]?.timingComplete;
  });
}

// 仅使用提供的父子关系，不猜测相邻事件的调用关系；循环和孤立节点可见。
export function traceLayout(events) {
  const nodes = new Map();
  for (const event of events || []) {
    const key = event.spanId || event.eventKey;
    if (key) nodes.set(key, { ...event, key });
  }
  function depth(node) {
    const seen = new Set([node.key]);
    let parent = node.parentSpanId, value = 0;
    while (parent && nodes.has(parent)) {
      if (seen.has(parent)) return { depth:0, relation:'cycle' };
      seen.add(parent); value += 1; parent = nodes.get(parent).parentSpanId;
    }
    return { depth:Math.min(value, 8), relation:parent ? 'parent_not_visible' : 'recorded' };
  }
  const values = [...nodes.values()].map(node => ({ ...node, ...depth(node) }));
  const starts = values.map(node => Date.parse(node.occurredAt)).filter(Number.isFinite);
  const begin = Math.min(...starts), end = Math.max(...values.map(node => {
    const at = Date.parse(node.occurredAt);
    return Number.isFinite(at) ? at + (Number.isFinite(node.durationMs) ? node.durationMs : 0) : begin;
  }));
  return values.sort((a,b) => Date.parse(a.occurredAt) - Date.parse(b.occurredAt)).map(node => {
    const at = Date.parse(node.occurredAt), elapsed = end - begin;
    return { ...node, offsetMs:Number.isFinite(at) ? Math.max(0, at - begin) : null,
      left:elapsed > 0 && Number.isFinite(at) ? (at - begin) / elapsed * 100 : 0,
      width:elapsed > 0 && Number.isFinite(node.durationMs) ? Math.max(.5, node.durationMs / elapsed * 100) : .5 };
  });
}
