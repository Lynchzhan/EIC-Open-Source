import http from '../../utils/http';
export const getAIModelsPageData = (params) => {
  return http.post('/api/AIModels/GetPageData', params);
};

export const addEditAIModel = (data) => {
  return http.post('/api/AIModels/AddEdit', data);
};
export const testAIModelConnection = (data, config = {}) => {
  return http.post('/api/AIModels/TestConnection', data, { eicErrorHandled:true, ...config });
};
export const getModelTestPreferences = config => http.get('/api/User/ModelTestPreferences', { eicErrorHandled:true, ...config });
export const saveModelTestPreferences = data => http.put('/api/User/ModelTestPreferences', data, { eicErrorHandled:true });
export const deleteAIModel = (id, revision, replacementId) => {
  return http.delete('/api/AIModels/Delete', { params: { Id: id, revision, replacementId: replacementId || undefined } });
};
export const getModelDeleteImpact = id => http.get('/api/AIModels/DeleteImpact', { params: { Id: id } });
export const getPendingModelReferences = kind => http.get('/api/AIModels/PendingReferences', { params: { kind } });
export const revealModelSecret = id => http.get('/api/AIModels/Secret', { params: { Id: id } });
export const fetchUpstreamModels = data => http.post('/api/AIModels/FetchModels', data, { timeout: 20000 });
export const getAIModelsALLList = (type) => {
  return http.get('/api/AIModels/GetALLList?Type='+type);
};
