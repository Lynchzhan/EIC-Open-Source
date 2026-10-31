import assert from 'node:assert/strict';
import { approvalTicket, createApprovalIdempotencyKey, continuationCursor, latestRequestId, inspectionMatches } from '../src/shared/ui/chatApproval.js';
const approval = { requestId: 'user-1', runId: 'run-1', callId: 'call-1',
  inputDigest: 'a'.repeat(64), checkpointSeq: 1, canDecide: true, raw: 'never-send' };
assert.deepEqual(approvalTicket(approval, 'stable-key', 'approve'), {
  requestId: 'user-1', runId: 'run-1', callId: 'call-1', inputDigest: 'a'.repeat(64),
  checkpointSeq: 1, idempotencyKey: 'stable-key', decision: 'approve',
});
assert.equal('decision' in approvalTicket(approval, 'consume-key'), false);
assert.throws(() => approvalTicket({ ...approval, inputDigest: 'bad' }, 'key'));
assert.throws(() => approvalTicket({ ...approval, checkpointSeq: 0 }, 'key'));
assert.throws(() => approvalTicket(null, 'key'));
assert.equal(continuationCursor('user-1', 'user-1-2', 'user-1-5'), 'user-1-5');
assert.equal(continuationCursor('user-1', 'user-1-5', 'user-1-2'), 'user-1-5');
assert.equal(continuationCursor('user-1', 'other-5', 'user-1-2'), 'user-1-2');
assert.equal(continuationCursor('user-1', '', 'user-1-bad'), '');
assert.equal(latestRequestId([{ id: 'assistant-id' }]), null);
assert.equal(latestRequestId([{ requestId: 'a' }, { requestId: 'b' }]), 'b');
assert.equal(inspectionMatches({ requestId: 'a', inspectionSeq: 2 }, 'a', 1), false);
assert.equal(inspectionMatches({ requestId: 'b', inspectionSeq: 2 }, 'a', 2), false);
assert.equal(inspectionMatches({ requestId: 'a', inspectionSeq: 2 }, 'a', 2), true);
assert.equal(createApprovalIdempotencyKey({ randomUUID() { return 'native-key'; } }), 'native-key');
const fallbackSource = { getRandomValues(bytes) { bytes.fill(0xff); return bytes; } };
assert.equal(createApprovalIdempotencyKey(fallbackSource), 'ffffffff-ffff-4fff-bfff-ffffffffffff');
assert.throws(() => createApprovalIdempotencyKey({}), /审批尚未提交/);
assert.throws(() => createApprovalIdempotencyKey(null), /审批尚未提交/);
assert.throws(() => createApprovalIdempotencyKey({ getRandomValues() { throw new Error('entropy failure'); } }), /entropy failure/);
const keys = new Set(Array.from({ length: 64 }, () =>
  createApprovalIdempotencyKey({ getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) })));
assert.equal(keys.size, 64);
assert.ok([...keys].every((key) => /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(key)));
console.log('P2_APPROVAL_STATE_PASSED checks=21');
