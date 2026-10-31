export const budgetMessages = {
  conversation_budget_exceeded: '会话预算校验未通过，请展开预算详情查看具体原因。',
  conversation_token_limit: '会话剩余 Token 不足以预留本次调用，请查看预算详情或联系管理员调整会话预算。',
  conversation_call_limit: '会话累计调用次数已达上限，请查看预算详情或联系管理员调整策略。',
  conversation_time_limit: '会话累计执行时长已耗尽，请查看预算详情。',
  conversation_usage_incomplete: '历史 Token 用量尚未核实，系统未按零计算；请联系管理员核对记录，或新建会话继续。',
  conversation_tiers_exhausted: '模型降级档位已耗尽，请检查各档位用量和失败记录。',
  conversation_request_pending: '会话已有执行中或待核对的请求，请先刷新状态并等待原请求结束。',
};
