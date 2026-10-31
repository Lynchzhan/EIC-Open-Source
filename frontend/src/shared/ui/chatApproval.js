// 审批身份仅来自服务端票据，重试复用同一动作的幂等键。
export function createApprovalIdempotencyKey(source = globalThis.crypto) {
  if (typeof source?.randomUUID === 'function') return source.randomUUID();
  if (typeof source?.getRandomValues !== 'function') {
    throw new Error('当前浏览器缺少安全随机数能力，审批尚未提交，请更换浏览器后重试');
  }
  // 普通 HTTP 不提供 randomUUID，但仍可使用密码学随机数生成 UUID v4。
  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function approvalTicket(approval, idempotencyKey, decision) {
  if (!approval || !idempotencyKey) throw new Error('审批票据尚未加载');
  const { requestId, runId, callId, inputDigest, checkpointSeq } = approval;
  if (!requestId || !runId || !callId || !/^[a-f0-9]{64}$/.test(inputDigest)
      || !Number.isSafeInteger(checkpointSeq) || checkpointSeq < 1) {
    throw new Error('审批票据格式不正确，请刷新后复核');
  }
  return { requestId, runId, callId, inputDigest, checkpointSeq, idempotencyKey,
    ...(decision ? { decision } : {}) };
}

export function continuationCursor(requestId, current, suggested) {
  const prefix = `${requestId}-`;
  const sequence = (value) => {
    if (!String(value || '').startsWith(prefix)) return -1;
    const suffix = value.slice(prefix.length);
    const number = Number(suffix);
    return /^\d+$/.test(suffix) && Number.isSafeInteger(number) ? number : -1;
  };
  return sequence(suggested) > sequence(current) ? suggested
    : sequence(current) >= 0 ? current : '';
}

export function latestRequestId(messages) {
  // 历史 assistant ID 与 requestId 不等价，绝不由 ID 猜测请求身份。
  return [...messages].reverse().find((item) => item.requestId)?.requestId || null;
}

export function inspectionMatches(run, requestId, sequence) {
  return Boolean(run && String(run.requestId) === String(requestId) && run.inspectionSeq === sequence);
}
