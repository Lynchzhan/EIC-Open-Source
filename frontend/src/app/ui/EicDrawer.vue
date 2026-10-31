<template>
  <n-drawer :show="open" :width="width" :placement="placement" :mask-closable="maskClosable" :close-on-esc="keyboard && closable"
    :display-directive="destroyOnClose ? 'if' : 'show'" @update:show="close" @after-leave="$emit('afterClose')">
    <n-drawer-content :title="title" :closable="closable" :native-scrollbar="false">
      <slot /><template v-if="$slots.footer" #footer><slot name="footer" /></template>
    </n-drawer-content>
  </n-drawer>
</template>
<script setup lang="ts">
import { NDrawer, NDrawerContent } from 'naive-ui'
withDefaults(defineProps<{
  open?: boolean; title?: string; width?: number | string; placement?: 'left' | 'right' | 'top' | 'bottom';
  maskClosable?: boolean; keyboard?: boolean; closable?: boolean; destroyOnClose?: boolean;
}>(), { open: false, width: 'min(720px, 100vw)', placement: 'right', maskClosable: true, keyboard: true, closable: true })
const emit = defineEmits(['update:open', 'close', 'afterClose'])
function close(show: boolean) { if (!show) { emit('update:open', false); emit('close') } }
</script>
