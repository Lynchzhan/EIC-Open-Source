import { createRouter, createWebHistory } from 'vue-router';
import LoginPage from '@/pages/LoginPage.vue';
import ConsoleLayout from '@/app/layouts/ConsoleLayout.vue';
const UserList = () => import('@/pages/UserList.vue');
const UserRole = () => import('@/pages/UserRole.vue');
const UserProfile = () => import('@/pages/UserProfile.vue');
const UnderDevelopment = () => import('@/pages/UnderDevelopment.vue');
const PermissionMg = () => import('@/pages/PermissionMg.vue');
const HttpLogMg = () => import('@/pages/HttpLog.vue');
const OSLogMG = () => import('@/pages/OSLog.vue');
// 消息管理相关页面
const SystemAnnouncement = () => import('@/pages/SystemAnnouncement.vue');
const MyMessages = () => import('@/pages/MyMessages.vue');
const DicConfig = () => import('@/pages/DicMg.vue');
// 代码生成器页面
const CodeGenerator = () => import('@/pages/CodeGenerator.vue');
// AI管理相关页面
const AiAppsMg = () => import('@/pages/ai/AgentManagement.vue');
const AiPromptsMg = () => import('@/pages/ai/PromptManagement.vue');
const AiKmssMg = () => import('@/pages/ai/KnowledgeBaseManagement.vue');
const AiModelMg = () => import('@/pages/ai/ModelManagement.vue');
const AiSkillToolMg = () => import('@/pages/ai/SkillToolManagement.vue');
const MyAIChat = () => import('@/pages/ai/MyAIChat.vue');
const MyAITasks = () => import('@/pages/ai/MyAITasks.vue');
const MyAgentList = () => import('@/pages/ai/MyAgentList.vue');
//组织架构相关页面
const PositionManagement = () => import('@/pages/organizational/PositionManagement.vue');
const DepartmentManagement = () => import('@/pages/organizational/DepartmentManagement.vue');
// 租户管理页面
const TenantManagement = () => import('@/pages/TenantManagement.vue');
const ExperiencePage = () => import('@/pages/ExperiencePage.vue');
const ExperienceAdmin = () => import('@/pages/ExperienceAdmin.vue');

const routes = [
  { path: '/experience', name: 'Experience', component: ExperiencePage },
  {
    path: '/',
    redirect: '/login'
  },
  {
    path: '/login',
    name: 'Login',
    component: LoginPage
  },
  {
    path: '/home',
    name: 'Home',
    component: ConsoleLayout,
    meta: { requiresAuth: true },
    children: [
      { path: 'system/experience', name: 'ExperienceAdmin', component: ExperienceAdmin },
      {
        path: '',
        name: 'Dashboard',
        redirect: '/home/my/ai-chat'
      },
      {
        path: 'user/list',
        name: 'UserList',
        component: UserList
      },
      {
        path: 'user/role',
        name: 'UserRole',
        component: UserRole
      },
      {
        path: 'user/profile',
        name: 'UserProfile',
        component: UserProfile
      },
      {
        path: 'user/permission',
        name: 'PermissionMg',
        component: PermissionMg
      },
      {
        path: 'system/dic',
        name: 'dicConfig',
        component: DicConfig
      },
      {
        path: 'system/log',
        name: 'HttpLogMg',
        component: HttpLogMg
      },
      {
        path: 'system/oslog',
        name: 'OSLogMG',
        component: OSLogMG
      },
      {
        path: 'system/announcement',
        name: 'SystemAnnouncement',
        component: SystemAnnouncement
      },
      {
        path: 'system/tenant',
        name: 'TenantManagement',
        component: TenantManagement
      },
      {
        path: 'system/code-generator',
        name: 'CodeGenerator',
        component: CodeGenerator
      },
      {
        path: 'my/message',
        name: 'MyMessages',
        component: MyMessages
      },
        {
        path: 'my/ai-chat',
        name: 'MyAIChat',
        component: MyAIChat
      },
      {
        path: 'my/ai-tasks',
        name: 'MyAITasks',
        component: MyAITasks
      },
      {
        path: 'my/observability',
        name: 'ObservabilityDashboard',
        redirect: '/home/my/ai-chat'
      },
      {
        path: 'my/ai-agents',
        name: 'MyAgentList',
        component: MyAgentList
      },
      {
        path: 'underdevelopment',
        name: 'UnderDevelopment',
        component: UnderDevelopment
      },
      {
        path: 'aimanagement',
        name: 'aimanagement',
        redirect: '/home/aimanagement/aiappsmg'
      },
        {
        path: 'aimanagement/aiappsmg',
        name: 'aiappsmg',
        component: AiAppsMg
      },
        {
        path: 'aimanagement/aipromptsmg',
        name: 'aipromptsmg',
        component: AiPromptsMg
      },
        {
        path: 'aimanagement/aikmssmg',
        name: 'aikmssmg',
        component: AiKmssMg // 替换为实际的知识库管理页面
      },
        {
        path: 'aimanagement/aimodelmg',
        name: 'aimodelmg',
        component: AiModelMg
      },
      {
        path: 'aimanagement/aiskilltoolmg',
        name: 'aiskilltoolmg',
        component: AiSkillToolMg
      },
      {
        path: 'aimanagement/airecalltest',
        name: 'airecalltest',
        redirect: '/home/aimanagement/aikmssmg'
      },
      {
        path: 'position/management',
        name: 'positionManagement',
        component: PositionManagement
      }  ,{
        path: 'department/management',
        name: 'departmentManagement',
        component: DepartmentManagement
      }  
    ]
  } 
];

const router = createRouter({
  history: createWebHistory(),
  routes
});

// 添加路由守卫
router.beforeEach((to, from, next) => {
  const requiresAuth = to.matched.some(record => record.meta.requiresAuth);
  const isAuthenticated = localStorage.getItem('token') || localStorage.getItem('eic-token');

  if (requiresAuth && !isAuthenticated) {
    next('/login');
  } else if (to.path === '/login' && isAuthenticated) {
    next('/home');
  } else {
    next();
  }
});

export default router;
