export const agentIds = (value) => {
  const values = value == null || value === '' ? [] : Array.isArray(value) ? value : [value];
  return [...new Set(values.map(String).filter((id) => !['', '0', 'null', 'undefined'].includes(id)))];
};
export const hasAgentPermission = (permissions, code) =>
  Array.isArray(permissions) && (permissions.includes('*') || permissions.includes(code));

// AddEdit 替换整个配置；保留表单未编辑的公开字段，排除只读元数据和密钥。
export function agentSavePayload(existing, changes) {
  const excluded = new Set(['createdAt', 'createTime', 'updateTime', 'updatedAt', 'tenantId', 'ownerId',
    'secretConfigured', 'secret_encrypted', 'headers_encrypted', 'modelKey', 'ModelKey', 'secretKey',
    'SecretKey', 'apiKey', 'lastMessage', 'appId']);
  return { ...Object.fromEntries(Object.entries(existing || {}).filter(([key]) => !excluded.has(key))), ...changes };
}

export function agentBindingId(item) {
  if (!item || typeof item !== 'object') return item;
  const ids = ['aIMcpId', 'aiSkillToolManagementId', 'id']
    .map((key) => item[key]).filter((id) => id != null);
  if (new Set(ids.map(String)).size > 1) throw new Error('绑定 ID 字段冲突，请核对原配置后重试');
  return ids[0];
}

// 只更新可见绑定的选择状态，保留旧配置和当前列表不可见的绑定。
export function agentSelectedBindings(selectedIds, options, existing = []) {
  const selected = new Set(agentIds(selectedIds));
  const bindingId = agentBindingId;
  const stored = Array.isArray(existing) ? existing : [];
  const previous = new Map(stored.map((item) => [String(bindingId(item)), item]));
  const known = new Set(options.map((item) => String(item.id)));
  return [
    ...options.map((item) => {
      const original = previous.get(String(item.id));
      return {
        ...(original && typeof original === 'object' ? original : {}),
        aiSkillToolManagementId: item.id,
        aiSkillToolManagementName: item.name,
        aiSkillToolManagementDescription: item.description,
        isSelect: selected.has(String(item.id)),
      };
    }),
    ...stored.filter((item) => agentIds(bindingId(item)).length && !known.has(String(bindingId(item)))),
  ];
}

// 配置状态与实际运行状态分开，更新时间不充当运行时间。
export function agentConfiguration(agent = {}) {
  if (agent.enabled === false || agent.status === 'disabled') return { key: 'disabled', label: '已停用' };
  if (agent.status === 'ready') return { key: 'ready', label: '配置就绪' };
  if (agent.status === 'draft') return { key: 'draft', label: '草稿' };
  return { key: 'unknown', label: '配置状态未提供' };
}
export function filterAgents(agents, query = '', status = 'all', type = 'all') {
  const needle = String(query).trim().toLocaleLowerCase();
  return agents.filter((agent) =>
    (!needle || [agent.name, agent.describe, agent.description, agent.ownerName].some(
      (value) => String(value ?? '').toLocaleLowerCase().includes(needle))) &&
    (status === 'all' || agentConfiguration(agent).key === status) &&
    (type === 'all' || String(agent.type ?? '') === type));
}
export function agentDimensions(agent = {}) {
  const text = (value) => value == null || value === '' ? '未提供' : String(value);
  const ids = (value) => agentIds(value).join('、') || '未提供';
  const flag = (value) => value === true ? '已启用' : value === false ? '未启用' : '未提供';
  const bindingIds = ['tools', 'skills', 'mcps'].flatMap((field) => Array.isArray(agent[field])
    ? agent[field].filter((item) => typeof item !== 'object' || item?.isSelect !== false)
      .map((item) => {
        try { return agentBindingId(item); } catch { return '绑定 ID 冲突'; }
      }) : []);
  return [
    { name: '身份', items: [['名称', text(agent.name)], ['类型', text(agent.type)],
      ['负责人', text(agent.ownerName)], ['描述', text(agent.describe || agent.description)]] },
    { name: '模型', items: [['会话模型 ID', text(agent.chatModelID || agent.chatModelId)],
      ['Embedding 模型 ID', text(agent.embeddingModelID)], ['温度', text(agent.temperature)], ['输出类型', text(agent.msgType)]] },
    { name: '提示词', items: [['提示词 ID', ids(agent.aiPromptIDs || agent.aiPromptID || agent.promptId)],
      ['上下文压缩', flag(agent.isAIMessageCompaction)], ['自动压缩', flag(agent.isAutoGetAIMessageCompaction)]] },
    { name: '工具与 Skill', items: [['工具', flag(agent.isAITools)], ['Skill', flag(agent.isSkill)],
      ['MCP', flag(agent.isMcp)], ['绑定 ID', ids(bindingIds.length ? bindingIds : agent.aiSkillsToolsBindIds)]] },
    { name: '知识库', items: [['知识库 ID', ids(agent.kmsIds || agent.kmsId || agent.knowledgeBaseId)],
      ['召回数量', text(agent.maxMatchesCount)], ['重排数量', text(agent.rerankCount)]] },
    { name: '记忆与运行策略', items: [['记忆', flag(agent.isMemory)], ['重试上限', text(agent.maxRetries)],
      ['网络超时', text(agent.networkTimeout)], ['会话消息上限', text(agent.chatMessageLimit)]] },
  ];
}
