import { budgetMessages } from '../shared/ui/budgetMessages.js';

const messages = {
  ...budgetMessages,
  task_execution_disabled: "自动任务尚未启用，执行入口保持关闭",
  redis_unavailable: "任务队列暂不可用，执行入口保持关闭",
  worker_unavailable: "任务执行服务尚未就绪，执行入口保持关闭",
  outbox_disabled: "任务派发尚未启用，执行入口保持关闭",
  handler_not_registered: "该任务类型尚未开放，请选择已支持的任务",
  agent_handler_not_registered: "智能体自动执行尚未开放",
  agent_application_required: "请选择一个 Agent 或 App，不能同时绑定两者",
  invalid_app_id: "Agent 或 App 标识无效",
  invalid_agent_prompt: "请填写 1 至 16000 字符的任务内容",
  invalid_agent_max_steps: "最大执行步数必须为 1 至 32 的整数",
  task_owner_permission_revoked: "任务所有者的执行权限已撤销，请重新核对授权",
  conflicting_task_handler: "任务类型字段不一致，请重新选择任务类型",
  invalid_knowledge_base_id: "知识库 ID 无效，请检查后重试",
  knowledge_base_unavailable: "知识库已删除、已禁用或不可访问",
  unknown_task_input: "任务参数包含不支持的字段",
  invalid_time_zone: "时区无效，请填写 Asia/Shanghai 或 UTC 等有效时区",
  invalid_schedule_policy: "漏触发处理策略无效",
  cron_requires_five_fields: "Cron 表达式需要五个字段：分、时、日、月、星期",
  invalid_cron_field: "Cron 表达式格式无效，请检查各字段",
  invalid_cron_step: "Cron 步长无效，请检查范围和步长",
  cron_out_of_range: "Cron 字段超出有效范围",
  cron_has_no_occurrence_within_eight_years: "该计划在未来八年内没有有效执行时间",
  dst_policy_required: "计划涉及夏令时的重复或不存在时刻，需要明确处理策略",
  once_requires_run_at: "一次性任务需要填写执行时间",
  once_requires_future_time: "一次性任务的执行时间必须晚于当前时间",
  once_requires_offset_for_dst: "该时间存在夏令时歧义，请明确填写时区偏移",
  empty_task_name: "请填写任务名称",
  permission_revoked: "任务所需权限已被撤销",
  read_only_handler_unavailable: "只读检查暂不可用，请稍后查看重试结果",
  invalid_handler_result: "任务结果未通过校验，尚未确认成功",
  invalid_or_unavailable_task_input: "任务参数或绑定资源不可用",
  consumer_interrupted: "执行已中断，结果尚不确定，不会自动重复执行",
  consumer_lease_expired: "执行服务失联，结果尚不确定，不会自动重复执行",
  ERR_NETWORK: "网络连接失败，请检查网络后重试",
  ECONNABORTED: "请求超时，请稍后重试",
  ETIMEDOUT: "请求超时，请稍后重试",
  ERR_CANCELED: "请求已取消",
  "Network Error": "网络连接失败，请检查网络后重试",
  "Failed to fetch": "网络连接失败，请检查网络后重试",
  "task not found": "任务不存在或不可访问",
};

export function toUserMessage(value, fallback = "请求未完成，请稍后重试") {
  if (typeof value !== "string" || !value.trim()) return fallback;
  const text = value.trim();
  if (Object.hasOwn(messages, text)) return messages[text];
  // 保留已有中文业务说明；未知英文错误不直接作为操作提示。
  return /[\u4e00-\u9fff]/.test(text) ? text : fallback;
}
