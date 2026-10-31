// 运行详情只绑定当前请求身份，避免历史响应覆盖新会话。
import { getChatApproval, getChatTrace } from '../../api/ai/aichathistorys.js'
import { inspectionMatches } from './chatApproval'
import { traceDetailPage } from './runReliability'

export function useChatInspection({ ensureChatRun, isLiveRun, activeConversationId, activeInspection }) {
  const resetInspection = (run, requestId) => {
    if (String(run.requestId || '') === String(requestId || '')) return;
    run.requestId = requestId ? String(requestId) : null;
    run.inspectionSeq += 1; run.detailSeq += 1;
    run.approval = null; run.trace = null; run.inspectionError = ''; run.inspectionLoading = false;
    run.detailOpen = false; run.detailBusy = false; run.detailError = '';
    run.detailEvents = []; run.detailTools = []; run.detailCursor = 0; run.detailHasMore = false;
  };
  const refreshInspection = async (conversationId, requestId) => {
    const run = ensureChatRun(conversationId);
    if (!run || !requestId) return;
    resetInspection(run, requestId);
    const seq = ++run.inspectionSeq;
    run.approval = null; run.trace = null; run.detailSeq += 1;
    run.detailOpen = false; run.detailBusy = false; run.detailError = '';
    run.detailEvents = []; run.detailTools = []; run.detailCursor = 0; run.detailHasMore = false;
    run.inspectionLoading = true; run.inspectionError = '';
    const results = await Promise.allSettled([getChatApproval(requestId), getChatTrace(requestId)]);
    if (!isLiveRun(conversationId, run) || !inspectionMatches(run, requestId, seq)) return;
    const errors = [];
    for (const [index, result] of results.entries()) {
      if (result.status === 'fulfilled' && result.value?.code === 200) {
        const data = result.value.data;
        if (index === 0) run.approval = data?.approval || null;
        else if (String(data?.requestId) === String(requestId)) run.trace = data;
        else { run.trace = null; errors.push('Trace 请求身份不匹配，请刷新执行记录'); }
      } else {
        if (index === 0) run.approval = null;
        else { run.trace = null; run.detailOpen = false; run.detailEvents = []; run.detailTools = []; run.detailSeq += 1; }
        errors.push(result.reason?.message || '执行记录读取失败');
      }
    }
    run.inspectionError = [...new Set(errors)].join('；');
    run.inspectionLoading = false;
    if (run.trace?.canReadDetail) {
      run.detailOpen = true;
      await loadTraceDetails(conversationId);
    }
  };
  const refreshActiveInspection = () => refreshInspection(activeConversationId.value, activeInspection.value.requestId);
  const loadTraceDetails = async (conversationId) => {
    const run = ensureChatRun(conversationId);
    if (!run?.trace?.canReadDetail || run.detailBusy) return;
    const requestId = run.requestId, seq = ++run.detailSeq;
    run.detailBusy = true; run.detailError = '';
    try {
      const response = await getChatTrace(requestId, { detail: true, afterSequence: run.detailCursor, limit: 30 });
      if (!isLiveRun(conversationId, run) || seq !== run.detailSeq || requestId !== run.requestId) return;
      if (response?.code !== 200 || !response.data?.events) throw new Error('脱敏详情读取失败');
      const page = traceDetailPage(response.data, requestId, run.detailCursor, run.detailEvents);
      run.detailEvents.push(...page.events);
      run.detailTools = page.tools;
      run.detailCursor = page.cursor; run.detailHasMore = page.hasMore;
    } catch (error) {
      if (isLiveRun(conversationId, run) && seq === run.detailSeq && requestId === run.requestId) {
        run.detailError = error.message || '脱敏详情读取失败';
        run.detailEvents = []; run.detailTools = []; run.detailCursor = 0;
        run.detailHasMore = false;
      }
    } finally { if (seq === run.detailSeq) run.detailBusy = false; }
  };
  const toggleTraceDetails = () => {
    const run = activeInspection.value;
    run.detailOpen = !run.detailOpen;
    if (run.detailOpen && !run.detailEvents.length) loadTraceDetails(activeConversationId.value);
  };
  
  return { resetInspection, refreshInspection, refreshActiveInspection, loadTraceDetails, toggleTraceDetails }
}
