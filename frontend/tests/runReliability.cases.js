import assert from 'node:assert/strict';
import { knownNumber, reliabilityView, reliabilityHint, traceDetailPage } from '../src/shared/ui/runReliability.js';
for (const missing of [null, undefined, '', false, -1, Infinity, NaN, '12']) assert.equal(knownNumber(missing), '未提供');
assert.equal(knownNumber(0), '0');
assert.equal(reliabilityView().degraded, '未提供');
const view = reliabilityView({ checkpointSeq: 2, currentNode: 'tool_pending', status: 'manual_review' }, {
  providerInvocations: 1, retryCount: 0, degraded: false,
  budget: { inputLimit: 100, inputEstimate: 0, droppedHistoryMessages: 0, contextManifest: {
    entries: [{ source: 'history', retained: true, content: 'SECRET' }, { source: 'history', retained: false },
      { source: 'unrecognized-secret-label', retained: null }], hash: 'a'.repeat(64),
  } },
});
assert.equal(view.inputEstimate, '0');
assert.equal(view.degraded, '未降级');
assert.equal(view.checkpoint, '2');
assert.equal(view.retries, '0');
assert.equal(view.sources[0].retained, 1);
assert.equal(view.sources[0].dropped, 1);
assert.equal(JSON.stringify(view).includes('SECRET'), false);
assert.equal(JSON.stringify(view).includes('unrecognized-secret-label'), false);
assert.match(view.hint, /不得自动重放/);
assert.match(reliabilityHint('circuit_open'), /熔断/);
for (const key of ['constructor', '__proto__', 'toString']) {
  assert.equal(typeof reliabilityHint(key), 'string');
  assert.equal(reliabilityView({}, {
    budget: { contextManifest: { entries: [{ source: key, retained: true }] } },
  }).sources[0].source, 'other');
}
assert.match(reliabilityView({}, { degraded: true, fallbackType: 'rule_notice' }).fallback, /非模型生成/);
const page = { requestId: 'r', nextSequence: 2, hasMore: true, events: [{ sequence: 1 }, { sequence: 2 }], tools: [] };
assert.equal(traceDetailPage(page, 'r', 0, []).cursor, 2);
assert.throws(() => traceDetailPage(page, 'other', 0, []), /身份/);
assert.throws(() => traceDetailPage({ ...page, nextSequence: 0, events: [] }, 'r', 0, []), /未前进/);
assert.throws(() => traceDetailPage({ ...page, events: [{ sequence: 2 }, { sequence: 1 }] }, 'r', 0, []), /序号/);
assert.throws(() => traceDetailPage(page, 'r', 0, Array(3000).fill({ sequence: 0 })), /上限/);
assert.throws(() => traceDetailPage({ ...page, events: [null] }, 'r', 0, []), /Trace 事件/);
for (const tool of [null, {}, { callId: '' }]) {
  assert.throws(() => traceDetailPage({ ...page, tools: [tool] }, 'r', 0, []), /Trace 工具/);
}
console.log('P9_RELIABILITY_CASES_PASSED');
