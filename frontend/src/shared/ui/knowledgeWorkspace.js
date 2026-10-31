const STATES = {
  pending: ['待处理', 'default'], empty: ['暂无索引', 'default'],
  queued: ['已提交，等待处理', 'blue'], queued_text: ['文本已提交，等待处理', 'blue'],
  processing: ['处理中', 'blue'], building: ['重建中', 'blue'],
  parsed_pending_embedding: ['已解析，等待向量化', 'blue'],
  ready: ['索引就绪', 'green'], done: ['索引就绪', 'green'],
  parsed: ['已解析，仅文本索引', 'orange'], lexical_only: ['仅文本索引', 'orange'],
  failed: ['处理失败', 'red'], uncertain: ['执行中断，需确认', 'red'],
  staged: ['等待整体切换', 'orange'], legacy: ['历史索引待验证', 'orange'],
  cancelling: ['正在停止，等待当前请求结束', 'orange'],
  cancelled: ['已取消', 'default'], queue_unavailable: ['队列暂不可用', 'orange'],
};
export function knowledgeStatus(record) {
  const raw = typeof record === 'object' && record !== null
    ? record.statusKey ?? record.indexStatus ?? record.status : record;
  const numericStates = { 0: 'pending', 1: 'processing', 2: 'ready', 3: 'failed' };
  const key = Object.hasOwn(numericStates, raw) ? numericStates[raw] : raw;
  const [label, color] = Object.hasOwn(STATES, key) ? STATES[key] : ['状态未提供', 'default'];
  return { key, label, color };
}
export const canRetryDocument = (record) => record?.persisted !== false && !!record?.id
  && ['failed', 'uncertain', 'legacy', 'cancelled', 'parsed'].includes(knowledgeStatus(record).key);
export const isDocumentPending = (record) => ['pending', 'queued', 'queued_text', 'processing',
  'building', 'parsed_pending_embedding', 'cancelling'].includes(knowledgeStatus(record).key);
const STAGE_LABELS = {
  pending: '待处理', running: '执行中', parsing: '解析中', embedding: '向量化中',
  ready: '完成', failed: '失败', building: '重建中', staged: '等待切换',
  lexical_only: '仅文本索引', not_configured: '未配置', uncertain: '需人工确认',
  deleted: '已删除', cancelled: '已取消',
};
function stageValue(value) {
  if (value == null || value === '') return '未提供';
  return Object.hasOwn(STAGE_LABELS, value) ? STAGE_LABELS[value] : String(value);
}
export function documentStages(record = {}) {
  return [
    { key: 'source', title: '来源', value: record.fileId ? '已关联上传文件' : '文本 / 来源未记录' },
    { key: 'parse', title: '解析', value: stageValue(record.parseStatus) },
    { key: 'split', title: '切分', value: Number.isInteger(record.chunkCount)
      ? `${record.chunkCount} 个切片（后端计数）` : '未提供' },
    { key: 'embedding', title: '向量化', value: stageValue(record.embeddingStatus) },
    { key: 'index', title: '索引', value: stageValue(record.indexStatus) },
  ];
}
export function documentFailure(record = {}) {
  if (record.indexError?.waiting === true) {
    return `等待请求额度恢复，已完成 ${record.indexError.completedChunks ?? 0} / ${record.indexError.totalChunks ?? '?'} 个切片，可关闭弹窗`;
  }
  const localReasons = {
    document_source_limit: '文档大小超过本地解析限额',
    document_expansion_limit: '文档解压后过大或含不支持的加密内容',
    document_page_limit: '文档页数超过本地解析限额',
    document_pixel_limit: '文档页面或图片像素超过本地解析限额',
    document_cell_limit: '表格单元格数量超过本地解析限额',
    document_output_limit: '文档提取文本超过本地解析限额',
    document_parse_timeout: '本地文档解析超时，解析进程已停止',
    document_memory_limit: '本地文档解析超出内存限额，解析进程已停止',
    document_parse_failed_or_limit_exceeded: '本地文档解析未完成，请检查文件或解析资源限额',
  };
  const code = record.indexError?.code || '';
  const reason = code.startsWith('parse_failed:local:') ? localReasons[code.slice('parse_failed:local:'.length)] : '';
  const parts = [reason, record.errorMessage, record.indexError?.stage, code]
    .filter((value) => typeof value === 'string' && value.trim()).map((value) => value.trim());
  return [...new Set(parts)].join(' / ');
}
export function ingestionFeedback(response) {
  if (response?.code !== 200 || response?.isSuccess === false || !response?.data
    || typeof response.data !== 'object') throw new Error(response?.msg || '入库请求未成功');
  const data = response.data;
  if (data.status === 'uncertain') return '执行中断，需人工确认，不自动重试';
  if (data.status === 'queue_unavailable' || data.queued === false && data.automaticRetry === false) {
    return '请求已保存，但队列未确认接收；请刷新状态，不代表处理完成';
  }
  if (data.status === 'ready') return '后端已确认索引就绪';
  if (data.status === 'parsed' || data.status === 'lexical_only') return '已解析，仅文本索引；向量化未完成';
  return '入库请求已提交；请以刷新后的文档状态为准';
}
export const optionalMetric = (value, digits = 3) => value == null || value === ''
  || !Number.isFinite(Number(value)) ? '未提供' : Number(value).toFixed(digits);
