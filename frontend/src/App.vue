<template>
  <n-config-provider :theme="dark ? darkTheme : null" :theme-overrides="naiveTheme" :locale="zhCN" :date-locale="dateZhCN">
    <router-view />
  </n-config-provider>
</template>

<script setup>
import { computed, onMounted, onUnmounted, watch, watchEffect } from 'vue'
import { useRoute } from 'vue-router'
import { accountMarker, fontSizes, loadAccountAppearance, resetAccountAppearance } from './app/appearancePreferences'
import { NConfigProvider, darkTheme, zhCN, dateZhCN } from 'naive-ui'
import { createTheme } from './app/ui/theme'
import { appearance } from './app/appearance'
const dark = computed(() => appearance.value === 'dark')
const naiveTheme = computed(() => createTheme(dark.value, fontSizes.value))
const route = useRoute()
function handleAccountStorage(event) {
  if (event.key === null || event.key === 'token' || event.key === 'eic-token') {
    if (event.key !== null && accountMarker(event.oldValue || '') === accountMarker(event.newValue || '')) return
    resetAccountAppearance()
    // 其他标签页换账号时重建全部业务状态，避免旧草稿保存到新账号。
    window.location.reload()
  }
}
onMounted(() => window.addEventListener('storage', handleAccountStorage))
onUnmounted(() => window.removeEventListener('storage', handleAccountStorage))
watch(() => route.path === '/login' || route.path.startsWith('/experience'), isolated => {
  if (isolated) resetAccountAppearance()
  else loadAccountAppearance()
}, { immediate:true })
watchEffect(() => {
  document.documentElement.dataset.eicTheme = dark.value ? 'dark' : 'light'
  document.documentElement.style.colorScheme = dark.value ? 'dark' : 'light'
  for (const [key,value] of Object.entries(fontSizes.value)) document.documentElement.style.setProperty('--eic-font-'+key, value+'px')
  document.documentElement.style.setProperty('--eic-font-button', Math.max(14,fontSizes.value.ui-1)+'px')
  document.documentElement.style.setProperty('--eic-font-title', (fontSizes.value.ui+11)+'px')
  document.documentElement.style.setProperty('--eic-font-section', (fontSizes.value.ui+4)+'px')
}, { flush: 'sync' })
</script>
