import assert from 'node:assert/strict';
import { knowledgeStatus, canRetryDocument, isDocumentPending, documentStages,
  documentFailure, ingestionFeedback, optionalMetric } from '../src/shared/ui/knowledgeWorkspace.js';
let checks = 0;
const equal = (actual, expected) => { assert.deepEqual(actual, expected); checks++; };
equal(knowledgeStatus({ status: 0 }).key, 'pending');
equal(knowledgeStatus({ status: 2, statusKey: 'uncertain' }).key, 'uncertain');
equal(knowledgeStatus({ indexStatus: 'lexical_only' }).label, '仅文本索引');
equal(knowledgeStatus(null).label, '状态未提供');
equal(knowledgeStatus('unknown').color, 'default');
for (const key of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf']) {
  equal(knowledgeStatus(key), { key, label: '状态未提供', color: 'default' });
  equal(knowledgeStatus({ statusKey: key }), { key, label: '状态未提供', color: 'default' });
  equal(canRetryDocument({ id: 'reserved-key', statusKey: key }), false);
  equal(isDocumentPending({ statusKey: key }), false);
  const stages = documentStages({ parseStatus: key, embeddingStatus: key, indexStatus: key });
  equal(stages.every((stage) => typeof stage.value === 'string'), true);
  equal(stages.filter((stage) => ['parse', 'embedding', 'index'].includes(stage.key))
    .map((stage) => stage.value), [key, key, key]);
}
for (const [key, expected] of [[0, 'pending'], ['0', 'pending'], [1, 'processing'],
  ['1', 'processing'], [2, 'ready'], ['2', 'ready'], [3, 'failed'], ['3', 'failed']]) {
  equal(knowledgeStatus(key).key, expected);
}
for (const key of ['failed', 'uncertain', 'legacy']) {
  equal(canRetryDocument({ id: '1', statusKey: key }), true);
}
for (const key of ['queued', 'processing', 'ready', 'staged', 'parsed']) {
  equal(canRetryDocument({ id: '1', statusKey: key }), false);
}
equal(canRetryDocument({ statusKey: 'failed' }), false);
equal(canRetryDocument({ id: '1', persisted: false, statusKey: 'failed' }), false);
equal(isDocumentPending({ statusKey: 'cancelled' }), false);
equal(isDocumentPending({ statusKey: 'building' }), true);
equal(isDocumentPending({ statusKey: 'parsed' }), false);
equal(documentStages({}).map((s) => s.value), ['文本 / 来源未记录', '未提供', '未提供', '未提供', '未提供']);
equal(documentStages({ fileId: '2', chunkCount: 0, parseStatus: 'ready', embeddingStatus: 'not_configured',
  indexStatus: 'lexical_only' }).map((s) => s.value),
['已关联上传文件', '完成', '0 个切片（后端计数）', '未配置', '仅文本索引']);
equal(documentFailure({ indexError: { stage: 'embedding', code: 'MODEL_UNAVAILABLE', secret: 'hidden' } }),
  'embedding / MODEL_UNAVAILABLE');
equal(documentFailure({ errorMessage: 'failed', indexError: { code: 'failed' } }), 'failed');
equal(optionalMetric(null), '未提供');
equal(optionalMetric(''), '未提供');
equal(optionalMetric('bad'), '未提供');
equal(optionalMetric(0), '0.000');
equal(optionalMetric(0.75), '0.750');
equal(ingestionFeedback({ code: 200, data: { queued: false, automaticRetry: false } }),
  '请求已保存，但队列未确认接收；请刷新状态，不代表处理完成');
equal(ingestionFeedback({ code: 200, data: { status: 'parsed' } }), '已解析，仅文本索引；向量化未完成');
equal(ingestionFeedback({ code: 200, data: { status: 'uncertain' } }), '执行中断，需人工确认，不自动重试');
equal(ingestionFeedback({ code: 200, data: { queued: true } }), '入库请求已提交；请以刷新后的文档状态为准');
assert.throws(() => ingestionFeedback({ code: 200, isSuccess: false, data: {} })); checks++;
console.log(`knowledgeWorkspace: ${checks} checks passed`);
