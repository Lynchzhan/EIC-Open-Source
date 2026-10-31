import { defineStore } from 'pinia'
import { ref } from 'vue'

function read<T>(key: string, fallback: T): T {
  try { return JSON.parse(localStorage.getItem(key) || 'null') ?? fallback }
  catch { return fallback }
}

export const usePreferences = defineStore('eic-v4-preferences', () => {
  const compact = ref(read<boolean>('eic-v4-compact', false) === true)
  const storedGroups = read<unknown>('eic-v4-collapsed-groups', [])
  const collapsedGroups = ref<string[]>(Array.isArray(storedGroups) ? storedGroups.filter(v => typeof v === 'string') : [])
  function toggleCompact() {
    compact.value = !compact.value
    localStorage.setItem('eic-v4-compact', JSON.stringify(compact.value))
  }
  function toggleGroup(key: string) {
    collapsedGroups.value = collapsedGroups.value.includes(key)
      ? collapsedGroups.value.filter(v => v !== key) : [...collapsedGroups.value, key]
    localStorage.setItem('eic-v4-collapsed-groups', JSON.stringify(collapsedGroups.value))
  }
  return { compact, collapsedGroups, toggleCompact, toggleGroup }
})
