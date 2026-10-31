import http from '../utils/http';

export const GetGuId = () => {
  return http.get(`/api/Base/GetGuId`);
};

export const GetSnowflakeId = () => {
  return http.get(`/api/Base/GetSnowflakeId`, { timeout: 30000 });
};

//获取登录用户角色权限
export const getUserPermissions = () => {
  return http.get(`/api/Permission/GetUserPermissions`);
};
