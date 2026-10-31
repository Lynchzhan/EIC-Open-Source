// 当前会话的统计请求与补读定时器由本实例持有。
import { ref, watch, onUnmounted } from 'vue'
import { getObservationSession } from '../../api/ai/observability'
import { checkedSession, needsStatisticsSettlement } from './conversationObservation'

export function useConversationStatistics({ activeConversationId, messages, experience }) {
  const sessionStatistics = ref(null), turnStatistics = ref({}), statisticsError = ref(''), statisticsLoading = ref(false)
  const usageTrace = ref(''), usageTraceQuery = ref(null)
  let disposed = false
  let statisticsRevision = 0
  let statisticsTimer
  async function refreshStatistics(chatId, attempt = 0) {
    if (experience) return;
    clearTimeout(statisticsTimer);
    if (!chatId || disposed) return;
    const ticket = ++statisticsRevision;
    statisticsError.value = ''; statisticsLoading.value = true;
    const requestIds = [...new Set(messages.value.map(item => item.requestId).filter(Boolean))].slice(-100);
    const params = { scope:'own', chatId:String(chatId), page:1, pageSize:200, requestIds:requestIds.join(',') };
    try {
      const response = await getObservationSession(params);
      if (disposed || ticket !== statisticsRevision || String(activeConversationId.value) !== String(chatId)) return;
      const data = checkedSession(response, params);
      sessionStatistics.value = data;
      turnStatistics.value = Object.fromEntries(data.data.filter(item => item.requestId).map(item => [item.requestId, item]));
      // 提交后的采集有短暂延迟；只为刚结束的轮次补读两次，不持续轮询历史。
      if (attempt < 2 && needsStatisticsSettlement(messages.value, turnStatistics.value)) {
        statisticsTimer = setTimeout(() => {
          if (!disposed && ticket === statisticsRevision && String(activeConversationId.value) === String(chatId)) {
            refreshStatistics(chatId, attempt + 1);
          }
        }, attempt === 0 ? 800 : 2000);
      }
    } catch (error) {
      if (ticket === statisticsRevision && String(activeConversationId.value) === String(chatId)) {
        sessionStatistics.value = null; turnStatistics.value = {};
        usageTrace.value = ''; usageTraceQuery.value = null;
        statisticsError.value = '会话统计读取失败';
      }
    } finally { if (ticket === statisticsRevision) statisticsLoading.value = false; }
  }
  function openUsageTrace(traceId) {
    const turn = Object.values(turnStatistics.value).find(item => item.traceIds.includes(traceId));
    if (!turn) return;
    const begin = new Date(turn?.startedAt || Date.now());
    const finish = new Date(turn?.finishedAt || Date.now());
    usageTraceQuery.value = { scope:'own', start:new Date(begin.getTime() - 1000).toISOString(),
      end:new Date(Math.max(begin.getTime() + 1000, finish.getTime() + 1000)).toISOString() };
    usageTrace.value = traceId;
  }
  
  function invalidateStatistics() {
    ++statisticsRevision
    clearTimeout(statisticsTimer)
    sessionStatistics.value = null
    turnStatistics.value = {}
    statisticsError.value = ''
    statisticsLoading.value = false
    usageTrace.value = ''
    usageTraceQuery.value = null
  }
  watch(activeConversationId, invalidateStatistics, { flush:'sync' })
  onUnmounted(() => { disposed = true; invalidateStatistics() })
  return { sessionStatistics, turnStatistics, statisticsError, statisticsLoading,
    usageTrace, usageTraceQuery, refreshStatistics, openUsageTrace, invalidateStatistics }
}
