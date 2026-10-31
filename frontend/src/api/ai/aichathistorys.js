import http from '../../utils/http';
export const getAIChatHistorysPageData = (params, signal) => {
  const payload = { ...params };
  if (payload.whereId && !payload.aIChatsId) payload.aIChatsId = payload.whereId;
  return http.post('/api/AIChatHistorys/GetPageData', payload, { signal, eicErrorHandled: true });
};

export const addAIChatHistorys = (data, signal) => {
  return http.post('/api/AIChatHistorys/Add', data, { signal, timeout: 30000 });
};
export const cancelAIChatRequest = (requestId) =>
  http.post('/api/AIChatHistorys/Cancel', null, { params: { requestId }, timeout: 15000, eicErrorHandled: true });
export const deleteAIChatHistorys = (id) => {
  return http.delete('/api/AIChatHistorys/Delete?Id='+id);
};

export const getChatApproval = (requestId) =>
  http.get('/api/AIChatHistorys/GetApproval', { params: { requestId }, timeout: 15000 });
export const decideChatApproval = (ticket) =>
  http.post('/api/AIChatHistorys/DecideApproval', ticket, { timeout: 30000 });
export const continueChatApproval = (ticket) =>
  http.post('/api/AIChatHistorys/ContinueApprovedTool', ticket, { timeout: 30000 });
export const getChatTrace = (requestId, options = {}) =>
  http.get('/api/AIChatHistorys/GetTrace', { params: { requestId, ...options }, timeout: 15000 });
export const getChatEvents = (requestId, signal) =>
  http.get('/api/AIChatHistorys/GetEvents', { params: { requestId }, signal,
    eicErrorHandled: Boolean(signal), timeout: 15000 });
