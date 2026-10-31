// 只解释已有执行结果，不参与请求、恢复或审批流程。
const states = {
  idle: { key: 'idle', label: '就绪' },
  running: { key: 'running', label: '运行中' },
  generating: { key: 'running', label: '生成中' },
  completed: { key: 'success', label: '已完成' },
  failed: { key: 'error', label: '执行失败' },
  cancelled: { key: 'neutral', label: '已停止' },
  uncertain: { key: 'warning', label: '状态待确认' },
  approval_required: { key: 'warning', label: '等待人工确认' },
  waiting_approval: { key: 'warning', label: '等待人工确认' },
  approval_approved: { key: 'warning', label: '已批准，尚未执行' },
  approval_executing: { key: 'running', label: '工具执行中' },
  manual_review: { key: 'warning', label: '结果需人工复核' },
  expired: { key: 'warning', label: '审批已过期' },
};

export function executionPresentation({ sending = false, resumeRequestId, status, reason } = {}) {
  if (sending) return states.running;
  if (Object.hasOwn(states, status) && ['waiting_approval', 'approval_approved', 'approval_executing',
    'manual_review', 'expired'].includes(status)) return states[status];
  if (status === 'approval_required' || reason === 'approval_required') return states.approval_required;
  if (resumeRequestId) return { key: 'warning', label: '连接待恢复' };
  return Object.hasOwn(states, status) ? states[status] : states.idle;
}

export function knowledgeSources(message) {
  const citations = message?.citations?.length ? message.citations : message?.tokenUsage?.citations;
  const structured = (Array.isArray(citations) ? citations : [])
    .filter(source => source && source.type !== 'web' && source.documentId);
  if (structured.length) return structured;
  return (message?.aIChatHistorysBindLogs || []).filter((log) => String(log.logType) === '1' && log.logContent);
}
