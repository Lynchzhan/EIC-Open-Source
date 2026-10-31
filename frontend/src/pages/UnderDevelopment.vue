<template>
  <e-card title="功能暂未提供">
    <e-empty description="此页面尚无可用业务功能，请使用导航中的已开放模块。" />
    <e-button type="primary" @click="router.push('/home/my/ai-chat')">返回 AI 对话</e-button>
  </e-card>
</template>
<script setup>
import { useRouter } from 'vue-router'
const router = useRouter()
</script>
