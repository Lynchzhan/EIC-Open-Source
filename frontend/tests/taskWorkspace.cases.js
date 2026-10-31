import assert from 'node:assert/strict';
import { taskStatus, taskRows, filterTasks, taskCounts, taskDate, taskReport, taskError,
  taskDetail, taskPermissions } from '../src/shared/ui/taskWorkspace.js';
import { countOf, checkedMutation, clearHistoryResult, workbenchPermissions, resolveWorkspaceIdentity } from '../src/shared/ui/workbenchContract.js';
let checks = 0;
const eq = (a, b) => { assert.deepEqual(a, b); checks++; };
const fails = (fn) => { assert.throws(fn); checks++; };
for (const key of [null, undefined, 'unknown', '__proto__', 'constructor']) eq(taskStatus(key).label, '状态未提供');
eq(taskStatus('running').bucket, 'active');
eq(taskStatus('queue_unavailable').bucket, 'attention');
eq(taskStatus('success').bucket, 'done');
eq(taskStatus('ready').bucket, 'waiting');
eq(taskStatus('cancelled').bucket, 'inactive');
const rows = taskRows({list:[{id:1, name:'知识库任务', status:'running', enabled:true, canExecute:true, last:0},
  {id:2, name:'另一个', status:'failed'}, 'name:旧任务 | Cron:0 2 * * * | TimeZone:Asia/Shanghai']});
eq(rows[0].id, '1'); eq(rows[0].last, 0); eq(rows[2].id, ''); eq(rows[2].canExecute, false);
eq(filterTasks(rows, '知识库').length, 1);
eq(filterTasks(rows, '', 'attention').length, 2);
eq(filterTasks(rows, '  另一个  ', 'active').length, 0);
eq(taskCounts(rows), {active:1, attention:2, waiting:0, done:0, inactive:0});
eq(taskRows([]), []);
for (const value of [null, {}, {data:[]}, [null], [false], [[]]]) fails(() => taskRows(value));
eq(taskDate(null), '未提供'); eq(taskDate('bad'), '时间格式未识别');
eq(taskDate(0).includes('1970'), true);
eq(taskReport({result:{handlerType:'unknown', report:{secret:'do-not-render', readOnly:true}}}), []);
eq(taskReport({result:{handlerType:'knowledge-check', report:{documentCount:0}}}), []);
const report = taskReport({result:{handlerType:'knowledge-check',
  report:{readOnly:true, documentCount:0, secret:'do-not-render'}}});
eq(report[2][1], '0'); eq(JSON.stringify(report).includes('do-not-render'), false);
eq(taskError('api_key=secret TOKEN: abc Authorization: Bearer abc.def password="hidden"').includes('secret'), false);
eq(taskError('Bearer abc.def').includes('abc.def'), false);
eq(taskError('x'.repeat(2000)).length, 1000);
eq(taskDetail({taskId:1, runs:[]}, '1').runs.length, 0);
fails(() => taskDetail({taskId:2, runs:[]}, '1'));
fails(() => taskDetail({taskId:1, runs:[null]}, '1'));
for (const value of [null, undefined, false, '', '0', -1, 0.2, NaN]) eq(countOf(value), null);
eq(countOf(0), 0);
eq(clearHistoryResult({code:200, data:{ok:true, deleted:0, sessions:0}}).deleted, 0);
for (const data of [false, true, {}, {ok:false, deleted:0, sessions:0}, {ok:true, deleted:-1, sessions:0}]) {
  fails(() => clearHistoryResult({code:200, data}));
}
fails(() => checkedMutation({code:200, data:false}));
fails(() => checkedMutation({code:200, isSuccess:false, data:true}));
checkedMutation({code:200, data:true}); checks++;
let storage = {};
globalThis.localStorage = {getItem:(key) => storage[key] ?? null};
eq(taskPermissions(), {read:false, write:false, execute:false});
storage = {UserPermissions:JSON.stringify(['task.read'])};
eq(taskPermissions(), {read:true, write:false, execute:false});
storage = {UserPermissions:JSON.stringify(['task.write'])};
eq(taskPermissions().execute, false);
storage = {UserPermissions:JSON.stringify(['task.read','task.write','knowledge.read'])};
eq(taskPermissions().execute, true);
storage = {user:JSON.stringify({isSuperAdmin:'false'})};
eq(taskPermissions().read, false); eq(workbenchPermissions().manageUsers, false);
storage = {UserPermissions:JSON.stringify(['Menu/MyMenu/MyAiChat'])};
eq(workbenchPermissions().chat, false);
storage = {UserPermissions:JSON.stringify(['ai.chat.read'])};
eq(workbenchPermissions().chat, false);
eq(workbenchPermissions().readHistory, true);
eq(workbenchPermissions().readAgents, false);
storage = {UserPermissions:JSON.stringify(['ai.chat.write'])};
eq(workbenchPermissions().chat, true);
eq(workbenchPermissions().readHistory, false);
eq(workbenchPermissions().readAgents, false);
storage = {UserPermissions:'INVALID', user:JSON.stringify({isSuperAdmin:true})};
eq(taskPermissions().read, false);
storage = {UserPermissions:'[]', user:JSON.stringify({isSuperAdmin:true})};
eq(taskPermissions().execute, true); eq(workbenchPermissions().readUsers, true);
eq((await resolveWorkspaceIdentity(
  async () => ({code:200,data:['1000/task.read','task.write']}),
  async () => ({code:200,data:{id:1,tenantId:1000}}))).permissions, ['task.read','task.write']);
await assert.rejects(() => resolveWorkspaceIdentity(async () => ({code:200,data:[]}), async () => ({code:200,data:null}))); checks++;
console.log(`taskWorkspace: ${checks} checks passed`);
