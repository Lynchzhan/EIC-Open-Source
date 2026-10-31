import assert from 'node:assert/strict';
import { eventHistoryContent, restoreHistoryEvents } from '../src/shared/ui/chatHistoryEvents.js';
const event = (sequence, text, kind = 'aIToolsContentMsg') => ({ sequence, event: kind, payload: { text } });
assert.deepEqual(eventHistoryContent(null), { aiToolsContent: '', aiReasoningContent: '' });
assert.equal(eventHistoryContent([event(2, '完成'), event(1, '审批')]).aiToolsContent, '审批\n完成');
assert.equal(eventHistoryContent([event(1, '一次'), event(1, '重复')]).aiToolsContent, '一次');
assert.equal(eventHistoryContent([event(0, '无效'), event(-1, '无效'), event('2', '无效')]).aiToolsContent, '');
assert.equal(eventHistoryContent([event(1, {}), event(2, '<script>text</script>')]).aiToolsContent, '<script>text</script>');
assert.equal(eventHistoryContent([event(1, '甲', 'aIReasoningContentMsg'), event(2, '乙', 'aIReasoningContentMsg')]).aiReasoningContent, '甲乙');
assert.equal(eventHistoryContent([event(1, '回答', 'aimsg')]).aiToolsContent, '');
const rows = [{ isSend: true, requestId: 'a' }, { isSend: false, requestId: 'a' }, { isSend: false }];
const frozen = JSON.stringify(rows), called = [];
let result = await restoreHistoryEvents(rows, async (id) => {
  called.push(id); return { code: 200, data: [event(1, '真实事件')] };
});
assert.deepEqual(called, ['a']);
assert.equal(result[0].aiToolsContent, undefined);
assert.equal(result[1].aiToolsContent, '真实事件');
assert.equal(result[2].aiToolsContent, undefined);
assert.equal(JSON.stringify(rows), frozen);
result = await restoreHistoryEvents([rows[1], rows[1]], async () => { throw new Error('403'); });
assert.match(result[0].eventHistoryError, /未重新执行/);
result = await restoreHistoryEvents([rows[1]], async () => ({ code: 200, data: [] }));
assert.equal(result[0].aiToolsContent, '');
assert.equal(result[0].eventHistoryError, undefined);
result = await restoreHistoryEvents([{ ...rows[1], aiToolsContent: '已有详情' }], async () => ({ code: 200, data: [event(1, '摘要')] }));
assert.equal(result[0].aiToolsContent, '已有详情');
let active = 0, maximum = 0;
await restoreHistoryEvents(Array.from({ length: 13 }, (_, index) => ({ isSend: false, requestId: String(index) })),
  async () => {
    maximum = Math.max(maximum, ++active);
    await new Promise((resolve) => setTimeout(resolve, 1));
    --active; return { code: 200, data: [] };
  });
assert.equal(maximum, 4);
console.log('P3_HISTORY_EVENTS_PASSED checks=17');
