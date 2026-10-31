import assert from 'node:assert/strict';
import { executionPresentation, knowledgeSources } from '../src/shared/ui/agentPresentation.js';

const cases = [
  [{ status: 'generating' }, 'running', '生成中'],
  [{ sending: true }, 'running', '运行中'],
  [{ status: 'running' }, 'running', '运行中'],
  [{ status: 'completed' }, 'success', '已完成'],
  [{ status: 'failed' }, 'error', '执行失败'],
  [{ status: 'approval_required' }, 'warning', '等待人工确认'],
  [{ status: 'waiting_approval' }, 'warning', '等待人工确认'],
  [{ status: 'approval_approved', reason: 'approval_required' }, 'warning', '已批准，尚未执行'],
  [{ status: 'approval_executing' }, 'running', '工具执行中'],
  [{ status: 'manual_review' }, 'warning', '结果需人工复核'],
  [{ status: 'expired' }, 'warning', '审批已过期'],
  [{ status: 'failed', reason: 'approval_required' }, 'warning', '等待人工确认'],
  [{ status: 'cancelled' }, 'neutral', '已停止'],
  [{ status: 'uncertain' }, 'warning', '状态待确认'],
  [{ resumeRequestId: '9223372036854775807' }, 'warning', '连接待恢复'],
  [{ sending: true, resumeRequestId: '9223372036854775807' }, 'running', '运行中'],
  [{ status: 'future_status' }, 'idle', '就绪'],
  ...['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf'].map(
    (status) => [{ status }, 'idle', '就绪']),
  [undefined, 'idle', '就绪'],
];

for (const [input, key, label] of cases) {
  assert.deepEqual(executionPresentation(input), { key, label });
}

const source = { logType: '1', logContent: '制度手册 · 第 12 页' };
assert.deepEqual(knowledgeSources({ aIChatHistorysBindLogs: [
  source, { logType: 1, logContent: '审批规范' }, { logType: '1', logContent: '' },
  { logType: '2', logContent: '工具事件' },
] }), [source, { logType: 1, logContent: '审批规范' }]);
assert.deepEqual(knowledgeSources(), []);
assert.deepEqual(knowledgeSources({}), []);
const citation = { documentId: '402', documentName: 'fixture.pdf', pageNumbers: [1] };
assert.deepEqual(knowledgeSources({ citations: [citation, { type: 'web', url: 'https://example.invalid' }] }), [citation]);
assert.deepEqual(knowledgeSources({ tokenUsage: { citations: [citation] } }), [citation]);
assert.deepEqual(knowledgeSources({ citations: [], tokenUsage: { citations: [citation] } }), [citation]);
assert.deepEqual(knowledgeSources({ citations: [citation], aIChatHistorysBindLogs: [source] }), [citation]);
console.log(JSON.stringify({ passed: cases.length + 7, scope: 'presentation-only' }));
