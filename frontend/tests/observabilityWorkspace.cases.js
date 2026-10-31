import assert from 'node:assert/strict';
import { metric, percent, coverage, distribution, observationPermissions, observationQuery,
  checkedObservation, localTimeInput, statusLabel, eventLabel } from '../src/shared/ui/observabilityWorkspace.js';
let checks = 0;
function check(actual, expected) { assert.deepEqual(actual, expected); checks += 1; }
for (const value of [null, undefined, NaN, Infinity, -1, '12', false]) check(metric(value), '未提供');
check(metric(0), '0'); check(percent(0), '0%'); check(percent(.5), '50%'); check(percent(1.1), '未提供');
check(coverage({ providedCalls:2, population:3 }), '2 / 3 次调用');
check(distribution({ z:0, a:2, b:null, c:-1 }), [{ label:'a', value:2 }, { label:'z', value:0 }]);
check(observationPermissions({ user:{ isSuperAdmin:false }, permissions:['Menu/MyMenu/MyAiChat'] }),
  { read:false, admin:false });
check(observationPermissions({ user:{ isSuperAdmin:false }, permissions:['ai.chat.read'] }),
  { read:true, admin:false });
check(observationPermissions({ user:{ isSuperAdmin:true }, permissions:[] }), { read:true, admin:true });
check(observationPermissions({ permissions:['*'] }), { read:true, admin:false });
for (const permissions of [undefined, null, '*', 'ai.chat.read', {}, { includes:() => true }]) {
  check(observationPermissions({ permissions }), { read:false, admin:false });
}
check(statusLabel('success'), '成功'); check(eventLabel('tool_call'), '工具调用');
for (const value of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf', 'future_value']) {
  check(statusLabel(value), value); check(eventLabel(value), value);
}
for (const value of [null, undefined, '', {}, []]) {
  check(statusLabel(value), '未提供'); check(eventLabel(value), '未提供');
}
const draft = { start:'2026-10-05T00:00:00+08:00', end:'2026-10-06T00:00:00+08:00', scope:'own', modelId:'  model-1  ' };
const params = observationQuery(draft, false);
for (const category of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf']) {
  assert.throws(() => observationQuery({ ...draft, category }, false), /观测事件类型无效/); checks += 1;
}
check(observationQuery({ ...draft, category:'request' }, false).category, 'request');
check(params, { start:'2026-10-04T16:00:00.000Z', end:'2026-10-05T16:00:00.000Z', scope:'own', modelId:'model-1' });
for (const change of [{ start:'' }, { start:'wrong' }, { end:draft.start }, { end:'2026-12-06T00:00:00+08:00' },
  { scope:'tenant' }, { scope:'other' }, { agentId:'x'.repeat(81) }, { category:'invalid' }]) {
  assert.throws(() => observationQuery({ ...draft, ...change }, false)); checks += 1;
}
check(observationQuery({ ...draft, scope:'tenant' }, true).scope, 'tenant');
check(localTimeInput(new Date('2026-10-06T00:00:00Z')).length, 19);
const payload = { scope:'own', window:{ timezone:'UTC' }, page:1, pageSize:20, hasMore:false, data:[] };
check(checkedObservation({ code:200, data:payload }, 'events', { page:1, pageSize:20, scope:'own' }), payload);
for (const change of [{ scope:'tenant' }, { page:2 }, { pageSize:200 }, { hasMore:'false' }, { data:null }]) {
  assert.throws(() => checkedObservation({ code:200, data:{ ...payload, ...change } }, 'events',
    { page:1, pageSize:20, scope:'own' })); checks += 1;
}
assert.throws(() => checkedObservation({ code:200, data:{ ...payload, traceId:'foreign' } }, 'trace',
  { page:1, pageSize:20, scope:'own', traceId:'own' })); checks += 1;
assert.throws(() => checkedObservation({ code:200, isSuccess:false, data:payload }, 'events',
  { page:1, pageSize:20, scope:'own' })); checks += 1;
console.log(`OBSERVABILITY_WORKSPACE_OK checks=${checks}`);
