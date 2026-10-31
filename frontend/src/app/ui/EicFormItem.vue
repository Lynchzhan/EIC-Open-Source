<template>
  <n-form-item v-bind="$attrs" :label="label" :path="path || (Array.isArray(name) ? name.join('.') : name)" :required="required" :rule="rules"
    :validation-status="validationStatus" :feedback="feedback || help">
    <div class="eic-form-control"><slot /><p v-if="extra" class="eic-field-description">{{ extra }}</p></div>
  </n-form-item>
</template>
<script setup>
import { NFormItem } from 'naive-ui'
defineOptions({ inheritAttrs: false })
defineProps({ label: String, path: String, name: [String, Array], required: Boolean, rules: [Array, Object], validationStatus: String, feedback: String, help: String, extra: String })
</script>
<style>.eic-form-control { width:100%; min-width:0; }.eic-field-description { color:var(--eic-muted); font-size:12px; line-height:1.7; margin:6px 0 0; }</style>
