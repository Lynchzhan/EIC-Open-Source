import { ref } from 'vue'

export const presets = {
  standard: { nav:15, ui:15, chat:16, meta:13, code:14 },
  large: { nav:17, ui:17, chat:19, meta:15, code:16 },
  extra: { nav:20, ui:20, chat:22, meta:18, code:19 },
}
export const ranges = { nav:[14,20], ui:[14,20], chat:[14,24], meta:[12,18], code:[12,22] }
export const fontSizes = ref({ ...presets.standard })
