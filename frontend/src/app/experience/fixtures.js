// 全部数据均为代码内虚构样例；禁止从正式用户缓存、配置或日志构造预览。
const created = '2026-01-01 09:00:00';
const department = [{ id:'demo-department', name:'示例研发部', code:'DEMO-RD', status:1, description:'虚构组织', parentId:0, leader:'示例负责人', sort:1, remarks:'虚构组织', children:[] }];
const position = [{ id:'demo-position', name:'示例工程师', code:'DEMO-ENGINEER', status:1, description:'虚构岗位', parentId:0, departmentId:'demo-department', sort:1, remarks:'虚构岗位', children:[] }];
const users = [{ id:'demo-user', name:'示例成员', userName:'demo-member', phone:'', email:'', enabled:true,
  status:true, nickName:'示例成员', positions:[{ id:'demo-position', name:'示例工程师' }],
  dtoUserInfo:{ departmentId:'demo-department', departmentName:'示例研发部' },
  departmentId:'demo-department', departmentName:'示例研发部', positionId:'demo-position', positionName:'示例工程师', roles:[{ id:'demo-role', name:'示例浏览者' }], createTime:created }];
const roles = [{ id:'demo-role', name:'示例浏览者', remarks:'虚构角色，仅用于展示', createTime:created }];
const permissions = [{ id:'demo-permission', name:'示例列表浏览', fullName:'示例列表浏览', area:'Demo', areaName:'示例区域',
  module:'Preview', moduleName:'预览模块', action:'Read', actionName:'查看', httpMethod:'GET', permissionType:0, isManual:true, remarks:'虚构权限' }];
const dictionaries = [{ id:'demo-dictionary', type:'展示偏好', key:'theme', value:'light', isSystem:false, sort:1, remarks:'虚构设置，不包含凭据', createTime:created }];
const messages = [{ id:'demo-message', title:'欢迎进入示例工作空间', messageContent:'本页全部内容为虚构数据，可浏览但不能提交修改。',
  content:'虚构消息内容', createUser:'示例管理员', senderName:'示例管理员', createTime:created, isRead:true, messageType:0 }];
const tenants = [{ id:'demo-tenant', code:'DEMO', name:'示例企业', active:true, status:1, createTime:created }];
const skills = [1, 2, 3].map(type => ({ id:`demo-skill-${type}`, name:['示例摘要工具','示例摘要技能','示例 MCP 元数据'][type - 1],
  skillToolType:type, activeStatus:1, enabled:true, description:'虚构配置，仅展示，不执行', classMethod:'demo.preview', isSystem:false }));
const logs = [{ id:'demo-log', userName:'示例成员', operateType:'浏览', operateRemark:'虚构预览记录', ip:'192.0.2.10', device:'示例浏览器',
  httpUrl:'/demo/preview', httpBody:'{}', httpMethod:'GET', httpAction:'Preview', createTime:created,
  table:'Demo', tableId:'demo-row', sign:'展示', content:'虚构变更：示例名称', actionUserName:'示例成员', ipAddress:'192.0.2.10', deviceMark:'示例设备' }];
const lists = {
  '/api/Department/GetPageData':['departments', department], '/api/Position/GetPageData':['positions', position],
  '/api/User/GetSysUserList':['users', users], '/api/Role/GetRolePage':['roles', roles],
  '/api/Permission/GetPageData':['permissions', permissions], '/api/Dictionary/GetPageData':['settings', dictionaries],
  '/api/AISkillToolManagement/GetPageData':['skills', skills],
  '/api/Tenant/GetPageData':['tenants', tenants], '/api/Log/GetHttpLogPageData':['logs', logs], '/api/Log/GetOSLogPageData':['audit', logs],
  ...Object.fromEntries(['GetSystemPageData','GetPrivateUserSystemPageData','GetPrivateUserPageData','GetAnnouncementPageData','GetAIUserPageData']
    .map(action => [`/api/Message/${action}`, ['messages', messages]])),
};
const gets = {
  '/api/Department/GetDepartmentTree':['departments', department], '/api/Department/GetALLList':['departments', department],
  '/api/Position/GetPositionTree':['positions', position], '/api/Position/GetALLList':['positions', position],
  '/api/Role/GetAllRoles':['roles', roles], '/api/Role/GetRoleById':['roles', roles[0]],
  '/api/User/GetUserRoleKey':['users', roles.map(r => ({ ...r, key:r.id, value:r.name, label:r.name }))],
  '/api/User/GetPendingApprovals':['users', []], '/api/User/RegistrationCodes':['users', []], '/api/User/GetAllUserCount':['users', 1],
  '/api/Permission/GetDetails':['permissions', permissions[0]],
  '/api/Permission/GetAllAreaPermissions':['permissions', { dtos:[{ permissionType:'功能权限', dtos:[{
    area:'Demo', areaName:'示例区域', modules:[{ module:'Preview', moduleName:'预览模块',
      actions:[{ id:'demo-permission', actionName:'示例列表浏览', isPermission:true }] }],
  }] }] }],
  '/api/AIApps/NewInitialization':['agents', { id:'demo-agent-new', name:'', enabled:true, tools:[], skills:[], mcps:[], aiPromptID:null, chatModelID:null }],
  '/api/AITasks/demo-task-0/Status':['tasks', { taskId:'demo-task-0', name:'每周知识摘要', runs:[], status:'schedule_held' }],
  '/api/Dictionary/GetTypeList':['settings', ['展示偏好']],
  '/api/Dictionary/GetTypeWhereList':['settings', dictionaries],
  '/api/CodeGenerator/GetAreaNames':['codegen', ['Demo']],
  '/api/CodeGenerator/GetAreaNameEntityItems':['codegen', [{ entityName:'DemoItem', fields:['id','name'], description:'虚构示例实体' }]],
  '/api/Message/GetMyNoReadCount':['messages', 0],
  '/api/Base/GetGuId':['profile', 'demo-form-id'], '/api/Base/GetSnowflakeId':['profile', 'demo-form-id'],
  '/api/AIPrompts/GetALLList':['prompts', [{ id:'demo-prompt-0', name:'知识问答模板', prompt:'根据给定虚构资料回答。' }]],
  '/api/AIPrompts/BuiltinCatalog':['prompts', { canImport:true, entries:[] }],
  '/api/AISkillToolManagement/GetAllTools':['skills', [{ id:'demo-tool', name:'资料摘要', description:'虚构工具，不执行' }]],
  '/api/AISkillToolManagement/GetAllSkills':['skills', [{ id:'demo-skill-0', name:'资料摘要', enabled:true, executionMode:'deterministic' }]],
  '/api/AISkillToolManagement/GetAllMcps':['skills', []],
  '/api/AISkillToolManagement/GetExecutionCatalog':['skills', { canWrite:true, canInstall:false, skills:[{ id:'demo-skill-0', name:'资料摘要', enabled:true, executable:false, executionMode:'deterministic' }], tools:[], builtins:[] }],
  '/api/AISkillToolManagement/GetMcpExecutionCapabilities':['skills', { canWrite:true, canMcpWrite:true, canMcpCall:false, isSuperAdmin:false }],
};
export function fixtureRequest(path, method, params = {}) {
  const readPosts = { '/api/AIPrompts/PreviewBuiltinImport':['prompts', {revision:'demo', entries:[]}] };
  const source = path === '/api/AISkillToolManagement/GetById' && method === 'get'
    ? ['skills', skills.find(item => item.id === String(params.Id)) || skills[1]]
    : method === 'post' ? lists[path] || readPosts[path] : method === 'get' ? gets[path] || lists[path] : null;
  return source ? { page:source[0], data:structuredClone(source[1]), paginated:!!lists[path] } : null;
}

export function adminPreview() {
  const settings = { enabled:true, minutes:30, pages:['chat'], realPages:['chat','models','agents','knowledge'],
    globalCalls:100, globalTokens:10000000, maxPublicSessions:50 };
  return {
    '/api/ExperienceAdmin/Settings':{ settings, usage:{ calls:0, tokens:0 },
      pageNames:{ chat:'AI 对话', knowledge:'知识库', agents:'智能体', models:'模型' } },
    '/api/ExperienceAdmin/Resources':{ models:[{ id:'demo-model', name:'示例对话模型', kind:'chat' }], agents:[], knowledge:[] },
    '/api/ExperienceAdmin/Grants':[{ id:'demo-grant', kind:'link', pages:['chat'], minutes:30, calls:10, tokens:3000000,
      usedCalls:0, usedTokens:0, revision:1, status:'unclaimed', created:1767225600, claimEnd:4102444800, hardEnd:4102531200,
      canReveal:false, allow_chat_attachments:false, allow_web_search:false, modelIds:['demo-model'], agentIds:[], knowledgeIds:[], history:[] }],
  };
}
