const metricKeys = ['elapsedMs', 'citationRate', 'ndcg', 'noResultRate', 'denseCount', 'sparseCount', 'denseStatus', 'rerankStatus', 'strategy', 'strategyLabel'];
const resultKeys = ['chunkId', 'documentId', 'documentName', 'score', 'channel', 'content', 'pageNumbers', 'vectorScore', 'bm25Score'];
const pick = (object, keys) => Object.fromEntries(keys.filter((key) => Object.hasOwn(object, key)).map((key) => [key, object[key]]));

export function recallSnapshot(request, response, browserElapsedMs, completedAt = new Date().toISOString()) {
  if (!Array.isArray(response?.results)) throw new Error('召回响应缺少结果列表');
  if (!response.results.every((row) => row && typeof row === 'object' && !Array.isArray(row))) throw new Error('召回切片响应无效');
  const snapshot = {
    request: { knowledgeBaseId:String(request.knowledgeBaseId), query:request.query, k:request.k, retrievalStrategy:request.retrievalStrategy || 'kb' },
    response: { ...pick(response, metricKeys), results:response.results.map((row) => pick(row, resultKeys)) },
    browserElapsedMs, completedAt,
  };
  return JSON.parse(JSON.stringify(snapshot));
}

export function comparableRecall(left, right) {
  return Boolean(left && right && ['knowledgeBaseId', 'query', 'k'].every((key) => left.request[key] === right.request[key]));
}

export function recallEvidence(current, baseline) {
  if (!current) throw new Error('尚无真实召回证据');
  return { schemaVersion:1, source:'EIC recall API', scope:'current page session', current,
    baseline:comparableRecall(current, baseline) ? baseline : null,
    comparable:comparableRecall(current, baseline) };
}
