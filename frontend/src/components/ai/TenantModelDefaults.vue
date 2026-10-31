<template>
  <e-card size="small" title="租户默认知识库模型" style="margin-bottom:16px">
    <e-alert v-if="error" type="error" :message="error" show-icon />
    <e-form layout="vertical">
      <e-form-item label="默认向量模型">
        <e-select v-model:value="defaults.embeddingModelId" :options="embeddings" allow-clear placeholder="未设置默认向量模型" />
      </e-form-item>
      <e-form-item label="默认 Rerank 模型">
        <e-select v-model:value="defaults.rerankModelId" :options="rerankers" allow-clear placeholder="未设置默认重排模型" />
      </e-form-item>
      <p>未绑定的知识库使用此设置。已有向量索引保留原模型；更换模型需要显式配置并重建索引。</p>
      <e-button :disabled="loading || !!error" :loading="saving" @click="save">保存默认设置</e-button>
    </e-form>
  </e-card>
</template>
<script setup>
import { onMounted, reactive, ref } from 'vue';
import { message } from '@/app/ui/controls';
import http, { isMutationSuccess } from '@/utils/http';
import { getAIModelsALLList } from '@/api/ai/aiModels';
const defaults = reactive({ embeddingModelId: null, rerankModelId: null });
const embeddings = ref([]);
const rerankers = ref([]);
const error = ref('');
const loading = ref(true);
const saving = ref(false);
onMounted(async () => {
  try {
    const [current, embedding, rerank] = await Promise.all([
      http.get('/api/AIModels/Defaults'), getAIModelsALLList(2), getAIModelsALLList(4),
    ]);
    if ([current, embedding, rerank].some(item => item.code !== 200)) throw new Error('默认模型读取失败');
    Object.assign(defaults, current.data);
    const options = list => list.map(item => ({ value: String(item.id), label: item.modelDescription || item.modelName }));
    embeddings.value = options(embedding.data);
    rerankers.value = options(rerank.data);
  } catch (exc) { error.value = exc.message || '默认模型读取失败'; }
  finally { loading.value = false; }
});
const save = async () => {
  saving.value = true;
  try {
    const result = await http.post('/api/AIModels/Defaults', defaults);
    if (!isMutationSuccess(result)) throw new Error(result.message || '保存失败');
    message.success('默认模型已保存，已有索引模型保持绑定');
  } catch (exc) { message.error(exc.message || '保存失败'); }
  finally { saving.value = false; }
};
</script>
