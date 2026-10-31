import { ref } from 'vue'

const saved = localStorage.getItem('app-theme')
export const appearance = ref(saved === 'dark' || saved === 'eic-dark' ? 'dark' : 'light')
if (saved && !['dark', 'eic-dark', 'light', 'cloud-blue'].includes(saved)) {
  if (!localStorage.getItem('app-theme-before-cloud-blue')) {
    localStorage.setItem('app-theme-before-cloud-blue', saved)
  }
}
export function setAppearance(value) {
  if (!['light', 'dark'].includes(value)) return
  appearance.value = value
  try { localStorage.setItem('app-theme', value) } catch { /* 存储不可用时仍可预览和保存到服务器。 */ }
}
