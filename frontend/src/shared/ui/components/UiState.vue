<template>
  <div class="ui-state" :class="`ui-state-${kind}`" :role="kind === 'error' ? 'alert' : 'status'">
    <span class="ui-state-symbol" aria-hidden="true"><slot name="icon"><MessageOutlined /></slot></span>
    <h2 v-if="title">{{ title }}</h2>
    <p>{{ description }}</p>
    <div v-if="$slots.default" class="ui-state-actions"><slot /></div>
  </div>
</template>
<script setup>
import { MessageOutlined } from '@/app/ui/icons'
defineProps({ kind: { type: String, default: 'empty' }, title: String, description: String })
</script>
<style scoped>
.ui-state { display:grid; justify-items:center; text-align:center; padding:32px 20px; color:var(--eic-text-secondary); }
.ui-state-symbol { display:grid; place-items:center; width:52px; height:52px; margin-bottom:18px; border-radius:16px; background:var(--eic-primary-soft); color:var(--eic-primary-dark); font-size:24px; }
h2 { margin:0 0 10px; color:var(--eic-text); font-size:24px; font-weight:650; }
p { max-width:480px; margin:0; color:var(--eic-text-secondary); line-height:1.75; overflow-wrap:anywhere; }
.ui-state-actions { margin-top:24px; }
.ui-state-error .ui-state-symbol { color:var(--eic-danger); background:var(--eic-danger-soft); }
</style>
