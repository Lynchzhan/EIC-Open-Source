<template><ChatRichContent :html="html" /></template>
<script setup>
import { computed } from 'vue'
import { marked } from 'marked'
import { sanitizeRichHtml } from '@/utils/safeHtml'
import ChatRichContent from './ChatRichContent.vue'

const props = defineProps({ content: { type: String, default: '' }, isUser: Boolean })
const html = computed(() => {
  let text = String(props.content ?? '')
  if (props.isUser) text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return sanitizeRichHtml(marked.parse(text, { async: false }))
})
</script>
