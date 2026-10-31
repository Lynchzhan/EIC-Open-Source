<template>
  <n-modal :show="show" preset="card" title="限时体验账号登录" class="experience-access-modal"
    :mask-closable="!busy" :close-on-esc="!busy" :closable="!busy" @update:show="emit('update:show', $event)">
    <p>使用管理员发放的体验账号；进入后仅可使用授权资源，结束自动清除临时内容。</p>
    <n-form label-placement="top" @submit.prevent="enter">
      <n-form-item label="体验账号"><n-input v-model:value="username" :disabled="busy" autocomplete="off" :maxlength="80" /></n-form-item>
      <n-form-item label="体验密码"><n-input v-model:value="password" type="password" :disabled="busy" autocomplete="off" :maxlength="200" show-password-on="click" /></n-form-item>
      <p v-if="error" role="alert" class="experience-error">{{ error }}</p>
      <n-button type="primary" attr-type="submit" :loading="busy" :disabled="!username.trim() || !password">进入限时体验</n-button>
    </n-form>
  </n-modal>
</template>
<script setup>
import { ref, watch, onBeforeUnmount } from 'vue';
import { NModal, NForm, NFormItem, NInput, NButton } from 'naive-ui';
import { experienceRequest } from '@/api/experience';
const props = defineProps({ show:Boolean });
const emit = defineEmits(['update:show', 'entered']);
const username = ref(''), password = ref(''), busy = ref(false), error = ref('');
let controller, disposed = false;
watch(() => props.show, () => { password.value = ''; error.value = ''; });
async function enter() {
  if (busy.value) return;
  busy.value = true; error.value = ''; controller = new AbortController();
  try { await experienceRequest('/enter', { mode:'account', username:username.value, password:password.value }, 'POST', controller.signal);
    password.value = ''; if (!disposed) { emit('update:show', false); emit('entered'); } }
  catch (e) { if (!disposed && e.name !== 'AbortError') error.value = e.message; }
  finally { busy.value = false; }
}
onBeforeUnmount(() => { disposed = true; controller?.abort(); password.value = ''; });
</script>
