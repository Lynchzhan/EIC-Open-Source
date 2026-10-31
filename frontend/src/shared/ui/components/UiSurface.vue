<template>
  <section class="ui-surface" :aria-label="label">
    <header v-if="title || $slots.actions" class="ui-surface-heading">
      <div><h2 v-if="title">{{ title }}</h2><p v-if="description">{{ description }}</p></div>
      <div v-if="$slots.actions" class="ui-surface-actions"><slot name="actions" /></div>
    </header>
    <slot />
  </section>
</template>
<script setup>
defineProps({ title: String, description: String, label: String })
</script>
<style scoped>
.ui-surface { min-width:0; padding:24px; border:1px solid var(--eic-line); border-radius:var(--eic-radius-lg); background:var(--eic-surface); box-shadow:var(--eic-shadow); }
.ui-surface-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:20px; }
h2 { color:var(--eic-text); font-size:20px; line-height:1.4; margin:0; font-weight:650; }
p { color:var(--eic-text-secondary); margin:6px 0 0; line-height:1.6; }
.ui-surface-actions { flex-shrink:0; }
@media(max-width:600px) { .ui-surface { padding:18px; } .ui-surface-heading { flex-wrap:wrap; } }
</style>
