<template>
  <div class="console-shell" :class="{ 'is-compact': sidebarCompact }">
    <button
      v-if="isMobile && mobileNavigationOpen"
      class="console-backdrop"
      type="button"
      aria-label="关闭导航"
      tabindex="-1"
      @click="mobileNavigationOpen = false"
    />

    <aside
      id="console-navigation"
      ref="navigationElement"
      class="console-sidebar"
      :class="{ 'is-mobile-open': isMobile && mobileNavigationOpen }"
      :role="isMobile ? 'dialog' : undefined"
      :aria-modal="isMobile && mobileNavigationOpen ? true : undefined"
      :inert="isMobile && !mobileNavigationOpen"
      aria-label="主导航"
    >
      <div class="console-brand">
        <img :src="logoImage" alt="EIC 企智枢" class="console-brand-logo" />
        <div class="console-brand-copy">
          <strong>企智枢</strong>
          <span>Enterprise Intelligence</span>
        </div>
        <button
          v-if="isMobile"
          class="console-icon-button console-mobile-close"
          type="button"
          aria-label="关闭导航"
          @click="mobileNavigationOpen = false"
        >
          <CloseOutlined />
        </button>
      </div>

      <div v-if="workspaceContext.tenantName" class="console-workspace-context" aria-label="当前工作空间">
        <strong>{{ workspaceContext.tenantName }}</strong>
        <span v-if="workspaceContext.organizationName">{{ workspaceContext.organizationName }}</span>
      </div>
      <nav class="console-nav" aria-label="工作导航">
        <section
          v-for="group in visibleGroups"
          :key="group.key"
          class="console-nav-group"
          :aria-label="group.title"
        >
          <button class="console-group-toggle" type="button"
            :aria-expanded="!preferences.collapsedGroups.includes(group.key)"
            :aria-controls="`nav-group-${group.key}`" @click="preferences.toggleGroup(group.key)">
            <span>{{ group.title }}</span>
            <ChevronDownOutline :class="{ 'is-closed': preferences.collapsedGroups.includes(group.key) }" />
          </button>
          <div :id="`nav-group-${group.key}`" v-show="!preferences.collapsedGroups.includes(group.key) || (sidebarCompact && !isMobile)">
          <button
            v-for="entry in group.items"
            :key="entry.key"
            type="button"
            class="console-nav-item"
            :class="{ 'is-active': activeRouteKey === entry.key }"
            :aria-label="entry.title"
            :aria-current="activeRouteKey === entry.key ? 'page' : undefined"
            :disabled="experience && !experienceCanNavigate(entry.key)"
            :title="experience && !experienceCanNavigate(entry.key) ? '此页面未开放体验' : entry.title"
            @click="navigateTo(entry)"
          >
            <component :is="entry.icon" class="console-nav-icon" aria-hidden="true" />
            <span class="console-nav-label">{{ entry.title }}</span>
          </button>
          </div>
        </section>
      </nav>

      <div class="console-sidebar-footer">
        <button v-if="!isMobile" class="console-collapse-button" type="button"
          :aria-label="sidebarCompact ? '展开主导航' : '收起主导航'"
          @click="preferences.toggleCompact()">
          <MenuOutlined /><span>{{ sidebarCompact ? '展开' : '收起导航' }}</span>
        </button>
        <div class="console-user-card">
          <img v-if="experience" :src="defaultAvatar" alt="体验访客" class="console-avatar" />
          <AuthenticatedImage v-else :source="userInfo.avatar" class="console-avatar" />
          <div class="console-user-copy">
            <strong>{{ userInfo.name }}</strong>
            <span>{{ userInfo.isSuperAdmin ? '系统管理员' : 'EIC 工作空间' }}</span>
          </div>
          <n-dropdown trigger="click" placement="top-start" :options="userMenuOptions"
            :show="userMenuOpen" @update:show="userMenuOpen = $event" @select="selectUserMenu">
            <button ref="userMenuTrigger" class="console-icon-button console-user-more" type="button"
              aria-label="用户菜单" aria-haspopup="menu" :aria-expanded="userMenuOpen"
              @keydown.down.prevent="userMenuOpen = true">
              <MoreOutlined />
            </button>
          </n-dropdown>
        </div>
      </div>
    </aside>

    <main class="console-main" :class="{ 'is-chat-page': activeRouteKey === 'my-ai-chat' }" :inert="isMobile && mobileNavigationOpen">
      <header class="console-topbar">
        <button
          v-if="isMobile"
          class="console-icon-button"
          type="button"
          aria-label="打开导航"
          aria-controls="console-navigation"
          :aria-expanded="mobileNavigationOpen"
          @click="mobileNavigationOpen = true"
        >
          <MenuOutlined />
        </button>
        <div class="console-mobile-title">
          <h1>{{ currentPageTitle }}</h1>
        </div>
        <n-badge :dot="noReadCount > 0">
          <button class="console-icon-button" type="button" aria-label="打开消息" @click="openMessages">
            <BellOutlined />
          </button>
        </n-badge>
        <button class="console-icon-button" type="button" :aria-label="appearance === 'dark' ? '切换浅色模式' : '切换深色模式'"
          @click="setAppearance(appearance === 'dark' ? 'light' : 'dark')"><BgColorsOutlined /></button>
      </header>
      <div class="console-workspace">
        <slot><router-view /></slot>
      </div>
    </main>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { appearance, setAppearance } from '@/app/appearance'
import {
  ColorPaletteOutline as BgColorsOutlined, NotificationsOutline as BellOutlined,
  CloseOutline as CloseOutlined, MenuOutline as MenuOutlined,
  EllipsisHorizontalOutline as MoreOutlined, ChevronDownOutline,
} from '@vicons/ionicons5'
import { NBadge, NDropdown } from 'naive-ui'
import { confirm } from '@/app/ui/feedback'
import { usePreferences } from '@/app/stores/preferences'
import { GetMyNoReadCount } from '@/api/message'
import { getUserPermissions } from '@/api/baseapi'
import { getTokenUser } from '@/api/userapi'
import http from '@/utils/http'
import AuthenticatedImage from '@/components/AuthenticatedImage.vue'
import defaultAvatar from '@/assets/eic-avatar.svg'
import logoImage from '@/assets/eic-logo.svg'
import {
  navigationGroups,
  navigationItemByKey,
  pageTitleMap,
  resolveNavigationKey,
  routePathMap,
} from '@/app/navigation/registry'
import { experiencePageByNav } from '@/app/experience/runtime'

const props = defineProps({ experience: { type: Object, default: null }, experienceNav: { type: String, default: 'my-ai-chat' } })
const emit = defineEmits(['experience-navigate', 'experience-leave'])
const experienceCanNavigate = key => (props.experience?.previewPages || props.experience?.pages || []).includes(experiencePageByNav[key])

const route = useRoute()
const router = useRouter()
const isMobile = ref(window.matchMedia('(max-width: 767.98px)').matches)
const mobileNavigationOpen = ref(false)
const preferences = usePreferences()
const sidebarCompact = computed(() => preferences.compact)
const navigationElement = ref(null)
const userMenuOpen = ref(false)
const userMenuTrigger = ref(null)
let navigationOpener = null
let previousBodyOverflow = ''
const userPermissions = ref([])
const userInfo = ref({ name: '用户', avatar: defaultAvatar, isSuperAdmin: false })
const noReadCount = ref(0)
const workspaceContext = ref({ tenantName:'', organizationName:'' })
let contextAlive = true
async function refreshWorkspaceContext() {
  if (props.experience) return
  try {
    const response = await http.get('/api/User/WorkspaceContext', { timeout:15000 })
    if (contextAlive) workspaceContext.value = response.data || { tenantName:'', organizationName:'' }
  } catch { if (contextAlive) workspaceContext.value = { tenantName:'', organizationName:'' } }
}
const pageMode = ref('single')
const isFullScreen = ref(Boolean(document.fullscreenElement))
const activeRouteKey = computed(() => props.experience ? props.experienceNav : resolveNavigationKey(route.path))
const currentPageTitle = computed(() => pageTitleMap[activeRouteKey.value] || '企智枢')
const visibleGroups = computed(() => navigationGroups
  .map((group) => ({
    ...group,
    items: group.items.filter((entry) => props.experience || hasPermission(entry.permission)),
  }))
  .filter((group) => group.items.length > 0))

const hasPermission = (permission) => {
  if (permission === '__super_admin_only__') return userInfo.value.isSuperAdmin
  if (!permission) return true
  const permissions = userPermissions.value
  if (permissions.includes('*') || permissions.includes('Menu/*')) return true
  if (userInfo.value.isSuperAdmin) return true
  return permissions.includes(permission)
}

const applyPermissions = (rawList) => {
  let user = null
  try {
    user = JSON.parse(localStorage.getItem('user') || 'null')
  } catch {
    user = null
  }
  const tenantPrefix = user?.tenantId != null ? `${user.tenantId}/` : ''
  const list = Array.isArray(rawList)
    ? rawList.map((entry) => {
      const value = String(entry)
      return tenantPrefix && value.startsWith(tenantPrefix) ? value.slice(tenantPrefix.length) : value
    })
    : []
  userPermissions.value = list
  localStorage.setItem('UserPermissions', JSON.stringify(list))
}

const loadCachedUser = () => {
  try {
    const cached = JSON.parse(localStorage.getItem('user') || 'null')
    if (!cached) return
    userInfo.value = {
      name: cached.name || cached.userName || '用户',
      avatar: cached.avatarUrl || cached.avatar || cached.headImgs?.[0]?.value || defaultAvatar,
      isSuperAdmin: Boolean(cached.isSuperAdmin),
    }
  } catch {
    userInfo.value = { name: '用户', avatar: defaultAvatar, isSuperAdmin: false }
  }
}

const refreshPermissions = async () => {
  try {
    const response = await getUserPermissions()
    if (response?.code === 200 && Array.isArray(response.data)) applyPermissions(response.data)
  } catch (error) {
    console.warn('刷新权限失败', error)
  }
}

const refreshUser = async () => {
  try {
    const response = await getTokenUser()
    const data = response?.data || {}
    localStorage.setItem('user', JSON.stringify(data))
    userInfo.value = {
      name: data.name || data.userName || userInfo.value.name,
      avatar: data.avatarUrl || data.avatar || data.headImgs?.[0]?.value || defaultAvatar,
      isSuperAdmin: Boolean(data.isSuperAdmin),
    }
    if (data.isSuperAdmin || data.phone || sessionStorage.getItem('eic-phone-reminded') === '1') return
    sessionStorage.setItem('eic-phone-reminded', '1')
    confirm({
      title: '完善手机号',
      content: '登录后请补充手机号，方便管理员联系。可稍后在个人中心填写，不会影响对话。',
      okText: '去填写',
      cancelText: '稍后再说',
      onOk: openProfile,
    })
  } catch (error) {
    console.warn('检查资料失败', error)
  }
}

const refreshNoReadCount = async () => {
  try {
    const response = await GetMyNoReadCount()
    if (response?.code === 200) noReadCount.value = Number(response.data) || 0
  } catch (error) {
    console.error('获取未读消息数量失败:', error)
  }
}

const navigateTo = (entry) => {
  if (props.experience) {
    if (experienceCanNavigate(entry?.key)) { mobileNavigationOpen.value = false; emit('experience-navigate', entry.key) }
    return
  }
  if (!entry?.path || !hasPermission(entry.permission)) return
  mobileNavigationOpen.value = false
  if (pageMode.value === 'tab') {
    window.open(entry.path, '_blank', 'noopener')
    return
  }
  router.push(entry.path)
}

const openProfile = () => navigateTo(navigationItemByKey.handleUserInfo || {
  key: 'handleUserInfo',
  path: routePathMap.handleUserInfo,
})

const openMessages = () => navigateTo(navigationItemByKey['my-message'])

const handleOpenPageEvent = (event) => {
  const key = event?.detail?.key
  const entry = navigationItemByKey[key]
  if (entry) navigateTo(entry)
}

const switchPageMode = (mode) => {
  if (mode !== 'single' && mode !== 'tab') return
  pageMode.value = mode
  localStorage.setItem('page-mode', mode)
}

const toggleFullScreen = async () => {
  try {
    if (!document.fullscreenElement) await document.documentElement.requestFullscreen()
    else await document.exitFullscreen()
  } catch (error) {
    console.warn('切换全屏失败', error)
  }
}

const syncFullScreen = () => {
  isFullScreen.value = Boolean(document.fullscreenElement)
}

const loggingOut = ref(false)
const handleLogout = async () => {
  if (props.experience) return emit('experience-leave')
  if (loggingOut.value) return
  loggingOut.value = true
  try {
    await http.post('/api/Authorize/Logout')
    localStorage.removeItem('token')
    localStorage.removeItem('eic-token')
    localStorage.removeItem('user')
    localStorage.removeItem('UserPermissions')
    await router.push('/login')
  } catch (error) {
    console.warn('退出登录未完成', error)
  } finally {
    loggingOut.value = false
  }
}

const userMenuOptions = computed(() => props.experience ? [
  { key: 'profile', label: '个人中心' }, { key: 'messages', label: '消息中心' },
  { key: 'theme', label: appearance.value === 'dark' ? '切换浅色模式' : '切换深色模式' },
  { key: 'fullscreen', label: '全屏显示' }, { key: 'logout', label: '结束体验并清除内容' },
] : [
  { key: 'profile', label: '个人中心' },
  { key: 'messages', label: noReadCount.value ? `消息中心（${noReadCount.value} 条未读）` : '消息中心' },
  { type: 'divider', key: 'separator-settings' },
  { key: 'theme', label: appearance.value === 'dark' ? '切换浅色模式' : '切换深色模式' },
  { key: 'single', label: `当前窗口${pageMode.value === 'single' ? ' · 已选择' : ''}` },
  { key: 'tab', label: `新浏览器标签页${pageMode.value === 'tab' ? ' · 已选择' : ''}` },
  { key: 'fullscreen', label: isFullScreen.value ? '退出全屏' : '全屏显示' },
  { type: 'divider', key: 'separator-logout' },
  { key: 'logout', label: '退出登录' },
])
function selectUserMenu(key) {
  userMenuOpen.value = false
  const actions = {
    profile: openProfile, messages: openMessages,
    theme: () => setAppearance(appearance.value === 'dark' ? 'light' : 'dark'),
    single: () => switchPageMode('single'), tab: () => switchPageMode('tab'),
    fullscreen: toggleFullScreen, logout: handleLogout,
  }
  actions[key]?.()
}

const mediaQuery = window.matchMedia('(max-width: 767.98px)')

const handleViewportChange = (event) => {
  isMobile.value = event.matches
  if (!event.matches) mobileNavigationOpen.value = false
}

const handleNavigationKeyboard = (event) => {
  if (event.key === 'Escape' && userMenuOpen.value) {
    event.preventDefault()
    event.stopPropagation()
    userMenuOpen.value = false
    nextTick(() => userMenuTrigger.value?.focus())
    return
  }
  if (!isMobile.value || !mobileNavigationOpen.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    mobileNavigationOpen.value = false
    return
  }
  if (event.key !== 'Tab') return
  const controls = Array.from(navigationElement.value?.querySelectorAll(
    'button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
  ) || []).filter((element) => element.getClientRects().length > 0 && !element.closest('[inert]'))
  if (!controls.length) return
  const first = controls[0]
  const last = controls[controls.length - 1]
  if (event.shiftKey && (document.activeElement === first || !navigationElement.value.contains(document.activeElement))) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (document.activeElement === last || !navigationElement.value.contains(document.activeElement))) {
    event.preventDefault()
    first.focus()
  }
}

watch(mobileNavigationOpen, async (open) => {
  if (open) {
    navigationOpener = document.activeElement
    previousBodyOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    await nextTick()
    if (mobileNavigationOpen.value) navigationElement.value?.querySelector('.console-mobile-close')?.focus()
  } else {
    userMenuOpen.value = false
    document.body.style.overflow = previousBodyOverflow
    await nextTick()
    if (isMobile.value && navigationOpener?.isConnected) navigationOpener.focus()
    navigationOpener = null
  }
})

watch(() => route.fullPath, () => {
  refreshWorkspaceContext()
  userMenuOpen.value = false
  mobileNavigationOpen.value = false
})

watch(sidebarCompact, () => { userMenuOpen.value = false })

onUnmounted(() => {
  contextAlive = false
  mediaQuery.removeEventListener?.('change', handleViewportChange)
  window.removeEventListener('eic-open-page', handleOpenPageEvent)
  document.removeEventListener('fullscreenchange', syncFullScreen)
  document.removeEventListener('keydown', handleNavigationKeyboard)
  if (mobileNavigationOpen.value) document.body.style.overflow = previousBodyOverflow
})

onMounted(() => {
  mediaQuery.addEventListener?.('change', handleViewportChange)
  document.addEventListener('keydown', handleNavigationKeyboard)
  if (props.experience) {
    userInfo.value = { name: '体验访客', avatar: defaultAvatar, isSuperAdmin: false }
    workspaceContext.value = { tenantName: props.experience.mode === 'public' ? '公开演示空间' : '限时体验空间', organizationName: '临时内容，结束自动清除' }
    return
  }
  const savedPageMode = localStorage.getItem('page-mode')
  pageMode.value = savedPageMode === 'tab' ? 'tab' : 'single'
  loadCachedUser()

  try {
    const cachedPermissions = JSON.parse(localStorage.getItem('UserPermissions') || '[]')
    applyPermissions(cachedPermissions)
  } catch {
    console.warn('权限数据解析失败，请重新登录')
    userPermissions.value = []
  }

  window.addEventListener('eic-open-page', handleOpenPageEvent)
  document.addEventListener('fullscreenchange', syncFullScreen)

  refreshPermissions()
  refreshWorkspaceContext()
  refreshUser()
  refreshNoReadCount()
})
</script>

<style>
@import './ConsoleLayout.css';
</style>

