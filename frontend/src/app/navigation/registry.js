import {
  GridOutline as AppstoreOutlined,
  ClipboardOutline as AuditOutlined,
  BusinessOutline as BankOutlined,
  BookOutline as BookOutlined,
  DocumentTextOutline as FileTextOutlined,
  TimeOutline as HistoryOutlined,
  KeyOutline as KeyOutlined,
  ChatbubblesOutline as MessageOutlined,
  MegaphoneOutline as NotificationOutlined,
  HardwareChipOutline as RobotOutlined,
  ShieldCheckmarkOutline as SafetyOutlined,
  CalendarOutline as ScheduleOutlined,
  SettingsOutline as SettingOutlined,
  PeopleOutline as TeamOutlined,
  ConstructOutline as ToolOutlined,
  PersonOutline as UserOutlined,
} from '@vicons/ionicons5'

export const routePathMap = {
  dashboard: '/home',
  'user-list': '/home/user/list',
  'user-role': '/home/user/role',
  'user-permission': '/home/user/permission',
  'system-dic': '/home/system/dic',
  'system-announcement': '/home/system/announcement',
  'log-management': '/home/system/log',
  oslog: '/home/system/oslog',
  'system-code-generator': '/home/system/code-generator',
  'ai-appsmg': '/home/aimanagement/aiappsmg',
  'ai-promptsmg': '/home/aimanagement/aipromptsmg',
  'ai-kmssmg': '/home/aimanagement/aikmssmg',
  'ai-modelmg': '/home/aimanagement/aimodelmg',
  'ai-skilltoolmg': '/home/aimanagement/aiskilltoolmg',
  'ai-recalltest': '/home/aimanagement/airecalltest',
  'my-message': '/home/my/message',
  'my-ai-chat': '/home/my/ai-chat',
  'my-ai-tasks': '/home/my/ai-tasks',
  observability: '/home/my/observability',
  'my-ai-agents': '/home/my/ai-agents',
  'organizational-position': '/home/position/management',
  'organizational-department': '/home/department/management',
  'system-tenant': '/home/system/tenant',
  'system-experience': '/home/system/experience',
  handleUserInfo: '/home/user/profile',
}

export const pageTitleMap = {
  dashboard: '工作台',
  'user-list': '用户列表',
  'user-role': '角色管理',
  'user-permission': '权限管理',
  'system-dic': '系统配置',
  'system-announcement': '系统公告',
  'log-management': '日志管理',
  oslog: '关键数据变动日志',
  'system-code-generator': '代码生成器',
  'ai-appsmg': '智能体管理',
  'ai-promptsmg': '提示词管理',
  'ai-kmssmg': '知识库管理',
  'ai-modelmg': '模型配置管理',
  'ai-skilltoolmg': 'AI技能工具管理',
  'ai-recalltest': '召回实验工作台',
  'my-message': '我的消息',
  'my-ai-chat': 'AI 对话',
  'my-ai-tasks': '自动任务',
  observability: '可观测性',
  'my-ai-agents': '我的智能体',
  'organizational-position': '岗位管理',
  'organizational-department': '部门管理',
  'system-tenant': '租户管理',
  'system-experience': '演示与限时体验',
  handleUserInfo: '个人中心',
}

const item = (key, title, permission, icon, capability = 'route') => ({
  key,
  title,
  permission,
  icon,
  capability,
  path: routePathMap[key],
})

export const navigationGroups = [
  {
    key: 'workspace',
    title: '工作台',
    items: [
      item('my-ai-chat', 'AI 对话', 'Menu/MyMenu/MyAiChat', MessageOutlined),
      item('my-ai-tasks', '自动任务', 'Menu/MyMenu/MyAiTasks', ScheduleOutlined),
      item('my-message', '我的消息', 'Menu/MyMenu/MyMessage', MessageOutlined),
    ],
  },
  {
    key: 'ai-builder',
    title: 'AI 构建',
    items: [
      item('my-ai-agents', '我的智能体', 'Menu/MyMenu/MyAiAgents', RobotOutlined),
      item('ai-appsmg', '智能体管理', 'Menu/AIManagement/AIAppsManagement', AppstoreOutlined),
      item('ai-promptsmg', '提示词管理', 'Menu/AIManagement/AIPromptsManagement', FileTextOutlined),
      item('ai-kmssmg', '知识库管理', 'Menu/AIManagement/AIKmssManagement', BookOutlined),
      item('ai-skilltoolmg', '技能与工具', 'Menu/AIManagement/AISkillToolManagement', ToolOutlined),
      item('ai-modelmg', '模型配置', 'Menu/AIManagement/AIModelManagement', SettingOutlined),
    ],
  },
  {
    key: 'organization',
    title: '组织架构',
    items: [
      item('organizational-department', '部门管理', 'Menu/OrganizationalManagement/DepartmentManagement', TeamOutlined),
      item('organizational-position', '岗位管理', 'Menu/OrganizationalManagement/PositionManagement', AuditOutlined),
    ],
  },
  {
    key: 'administration',
    title: '系统管理',
    items: [
      item('user-list', '用户管理', 'Menu/UserManagement/UserList', UserOutlined),
      item('user-role', '角色管理', 'Menu/UserManagement/UserRole', SafetyOutlined),
      item('user-permission', '权限管理', 'Menu/UserManagement/UserPermission', KeyOutlined),
      item('system-announcement', '系统公告', 'Menu/SystemSettings/SystemAnnouncement', NotificationOutlined),
      item('system-dic', '系统配置', 'Menu/SystemSettings/SystemDic', SettingOutlined),
      item('system-tenant', '租户管理', 'Menu/SystemSettings/SystemTenant', BankOutlined),
      item('system-experience', '演示与限时体验', '__super_admin_only__', SafetyOutlined),
      item('log-management', '日志管理', 'Menu/SystemSettings/LogManagement', FileTextOutlined),
      item('oslog', '变更审计', 'Menu/SystemSettings/OsLog', HistoryOutlined),
      item('system-code-generator', '代码生成器', 'Menu/SystemSettings/CodeGenerator', ToolOutlined),
    ],
  },
]

export const navigationItemByKey = Object.fromEntries(
  navigationGroups.flatMap((group) => group.items.map((entry) => [entry.key, entry])),
)

export const resolveNavigationKey = (path) => {
  const normalizedPath = String(path || '').replace(/\/+$/, '') || '/'
  const match = Object.entries(routePathMap)
    .sort(([, left], [, right]) => right.length - left.length)
    .find(([, routePath]) => normalizedPath === routePath || normalizedPath.startsWith(`${routePath}/`))
  return match?.[0] || 'dashboard'
}
