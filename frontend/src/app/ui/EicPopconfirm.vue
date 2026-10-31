<template>
  <n-popconfirm :positive-text="okText" :negative-text="cancelText" :disabled="disabled"
    @positive-click="confirm" @negative-click="$emit('cancel')">
    <template #trigger><slot /></template>{{ title }}
  </n-popconfirm>
</template>
<script setup>
import { NPopconfirm } from 'naive-ui'
import { message } from './feedback'
const props = defineProps({ title: String, okText: { type:String, default:'确认' }, cancelText: { type:String, default:'取消' }, disabled:Boolean, onConfirm:Function })
defineEmits(['cancel'])
let busy = false
async function confirm() {
  if (busy) return false
  busy = true
  try { return await props.onConfirm?.() }
  catch (error) { message.error(error?.message || '操作失败，请重试'); return false }
  finally { busy = false }
}
</script>
