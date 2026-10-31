export function checkedData(response) {
  if (response?.code !== 200 || response?.isSuccess === false) {
    throw new Error(response?.message || '响应无效');
  }
  return response.data;
}

export const countOf = (value) => Number.isInteger(value) && value >= 0 ? value : null;

export function clearHistoryResult(response) {
  const data = checkedData(response);
  if (data?.ok !== true || countOf(data.deleted) == null || countOf(data.sessions) == null) {
    throw new Error('清除未完成，请刷新确认');
  }
  return data;
}

export function checkedMutation(response) {
  if (checkedData(response) !== true) throw new Error('操作未完成，请刷新确认');
}

export async function resolveWorkspaceIdentity(fetchPermissions, fetchUser) {
  const [permissionResponse, userResponse] = await Promise.all([fetchPermissions(), fetchUser()]);
  const list = checkedData(permissionResponse);
  const user = checkedData(userResponse);
  if (!Array.isArray(list) || !user || typeof user !== 'object' || user.id == null) {
    throw new Error('账户权限核验失败，操作入口保持关闭');
  }
  const prefix = user.tenantId == null ? '' : `${user.tenantId}/`;
  return { user, permissions: list.map(String).map((code) =>
    prefix && code.startsWith(prefix) ? code.slice(prefix.length) : code) };
}

export function workbenchPermissions() {
  try {
    const list = JSON.parse(localStorage.getItem('UserPermissions') || '[]');
    const user = JSON.parse(localStorage.getItem('user') || 'null');
    const has = (code) => user?.isSuperAdmin === true ||
      (Array.isArray(list) && (list.includes('*') || list.includes(code)));
    return { manageUsers: has('system.user.write'), readUsers: has('system.user.read'),
      chat: has('ai.chat.write'), readHistory: has('ai.chat.read'), readAgents: has('ai.resource.read') };
  } catch {
    return { manageUsers: false, readUsers: false, chat: false, readHistory: false, readAgents: false };
  }
}
