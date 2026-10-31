<template>
  <e-card size="small" title="会话标题与摘要模型" style="margin-bottom:16px">
    <e-alert v-if="error" type="error" :message="error" />
    <e-select v-model:value="modelId" :options="options" allow-clear placeholder="未指定时使用当前会话模型" aria-label="用于会话标题与摘要的模型" />
    <p>仅指定一个 Chat 模型。未指定或不可用时回退到当前会话模型；总结调用计入会话预算。</p>
    <e-button :loading="saving" :disabled="loading || !!error" @click="save">保存用途模型</e-button>
  </e-card>
</template>
<script setup>
import { ref, onMounted } from 'vue';
import http, { isMutationSuccess } from '@/utils/http';
import { message } from '@/app/ui/controls';
import { getAIModelsALLList } from '@/api/ai/aiModels';
const modelId = ref(null), revision = ref(0), options = ref([]), loading = ref(true), saving = ref(false), error = ref('');
onMounted(async () => {
  try {
    const [settings, models] = await Promise.all([http.get('/api/AIModels/ConversationLabelModel'), getAIModelsALLList(1)]);
    if (settings.code !== 200 || models.code !== 200) throw new Error('用途模型读取失败');
    modelId.value = settings.data.modelId; revision.value = settings.data.revision;
    options.value = models.data.map(v => ({ value: String(v.id), label: v.modelDescription || v.modelName }));
  } catch (exc) { error.value = exc.message; } finally { loading.value = false; }
});
const save = async () => {
  saving.value = true;
  try {
    const result = await http.put('/api/AIModels/ConversationLabelModel', { modelId: modelId.value, revision: revision.value });
    if (!isMutationSuccess(result)) throw new Error(result.msg || '保存失败');
    revision.value += 1; message.success('会话总结用途模型已保存');
  } catch (exc) { message.error(exc.message || '保存失败'); } finally { saving.value = false; }
};
</script>
