// 请求结果只由持久终态确认；断流保留原请求，不隐式重发。
export const isConfirmedTerminal = terminal => [
  'completed', 'failed', 'cancelled', 'waiting_approval', 'manual_review',
].includes(terminal?.status)

export function acceptRunTerminal(run, terminal) {
  run.terminal = terminal
  if (isConfirmedTerminal(terminal)) {
    run.resumeRequestId = null
    run.pendingSubmission = null
    run.stopRequested = false
    return true
  }
  return false
}

export async function cancelRunRequest(run, cancel) {
  run.stopRequested = true
  run.queued = null
  if (!run.resumeRequestId || run.cancelBusy || run.submissionBusy) return false
  const requestId = String(run.resumeRequestId)
  run.cancelBusy = true
  try {
    const response = await cancel(requestId)
    if (response?.code !== 200 || response.data !== true) throw new Error('停止请求尚未确认，请恢复原请求连接核对状态')
    // HTTP 接收只确认取消指令，终态仍由持久事件或历史确认。
    return true
  } finally { run.cancelBusy = false }
}

export function restoreRunFromHistory(run, messages) {
  if (run.sending || run.cancelBusy) return
  const latest = [...messages].reverse().find(item => item.isSend === true && item.requestId)
  if (!latest) return
  const requestId = String(latest.requestId)
  if (run.resumeRequestId && String(run.resumeRequestId) !== requestId) return
  if (['streaming', 'generating', 'approval_executing'].includes(latest.status)) {
    if (!run.resumeRequestId) {
      run.lastEventId = ''
      run.aimessage2 = ''; run.aIReasoningContentMsg = ''; run.aIToolsContentMsg = ''
    }
    run.resumeRequestId = requestId
    run.currentReceivingMsgId = requestId
  } else if (isConfirmedTerminal(latest) && !latest.eventHistoryError && String(run.resumeRequestId) === requestId) {
    acceptRunTerminal(run, { status: latest.status })
    run.lastEventId = ''
    run.aimessage2 = ''; run.aIReasoningContentMsg = ''; run.aIToolsContentMsg = ''
  }
}

export const hasUnconfirmedContent = run => Boolean(run?.resumeRequestId
  && (run.aimessage2 || run.aIReasoningContentMsg || run.aIToolsContentMsg))
