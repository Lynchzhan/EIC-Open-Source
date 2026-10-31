<template><div class="message-text" v-html="html"></div></template>
<script setup>
// 输入由调用方的既有 safeMessageHtml 完成净化；不重新解释 Markdown。
defineProps({ html: { type: String, default: '' } })
</script>
