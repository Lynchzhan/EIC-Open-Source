import assert from 'node:assert/strict';
import { tokenValue, seconds, traceLayout, checkedSession, checkedSessions, showTurnUsage, needsStatisticsSettlement } from '../src/shared/ui/conversationObservation.js';
assert.equal(tokenValue({ value:0, complete:true }), '0');
assert.equal(tokenValue({ value:null }), '未采集');
assert.equal(tokenValue({ value:12, complete:false }), '12（部分）');
assert.equal(seconds(0), '0 秒');
assert.equal(seconds(null), '未采集');
const messages = [{ id:1, requestId:'a', isSend:true }, { id:2, requestId:'a', isSend:false },
  { id:3, requestId:'b', isSend:true }, { id:4, requestId:'a', isSend:false }];
assert.equal(showTurnUsage(messages, messages[0]), false);
assert.equal(showTurnUsage(messages, messages[1]), false);
assert.equal(showTurnUsage(messages, messages[2]), true);
assert.equal(showTurnUsage(messages, messages[3]), true);
const now = Date.parse('2026-10-09T00:00:10Z');
const recent = [{ isSend:true, requestId:'a', status:'completed', createdAt:'2026-10-09T00:00:00Z' }];
assert.equal(needsStatisticsSettlement(recent, {}, now), true);
assert.equal(needsStatisticsSettlement(recent, { a:{ timingComplete:true } }, now), false);
assert.equal(needsStatisticsSettlement(recent, {}, now + 60000), false);
assert.equal(needsStatisticsSettlement([{ ...recent[0], status:'streaming' }], {}, now), false);
const rows = traceLayout([
  { eventKey:'root', spanId:'root', occurredAt:'2026-10-09T00:00:00Z', durationMs:2000 },
  { eventKey:'call', spanId:'call', parentSpanId:'root', occurredAt:'2026-10-09T00:00:01Z', durationMs:1000 },
  { eventKey:'unknown', spanId:'unknown', parentSpanId:'missing', occurredAt:'2026-10-09T00:00:01Z' },
]);
assert.equal(rows[1].depth, 1);
assert.equal(rows[1].left, 50);
assert.equal(rows[2].relation, 'parent_not_visible');
assert.equal(traceLayout([{ spanId:'a', parentSpanId:'b' }, { spanId:'b', parentSpanId:'a' }])[0].relation, 'cycle');
assert.throws(() => checkedSession({ code:200, data:{ scope:'tenant', chatId:'1' } }, { scope:'own', chatId:'1' }));
assert.throws(() => checkedSessions({ code:200, data:{ scope:'own', data:[] } }, { scope:'own', page:1, pageSize:20 }));
console.log('conversationObservation cases PASS: real zero, missing, partial, timing, parent, orphan, cycle, response scope');
