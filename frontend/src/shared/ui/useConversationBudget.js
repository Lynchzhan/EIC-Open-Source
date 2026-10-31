import { onUnmounted, ref, watch } from 'vue'

export function useConversationBudget({ conversationId, isSending, http, disabled = false }) {
  const conversationBudget = ref(null)
  const budgetReadError = ref('')
  let pending = null
  let disposed = false
  let revision = 0

  async function refreshConversationBudget() {
    const id = conversationId.value
    if (disposed || disabled || !id) return
    if (pending?.id === String(id)) return
    pending?.controller.abort()
    const request = { id: String(id), revision: ++revision, controller: new AbortController() }
    pending = request
    const current = () => !disposed && request.revision === revision && request.id === String(conversationId.value)
    try {
      const result = await http.get('/api/AIChatHistorys/Budget', {
        params: { sessionId: id }, signal: request.controller.signal, eicErrorHandled: true,
      })
      if (!current()) return
      if (result.code !== 200) throw new Error('会话预算读取失败')
      conversationBudget.value = result.data
      budgetReadError.value = ''
    } catch (error) {
      if (current() && !request.controller.signal.aborted) budgetReadError.value = error.message || '会话预算读取失败'
    } finally {
      if (pending === request) pending = null
    }
  }

  watch(conversationId, () => {
    revision += 1
    pending?.controller.abort()
    pending = null
    conversationBudget.value = null
    budgetReadError.value = ''
    refreshConversationBudget()
  }, { flush: 'sync' })
  watch(isSending, refreshConversationBudget)
  const timer = setInterval(() => { if (isSending.value) refreshConversationBudget() }, 5000)
  onUnmounted(() => {
    disposed = true
    revision += 1
    clearInterval(timer)
    pending?.controller.abort()
  })
  return { conversationBudget, budgetReadError, refreshConversationBudget }
}
