<template>
  <n-modal :show="open" preset="card" :title="title" :closable="closable" :mask-closable="maskClosable"
    :close-on-esc="keyboard" :class="wrapClassName" :style="{ width: cssWidth, maxWidth: 'calc(100vw - 32px)', maxHeight: 'calc(100dvh - 40px)', overflow: 'auto' }"
    :display-directive="destroyOnClose ? 'if' : 'show'" @update:show="close" @after-leave="$emit('afterClose')">
    <slot />
    <template v-if="footer !== null" #footer>
      <slot name="footer"><div class="eic-modal-actions">
        <n-button v-bind="cancelButtonProps" @click="close(false)">{{ cancelText }}</n-button>
        <n-button :type="okType === 'danger' ? 'error' : 'primary'" :loading="confirmLoading" v-bind="okButtonProps" @click="$emit('ok')">{{ okText }}</n-button>
      </div></slot>
    </template>
  </n-modal>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { NModal, NButton } from 'naive-ui'
const props = withDefaults(defineProps<{
  open?: boolean; title?: string; width?: number | string; footer?: unknown;
  closable?: boolean; maskClosable?: boolean; keyboard?: boolean; destroyOnClose?: boolean;
  confirmLoading?: boolean; okText?: string; cancelText?: string; okType?: string;
  okButtonProps?: Record<string, unknown>; cancelButtonProps?: Record<string, unknown>; wrapClassName?: string;
}>(), { open: false, width: 560, closable: true, maskClosable: true, keyboard: true, okText: '确认', cancelText: '取消' })
const emit = defineEmits(['update:open', 'cancel', 'ok', 'afterClose'])
const cssWidth = computed(() => typeof props.width === 'number' ? `${props.width}px` : props.width)
function close(show: boolean) {
  if (show || props.confirmLoading) return
  emit('update:open', false)
  emit('cancel')
}
</script>
<style>.eic-modal-actions { display:flex; justify-content:flex-end; gap:12px; }</style>
