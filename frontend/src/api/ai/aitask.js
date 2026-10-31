import http from '../../utils/http';
//查询我的任务 返回数据list $"name:{r.Id} | Cron:{r.Cron} | Next:{next} | Last:{last} | TimeZone:{r.TimeZoneId}";
export const getTaskList = () => {
  return http.post('/api/AITasks/GetTaskList', {});
};
//删除任务 返回 true or false
export const removeCronTask = (name) => {
  return http.delete('/api/AITasks/RemoveCronTask?name='+encodeURIComponent(name));
};
//执行任务 返回 true or false
export const triggerCronTask = (name) => {
  return http.post('/api/AITasks/Trigger?name='+encodeURIComponent(name));
};
export const addEditTask = (data, idempotencyKey) => {
  const config = idempotencyKey ? { headers: { 'Idempotency-Key': idempotencyKey } } : {};
  return http.post('/api/AITasks/AddEdit', data, config);
};
export const getTaskCapabilities = () => http.get('/api/AITasks/Capabilities');
export const cancelTask = (id) => http.post(`/api/AITasks/${encodeURIComponent(id)}/Cancel`);
export const getTaskStatus = (id) => http.get(`/api/AITasks/${encodeURIComponent(id)}/Status`);
