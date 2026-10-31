// 会话运行对象与浏览器订阅由页面实例拥有；断开订阅不重发模型请求。
import { reactive, computed, onUnmounted, ref } from 'vue'

export function useChatRuns({ isAlive }) {
  let disposed = false
  const chatRuns = reactive({});
  const deletedConversationIds = reactive(new Set());
  const isLiveRun = (id, run) => !disposed && isAlive() && Boolean(run)
    && !deletedConversationIds.has(String(id)) && chatRuns[String(id)] === run;
  const ensureChatRun = (conversationId) => {
    const id = String(conversationId || "");
    if (disposed || !isAlive() || !id || deletedConversationIds.has(id)) return null;
    if (!chatRuns[id]) {
      chatRuns[id] = {
        sending: false,
        queued: null,
        abortController: null,
        connection: null,
        aimessage: "",
        aimessage2: "",
        aIToolsContentMsg: "",
        aIReasoningContentMsg: "",
        currentReceivingMsgId: null,
        lastSentMessage: "",
        lastSentMessageId: null,
        canStopSend: false,
        stopArmTimer: null,
        userStopped: false,
        stopRequested: false,
        cancelBusy: false,
        submissionBusy: false,
        messagesSnapshot: null,
        historyPage: 1,
        historyHasMore: true,
        draftText: '',
        draftFiles: [],
        resumeRequestId: null,
        lastEventId: '',
        pendingSubmission: null,
        requestId: null,
        approval: null,
        trace: null,
        inspectionSeq: 0,
        inspectionLoading: false,
        inspectionError: '',
        approvalBusy: '',
        approvalKeys: {},
        detailOpen: false,
        detailBusy: false,
        detailError: '',
        detailSeq: 0,
        detailEvents: [],
        detailTools: [],
        detailHasMore: false,
        detailCursor: 0,
      };
    }
    return chatRuns[id];
  };
  const parallelRunCount = computed(() => Object.values(chatRuns).filter((run) => run && run.sending).length);
  const isConversationSending = (id) => Boolean(chatRuns[String(id)]?.sending);
  const bindActiveField = (activeId, field, fallback) => {
    const unbound = ref(fallback);
    return computed({
      get: () => {
        const run = ensureChatRun(activeId.value);
        return run ? run[field] : unbound.value;
      },
      set: value => {
        const run = ensureChatRun(activeId.value);
        if (run) run[field] = value;
        else unbound.value = value;
      },
    });
  };
  
  onUnmounted(() => {
    disposed = true
    for (const run of Object.values(chatRuns)) {
      ++run.inspectionSeq
      ++run.detailSeq
      clearTimeout(run.stopArmTimer)
      try { run.abortController?.abort() } catch { /* 客户端资源可能已关闭 */ }
      try { run.connection?.close?.() } catch { /* 服务端请求由其持久租约继续管理 */ }
      run.connection = null
      run.sending = false
    }
  })
  return { chatRuns, deletedConversationIds, isLiveRun, ensureChatRun, parallelRunCount, isConversationSending, bindActiveField }
}
