import http from '../../utils/http';

export const getAIKmssList = (params) => {
  return http.post('/api/AIKmss/GetList', params);
};

export const getAIKmssALLList = () => {
  return http.get('/api/AIKmss/GetALLList');
};

export const getAIKmssPageData = (params) => {
  return http.post('/api/AIKmss/GetMyPageData', params);
};

export const getAIKmssDetails = (id) => {
  return http.get(`/api/AIKmss/GetDetails?id=${id}`);
};

export const addEditAIKmss = (data) => {
  return http.post('/api/AIKmss/AddEdit', data);
};

export const deleteAIKmss = (id) => {
  return http.delete(`/api/AIKmss/Delete?Id=${id}`);
};

export const getKnowledgeDocuments = (id) => {
  return http.get(`/api/AIKmss/GetDocuments?Id=${id}`);
};

export const getKnowledgeDocument = (id) => {
  return http.get(`/api/AIKmss/GetDocument?Id=${id}`);
};

export const updateKnowledgeDocument = (data) => {
  return http.post('/api/AIKmss/UpdateDocument', data);
};

export const replaceKnowledgeDocumentFile = (id, fileId) => {
  return http.post(`/api/AIKmss/ReplaceDocumentFile?Id=${id}&fileId=${fileId}`);
};

export const deleteKnowledgeDocument = (id) => {
  return http.delete(`/api/AIKmss/DeleteDocument?Id=${id}`);
};

export const importKnowledgeText = (data) => {
  return http.post('/api/AIKmss/ImportText', data);
};

export const importKnowledgeFile = (id, fileId) => {
  return http.post(`/api/AIKmss/ImportFile?Id=${id}&fileId=${fileId}`);
};

export const retryKnowledgeDocument = (id) => {
  return http.post('/api/AIKmss/RetryDocument', { id, acknowledgeDuplicateCall: true });
};

export const importKnowledgeFiles = (data) => {
  return http.post('/api/AIKmss/ImportFiles', data);
};

export const retryKnowledgeDocuments = (data) => {
  return http.post('/api/AIKmss/RetryDocuments', data);
};

export const cancelKnowledgeDocument = (id) => {
  return http.post('/api/AIKmss/CancelDocument', { id });
};

export const saveKnowledgeRetrieval = (data) => {
  return http.post('/api/AIKmss/SaveRetrieval', data);
};

export const recallKnowledgeTest = (data) => {
  return http.post('/api/AIKmss/RecallTest', data);
};

export const getKnowledgeEvalSets = (knowledgeBaseId) => {
  return http.get(`/api/AIKmss/GetEvalSets?knowledgeBaseId=${knowledgeBaseId}`);
};

export const saveKnowledgeEvalSet = (data) => {
  return http.post('/api/AIKmss/SaveEvalSet', data);
};

export const deleteKnowledgeEvalSet = (id) => {
  return http.delete(`/api/AIKmss/DeleteEvalSet?Id=${id}`);
};

export const getKnowledgeEvalCases = (evalSetId) => {
  return http.get(`/api/AIKmss/GetEvalCases?evalSetId=${evalSetId}`);
};

export const saveKnowledgeEvalCase = (data) => {
  return http.post('/api/AIKmss/SaveEvalCase', data);
};

export const deleteKnowledgeEvalCase = (id) => {
  return http.delete(`/api/AIKmss/DeleteEvalCase?Id=${id}`);
};

export const runKnowledgeEvalSet = (data) => {
  return http.post('/api/AIKmss/RunEvalSet', data);
};
