import http from '../../utils/http';
//获取AI技能工具列表
export const getAISkillToolManagementPageData = (params) => {
  return http.post('/api/AISkillToolManagement/GetPageData', params);
};
//获取AI技能工具详情
export const getAISkillToolManagementById = (id) => {
  return http.get('/api/AISkillToolManagement/GetById?Id=' + id);
};
//添加/编辑AI技能工具
export const addEditAISkillToolManagement = (data) => {
  return http.post('/api/AISkillToolManagement/AddEdit', data);
};
//删除AI技能工具
export const deleteAISkillToolManagement = (id) => {
  return http.delete('/api/AISkillToolManagement/Delete?Id='+id);
};
//获取AI工具列表
export const GetAllTools = () => {
  return http.get('/api/AISkillToolManagement/GetAllTools');
};
//获取AISkill技能列表
export const GetAllSkills = () => {
  return http.get('/api/AISkillToolManagement/GetAllSkills');
};
//获取Mcp工具列表
export const GetAllMcps = () => {
  return http.get('/api/AISkillToolManagement/GetAllMcps');
};
export const getExecutionCatalog = () => http.get('/api/AISkillToolManagement/GetExecutionCatalog');
export const getMcpExecutionCapabilities = () => http.get('/api/AISkillToolManagement/GetMcpExecutionCapabilities');
export const installBuiltinSkills = (keys) => http.post('/api/AISkillToolManagement/InstallBuiltinSkills', { keys });
export const executeSkill = (data) => http.post('/api/AISkillToolManagement/ExecuteSkill', data);
export const executeTool = (data) => http.post('/api/AISkillToolManagement/ExecuteTool', data);
export const setBuiltinSkillEnabled = (data) => http.post('/api/AISkillToolManagement/SetBuiltinSkillEnabled', data);
export const setSkillEnabled = (data) => http.post('/api/AISkillToolManagement/SetSkillEnabled', data);
export const addMcp = (data) => http.post('/api/AISkillToolManagement/AddMcp', data);
export const deleteMcp = (id) => http.delete('/api/AISkillToolManagement/DeleteMcp', { params: { Id: id } });
export const callMcp = (data) => http.post('/api/AISkillToolManagement/CallMcp', data);
