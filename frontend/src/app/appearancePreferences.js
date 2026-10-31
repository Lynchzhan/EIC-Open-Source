import { ref } from 'vue'
import http from '@/utils/http'
import { setAppearance } from './appearance'

import { presets, ranges, fontSizes } from './typography'
export { presets, ranges, fontSizes } from './typography'
export const confirmedAppearance = ref(null)
export const appearanceError = ref('')
export const appearanceReady = ref(false)
export const appearanceSaving = ref(false)
let epoch = 0, ownerKey = '', editing = false, pendingLoad = null
const token = () => localStorage.getItem('token') || localStorage.getItem('eic-token') || ''
// 仅用于客户端响应归属判断；权限仍由服务端验证签名。
export function accountMarker(auth = token()) {
  if (!auth) return ''
  try {
    const segment = auth.split('.')[1].replace(/-/g,'+').replace(/_/g,'/')
    const value = JSON.parse(atob(segment.padEnd(Math.ceil(segment.length / 4) * 4, '=')))
    if (value.sub != null && value.tenant_id != null) return `${value.tenant_id}:${value.sub}`
  } catch { /* 非 JWT 使用完整令牌隔离。 */ }
  return auth
}
function validated(value) {
  if (value?.schemaVersion !== 1 || !Number.isSafeInteger(value.revision) || value.revision < 0 || !['light','dark'].includes(value.theme)) throw new Error('外观设置格式不兼容')
  for (const [key,[min,max]] of Object.entries(ranges)) {
    if (!Number.isInteger(value.fontSizes?.[key]) || value.fontSizes[key] < min || value.fontSizes[key] > max) throw new Error('字号必须为允许范围内的整数')
  }
  return JSON.parse(JSON.stringify(value))
}
export function previewAppearance(value) {
  const checked = validated({ ...value, schemaVersion:1, revision:0 })
  fontSizes.value = checked.fontSizes
  setAppearance(checked.theme)
}
function accept(value) {
  confirmedAppearance.value = validated(value)
  previewAppearance(value)
}
export function resetAccountAppearance() {
  epoch++; pendingLoad = null; ownerKey = ''; editing = false
  confirmedAppearance.value = null; appearanceReady.value = false; appearanceSaving.value = false; appearanceError.value = ''
  previewAppearance({ theme:'light', fontSizes:presets.standard })
}
export function loadAccountAppearance(force = false) {
  if (editing || appearanceSaving.value) return Promise.resolve()
  if (pendingLoad) return pendingLoad
  if (!force && appearanceReady.value) return Promise.resolve()
  const ticket = ++epoch, auth = accountMarker()
  if (!auth) { resetAccountAppearance(); return Promise.resolve() }
  const live = () => ticket === epoch && auth === accountMarker()
  const operation = (async () => {
    try {
      const identity = (await http.get('/api/User/GetUser', { timeout:15000 })).data
      if (!live()) return
      if (!identity?.id || !identity?.tenantId) throw new Error('无法确定当前账号')
      const key = `eic-v4-appearance:${identity.tenantId}:${identity.id}`
      if (ownerKey !== key) {
        ownerKey = key; confirmedAppearance.value = null; appearanceReady.value = false
        previewAppearance({ theme:'light', fontSizes:presets.standard })
        try { const cached = localStorage.getItem(key); if (cached) accept(JSON.parse(cached)) } catch { /* 无效缓存不应用。 */ }
      }
      const value = (await http.get('/api/User/AppearancePreferences', { timeout:15000 })).data
      if (!live() || editing) return
      accept(value); appearanceReady.value = true; appearanceError.value = ''
      try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* 缓存不可用不影响服务器保存。 */ }
    } catch (error) {
      if (live()) { appearanceReady.value = false; appearanceError.value = error.message || '外观设置未同步' }
    }
  })()
  pendingLoad = operation
  operation.finally(() => { if (pendingLoad === operation) pendingLoad = null })
  return operation
}
export function beginAppearanceEdit() { editing = true }
export function cancelAppearanceEdit() {
  editing = false
  if (confirmedAppearance.value) previewAppearance(confirmedAppearance.value)
}
export async function saveAccountAppearance(value) {
  if (!appearanceReady.value || !ownerKey || !confirmedAppearance.value) throw new Error('请先重新载入服务器设置')
  if (appearanceSaving.value) throw new Error('正在保存')
  const checked = validated({ ...value, schemaVersion:1, revision:confirmedAppearance.value.revision })
  const ticket = epoch, auth = accountMarker(), key = ownerKey
  appearanceSaving.value = true
  try {
    const result = await http.put('/api/User/AppearancePreferences', {
      schemaVersion:1, expectedRevision:checked.revision, theme:checked.theme, fontSizes:checked.fontSizes,
    }, { timeout:15000 })
    if (ticket !== epoch || auth !== accountMarker() || key !== ownerKey) return false
    accept(result.data); appearanceError.value = ''
    try { localStorage.setItem(key, JSON.stringify(result.data)) } catch { /* 服务器已保存。 */ }
    return true
  } catch (error) {
    if (ticket === epoch && auth === accountMarker()) appearanceError.value = error.message || '保存失败，尚未同步'
    throw error
  } finally { if (ticket === epoch) appearanceSaving.value = false }
}
