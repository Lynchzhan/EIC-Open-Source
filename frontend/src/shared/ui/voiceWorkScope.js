// 每个语音实例独享代次和定时器，切换会话后旧回调不能恢复录音或播报。
export function createVoiceWorkScope() {
  let disposed = false
  let epoch = 0
  const timers = new Set()
  const isCurrent = ticket => !disposed && ticket === epoch
  function invalidate() {
    epoch++
    for (const timer of timers) clearTimeout(timer)
    timers.clear()
  }
  function scheduleTimeout(callback, delay) {
    if (disposed) return null
    const ticket = epoch
    const timer = setTimeout(() => {
      timers.delete(timer)
      if (isCurrent(ticket)) callback()
    }, delay)
    timers.add(timer)
    return timer
  }
  return {
    get disposed() { return disposed },
    capture: () => epoch, isCurrent, scheduleTimeout, invalidate,
    dispose() { disposed = true; invalidate() },
  }
}
