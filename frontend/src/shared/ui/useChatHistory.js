// 历史分页拥有请求代次、取消和滚动恢复；不会提交模型请求。
import { ref, computed, nextTick, watch, onUnmounted } from 'vue'
import { getAIChatHistorysPageData, getChatEvents } from '../../api/ai/aichathistorys'
import { latestRequestId } from './chatApproval'
import { restoreHistoryEvents, deduplicateHistoryProjections } from './chatHistoryEvents'
import { restoreRunFromHistory } from './chatRunState'

export function useChatHistory({ activeConversationId, messages, isActiveConversation,
  captureReadingPosition, restoreReadingPosition, ensureChatRun, resetInspection,
  refreshInspection, refreshStatistics, loadErrorText }) {
  const currentPage = ref(1), hasMoreMessages = ref(true), loadingMessages = ref(false)
  const loadingHistoryPage = ref(0), historyError = ref(null), pageSize = ref(20)
  const historyUnavailable = computed(() =>
    (loadingMessages.value && loadingHistoryPage.value === 1) || historyError.value?.page === 1)
  let controller = null
  let disposed = false
  const compareChatMessages = (a, b) => {
    const timeDifference = (Date.parse(a.createdAt) || 0) - (Date.parse(b.createdAt) || 0);
    return timeDifference || String(a.id).localeCompare(String(b.id), undefined, { numeric:true });
  };
  
  // 加载聊天记录
  let historyLoadSeq = 0;
  const loadChatHistory = async (chatId, page) => {
    if (!isActiveConversation(chatId)) return;
    if (page !== 1 && (loadingMessages.value || !hasMoreMessages.value)) return;
  
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    const seq = ++historyLoadSeq;
    const loadingPosition = captureReadingPosition();
    let readingPosition = null;
    let keptOlderMessages = false;
    loadingMessages.value = true;
    loadingHistoryPage.value = page;
    historyError.value = null;
    try {
      await nextTick();
      if (!isActiveConversation(chatId) || disposed || seq !== historyLoadSeq) return;
      restoreReadingPosition(loadingPosition, chatId);
      const response = await getAIChatHistorysPageData({
        whereId: chatId,
        pageNum: page,
        pageSize: pageSize.value,
      }, signal);
  
      if (response && response.code === 200 && response.data) {
        if (String(activeConversationId.value) !== String(chatId) || disposed || seq !== historyLoadSeq) {
          return;
        }
        const historyData = response.data.data || [];
  
        // 处理消息格式
        const historyMessages = await restoreHistoryEvents(historyData.map((item) => ({
          id: item.id,
          requestId: item.requestId,
          conversationId: item.aIChatsId,
          isSend: item.isSend,
          content: item.content,
          status: item.status,
          tokenUsage: item.tokenUsage || {},
          citations: item.citations || item.tokenUsage?.citations || [],
          aiReasoningContent: item.aiReasoningContent,
          aiToolsContent: item.aiToolsContent,
          fileNames: item.fileNames || '',
          contentFileUrls: item.contentFileUrls || '',
          attachments: item.attachments || [],
          fileIds: item.fileIds || [],
          aIChatHistorysBindLogs: item.aIChatHistorysBindLogs || [],
          createdAt: item.createTime || new Date().toISOString(),
          cachedInputTokenCount: item.cachedInputTokenCount || 0,
           InputTokenCount: item.InputTokenCount || 0,
           OutputTokenCount: item.OutputTokenCount || 0,
           cachedOutputTokenCount: item.cachedOutputTokenCount || 0,
           totalTokenCount: item.totalTokenCount || 0,
           reasoningTokenCount: item.reasoningTokenCount || 0,
        })), id => getChatEvents(id, signal), { signal });
        if (String(activeConversationId.value) !== String(chatId) || disposed || seq !== historyLoadSeq) return;
        historyMessages.sort(compareChatMessages);
        readingPosition = captureReadingPosition();
  
        // 如果是第一页，直接替换消息列表，否则添加到列表开头（历史消息在前）
        if (page === 1) {
          // 生成结束只合入最新记录，保留正在阅读的旧轮次及历史分页进度。
          keptOlderMessages = !!readingPosition && messages.value.length > 0;
          messages.value = keptOlderMessages
            ? deduplicateHistoryProjections([...new Map([...messages.value, ...historyMessages].map(item => [String(item.id), item])).values()]).sort(compareChatMessages)
            : historyMessages;
          await nextTick();
          if (!isActiveConversation(chatId) || disposed || seq !== historyLoadSeq) return;
          restoreReadingPosition(readingPosition, chatId);
          readingPosition = null;
          const run = ensureChatRun(chatId);
          if (run && !run.sending) {
            restoreRunFromHistory(run, historyMessages);
            const requestId = latestRequestId(historyMessages);
            resetInspection(run, requestId);
            if (requestId) await refreshInspection(chatId, requestId);
          }
          // 审批/Trace 读取期间可能已切换会话，旧分页状态不得回写当前会话。
          if (String(activeConversationId.value) !== String(chatId) || disposed || seq !== historyLoadSeq) return;
        } else {
          messages.value = deduplicateHistoryProjections([...new Map([...historyMessages, ...messages.value]
            .map(item => [String(item.id), item])).values()]).sort(compareChatMessages);
        }
  
        refreshStatistics(chatId);
        // 判断是否还有更多数据
        if (!keptOlderMessages) hasMoreMessages.value = historyData.length >= pageSize.value;
  
        // 更新当前页
        if (page === 1 && !keptOlderMessages) {
          currentPage.value = 1;
        } else if (page !== 1) {
          currentPage.value = page;
        }
      } else {
        throw new Error(response?.msg || "获取聊天记录失败");
      }
    } catch (error) {
      if (String(activeConversationId.value) !== String(chatId) || disposed || seq !== historyLoadSeq) return;
      console.error("加载聊天记录失败:", error);
      historyError.value = { page, text: loadErrorText(error, '聊天记录加载失败') };
    } finally {
      if (!disposed && seq === historyLoadSeq) {
        loadingMessages.value = false;
        nextTick(() => { if (seq === historyLoadSeq) restoreReadingPosition(readingPosition, chatId); });
      }
    }
  };
  
  // 加载更多消息
  const loadMoreMessages = () => {
    if (hasMoreMessages.value && activeConversationId.value) {
      loadChatHistory(activeConversationId.value, currentPage.value + 1);
    }
  };
  
  
  function invalidateHistory() {
    ++historyLoadSeq
    controller?.abort()
    controller = null
    loadingMessages.value = false
  }
  watch(activeConversationId, invalidateHistory, { flush:'sync' })
  onUnmounted(() => { disposed = true; invalidateHistory() })
  return { currentPage, hasMoreMessages, loadingMessages, loadingHistoryPage, historyError,
    historyUnavailable, loadChatHistory, loadMoreMessages, invalidateHistory }
}

