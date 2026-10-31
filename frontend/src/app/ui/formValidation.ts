import { reactive, toRaw, unref, watch } from 'vue'
import Schema from 'async-validator'

// 现有编辑器有独立于表单 DOM 的完整提交校验，保留这一行为。
export function useForm(model: any, rules: any) {
  const initial = structuredClone(toRaw(unref(model)))
  const validateInfos = reactive<Record<string, { validationStatus?: 'error'; feedback?: string }>>({})
  let revision = 0
  async function validate(names?: string | string[]) {
    const ticket = ++revision
    const all = unref(rules) || {}
    const keys = names == null ? Object.keys(all) : Array.isArray(names) ? names : [names]
    const selected = Object.fromEntries(keys.filter(key => key in all).map(key => [key, all[key]]))
    try {
      const source = unref(model)
      const values = Object.fromEntries(keys.map(key => [key, key.split('.').reduce((value, part) => value?.[part], source)]))
      await new Schema(selected).validate(values, { firstFields: true })
      if (ticket === revision) keys.forEach(key => { validateInfos[key] = {} })
      return unref(model)
    } catch (error: any) {
      if (ticket === revision) {
        keys.forEach(key => { validateInfos[key] = {} })
        for (const [key, entries] of Object.entries(error.fields || {})) {
          validateInfos[key] = { validationStatus: 'error', feedback: (entries as any[]).map(v => v.message).join('；') }
        }
      }
      throw error
    }
  }
  function clearValidate() { revision++; Object.keys(validateInfos).forEach(key => { validateInfos[key] = {} }) }
  function resetFields(values?: Record<string, unknown>) {
    clearValidate()
    Object.assign(unref(model), structuredClone(initial), values || {})
  }
  watch(() => unref(rules), () => clearValidate(), { deep: true })
  return { validate, validateInfos, resetFields, clearValidate }
}
