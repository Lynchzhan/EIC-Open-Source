export function startChatStream(requestId, handlers = {}, options = {}) {
  let cursor = options.lastEventId || ''
  let sequence = Number(cursor.match(/-(\d+)$/)?.[1] || 0)
  const query = cursor ? `&afterEventId=${encodeURIComponent(cursor)}` : ''
  const source = new EventSource(`/api/AIChatHistorys/Stream?requestId=${encodeURIComponent(requestId)}${query}`, { withCredentials: true })
  let finished = false
  let reconnecting = false
  let timer = null
  const armWatchdog = (timeout) => {
    clearTimeout(timer)
    timer = setTimeout(() => finish('error'), timeout)
  }
  const touch = () => armWatchdog(options.idleTimeoutMs ?? 45000)
  armWatchdog(options.connectTimeoutMs ?? 30000)
  source.onopen = () => {
    if (finished) return
    reconnecting = false
    touch()
    handlers.connection?.('connected')
  }
  // 心跳不带事件 ID，也不推进持久游标；长思考期间仍确认连接存活。
  source.addEventListener('heartbeat', () => { if (!finished) touch() })
  const events = ['aimsg', 'processmsg', 'aIToolsContentMsg', 'aIReasoningContentMsg', 'retrieval', 'generationerror', 'reasoningConfig', 'citations']
  const observe = (event) => {
    if (!event.lastEventId) return true
    const prefix = `${requestId}-`
    if (!event.lastEventId.startsWith(prefix)) return false
    const number = Number(event.lastEventId.slice(prefix.length))
    if (!Number.isSafeInteger(number) || number <= sequence) return false
    sequence = number
    cursor = event.lastEventId
    handlers.cursor?.(cursor)
    return true
  }
  const emitText = (eventName, raw) => {
    let text = raw || ''
    let payload = {}
    try {
      payload = JSON.parse(raw) || {}
      text = payload.text ?? payload.msg ?? raw
    } catch {
      text = raw || ''
    }
    if (text == null) text = ''
    handlers[eventName]?.(String(text), payload)
  }
  for (const eventName of events) {
    source.addEventListener(eventName, (event) => {
      if (finished || !observe(event)) return
      touch()
      emitText(eventName, event.data)
    })
  }
  const finish = (kind, payload = {}) => {
    if (finished) return
    finished = true
    clearTimeout(timer)
    timer = null
    try { source.close() } catch { /* ignore */ }
    if (kind === 'done') handlers.done?.(payload)
    else handlers.error?.({ ...payload, status: 'uncertain', partial: false,
      reason: payload.reason || 'stream_connection_closed', retryable: false })
  }
  source.addEventListener('subscriptionerror', (event) => {
    let payload = {}
    try { payload = JSON.parse(event.data || '{}') || {} } catch { /* 保留未知结果 */ }
    finish('error', payload)
  })
  source.addEventListener('done', (event) => {
    if (finished || !observe(event)) return
    let payload = {}
    try { payload = JSON.parse(event.data || '{}') || {} } catch {
      payload = { status: 'failed', reason: 'invalid_terminal' }
    }
    // 兼容历史空 done；未知终态不按成功处理。
    const status = payload.status ?? 'completed'
    finish('done', {
      ...payload, status: ['completed', 'failed', 'cancelled', 'waiting_approval',
        'approval_executing', 'manual_review'].includes(status) ? status : 'failed',
      partial: payload.partial === true, retryable: false,
    })
  })
  source.onerror = () => {
    if (finished) {
      try { source.close() } catch { /* ignore */ }
      return
    }
    if (source.readyState === EventSource.CLOSED) {
      finish('error')
    } else {
      handlers.connection?.('reconnecting')
      // 重连同一个 EventSource，由浏览器携带 Last-Event-ID；不创建新消息。
      // 重复 error 不延长已有期限。
      if (!reconnecting) { reconnecting = true; armWatchdog(options.reconnectTimeoutMs ?? 15000) }
    }
  }
  return {
    close() {
      finish(finished ? 'done' : 'error')
    },
  }
}
