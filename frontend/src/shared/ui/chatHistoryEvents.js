// 只读已持久化事件；读取失败不触发模型调用或工具重放。
export function eventHistoryContent(events) {
  const seen = new Set();
  const tools = [], reasoning = [], answer = [];
  let reasoningConfig = null;
  for (const row of [...(Array.isArray(events) ? events : [])].sort((a, b) => a.sequence - b.sequence)) {
    if (!Number.isSafeInteger(row?.sequence) || row.sequence < 1 || seen.has(row.sequence)) continue;
    seen.add(row.sequence);
    if (row.event === 'reasoningConfig' && row.payload && typeof row.payload === 'object') reasoningConfig = row.payload;
    const text = row.payload?.text;
    if (typeof text !== 'string') continue;
    if (row.event === 'aIToolsContentMsg') tools.push(text);
    if (row.event === 'aIReasoningContentMsg') reasoning.push(text);
    if (row.event === 'aimsg') answer.push(text);
  }
  return { content: answer.join(''), aiToolsContent: tools.join('\n'), aiReasoningContent: reasoning.join(''), reasoningConfig };
}

export function deduplicateHistoryProjections(messages) {
  const persisted = new Set(messages.filter(row => !row.isSend && !row.eventProjection && row.requestId)
    .map(row => String(row.requestId)));
  return messages.filter(row => !row.eventProjection || !persisted.has(String(row.requestId)));
}

export async function restoreHistoryEvents(messages, fetchEvents, { signal } = {}) {
  const terminal = new Set(['completed', 'failed', 'cancelled', 'waiting_approval', 'manual_review']);
  const ids = [...new Set(messages.filter((row) => row.requestId && (!row.isSend || terminal.has(row.status)))
    .map((row) => String(row.requestId)))];
  const results = new Map();
  // 限制并发，避免翻页时对业务库发起无界请求。
  for (let start = 0; start < ids.length; start += 4) {
    if (signal?.aborted) break;
    await Promise.all(ids.slice(start, start + 4).map(async (id) => {
      try {
        const response = await fetchEvents(id);
        if (response?.code !== 200 || !Array.isArray(response.data)) throw new Error('记录响应无效');
        results.set(id, eventHistoryContent(response.data));
      } catch {
        results.set(id, { eventHistoryError: '工具与思考记录加载失败，未重新执行。请重试记录。' });
      }
    }));
  }
  const assistantIds = new Set(messages.filter(row => !row.isSend && row.requestId).map(row => String(row.requestId)));
  return messages.flatMap((row) => {
    if (!row.requestId) return [row];
    const events = results.get(String(row.requestId)) || {};
    if (row.isSend) {
      const user = { ...row, ...(events.eventHistoryError ? { eventHistoryError: events.eventHistoryError } : {}) };
      if (assistantIds.has(String(row.requestId)) || !terminal.has(row.status)
          || !(events.content || events.aiToolsContent || events.aiReasoningContent)) return [user];
      assistantIds.add(String(row.requestId));
      // 取消等路径可能只有持久事件；只读投影恢复，不创建服务端消息或重放模型。
      return [user, { id: 'events-' + row.requestId, requestId: row.requestId,
        conversationId: row.conversationId, isSend: false, createdAt: row.createdAt,
        status: row.status, tokenUsage: row.tokenUsage || {}, citations: row.citations || [],
        ...events, eventProjection: true }];
    }
    return [{ ...row, ...events, content: row.content || events.content || '',
      aiToolsContent: row.aiToolsContent || events.aiToolsContent || '',
      aiReasoningContent: row.aiReasoningContent || events.aiReasoningContent || '' }];
  });
}
