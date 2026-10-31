import assert from 'node:assert/strict';
import { agentIds, agentConfiguration, agentDimensions, filterAgents, hasAgentPermission, agentSavePayload, agentSelectedBindings } from '../src/shared/ui/agentWorkspace.js';
const rows = [
  { id: 1, name: '财务助手', type: 'chat', status: 'ready', createUser: '不是负责人', updateTime: '2026-10-06' },
  { id: 2, name: '文档助手', type: 'rag', status: 'draft', enabled: false },
  { id: 3, name: '研究员', type: 'chat' },
];
assert.equal(hasAgentPermission(['Menu/*'], 'ai.resource.write'), false);
assert.equal(hasAgentPermission(['*'], 'ai.resource.write'), true);
assert.equal(hasAgentPermission(['ai.resource.read'], 'ai.resource.write'), false);
assert.deepEqual(agentIds([1, '1', 0, null, undefined]), ['1']);
assert.equal(filterAgents(rows, '财务')[0].id, 1);
assert.deepEqual(filterAgents(rows, '', 'ready').map((item) => item.id), [1]);
assert.deepEqual(filterAgents(rows, '', 'all', 'rag').map((item) => item.id), [2]);
assert.equal(agentConfiguration(rows[1]).key, 'disabled');
assert.equal(agentConfiguration(rows[2]).key, 'unknown');
const dims = agentDimensions(rows[0]);
assert.equal(dims.length, 6);
assert.equal(dims[0].items.find(([name]) => name === '负责人')[1], '未提供');
assert.equal(dims[5].items.find(([name]) => name === '记忆')[1], '未提供');
assert.equal(agentDimensions({ isMemory: false })[5].items[0][1], '未启用');
assert.deepEqual(agentSavePayload({ id: '1', enabled: false, policy: { limit: 4 },
  apiKey: 'secret', secretConfigured: true, createdAt: 'old' }, { name: '更新' }),
  { id: '1', enabled: false, policy: { limit: 4 }, name: '更新' });
assert.deepEqual(agentSavePayload({ tools: [1] }, { tools: [] }), { tools: [] });
assert.equal(agentDimensions({ skills: [{ aiSkillToolManagementId: '7', isSelect: true },
  { aiSkillToolManagementId: '8', isSelect: false }] })[3].items[3][1], '7');
const options = [{ id: 7, name: '新名称', description: '新描述' }, { id: '8', name: '另一个工具' }];
const existing = [{ aiSkillToolManagementId: '7', isSelect: true,
  bindingOptions: { marker: 'keep', budget: 4 } }, { id: '99', isSelect: false, extension: 'invisible' }, 100];
const unchanged = structuredClone(existing);
const bindings = agentSelectedBindings(['7'], options, existing);
assert.deepEqual(bindings[0].bindingOptions, existing[0].bindingOptions);
assert.equal(bindings[0].aiSkillToolManagementName, '新名称');
assert.equal(bindings[0].isSelect, true);
assert.equal(bindings[1].isSelect, false);
assert.deepEqual(bindings.slice(2), existing.slice(1));
assert.deepEqual(existing, unchanged);
const deselected = agentSelectedBindings([], options, existing);
assert.equal(deselected[0].isSelect, false);
assert.deepEqual(deselected[0].bindingOptions, existing[0].bindingOptions);
assert.equal(agentSelectedBindings([7], options, [7])[0].isSelect, true);
assert.deepEqual(agentSelectedBindings([], [], [null, undefined, 0, '', 'lost']), ['lost']);
assert.deepEqual(agentSelectedBindings([], [], undefined), []);
console.log('AGENT_WORKSPACE_CASES_OK checks=27');
