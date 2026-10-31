<template>
        <e-form layout="vertical">
          <e-form-item label="记忆模式" extra="默认关闭。自动提取仅生成待确认候选，确认后才会参与后续对话。">
            <e-select v-model:value="settings.mode" :disabled="disabled">
              <e-select-option value="off">关闭</e-select-option>
              <e-select-option value="manual">仅手工保存</e-select-option>
              <e-select-option value="review">后台提取并由我确认</e-select-option>
            </e-select>
          </e-form-item>
          <template v-if="settings.mode !== 'off'">
            <e-form-item label="记忆向量模型" extra="独立于知识库。留空使用关键词召回；更换模型后需在记忆面板重建索引。">
              <e-select v-model:value="settings.embeddingModelId" :options="embeddingOptions"
                :disabled="disabled || !embeddingReady" allow-clear placeholder="仅关键词" />
            </e-form-item>
            <e-form-item v-if="settings.mode === 'review'" label="候选提取模型" extra="留空使用会话模型。摘要、提取及向量调用计入同一会话运行预算。">
              <e-select v-model:value="settings.extractionModelId" :options="chatOptions"
                :disabled="disabled || !modelsReady" allow-clear placeholder="使用会话模型" />
            </e-form-item>
            <e-row :gutter="16">
              <e-col :xs="24" :sm="12"><e-form-item label="生效条目上限">
                <e-input-number v-model:value="settings.maxActiveMemories" :min="10" :max="10000" :precision="0" :disabled="disabled" />
              </e-form-item></e-col>
              <e-col :xs="24" :sm="12"><e-form-item label="待确认候选上限">
                <e-input-number v-model:value="settings.maxPendingCandidates" :min="5" :max="500" :precision="0" :disabled="disabled" />
              </e-form-item></e-col>
              <e-col :xs="24" :sm="12"><e-form-item label="默认有效天数（项目最多30天）">
                <e-input-number v-model:value="settings.retentionDays" :min="1" :max="3650" :precision="0" :disabled="disabled" />
              </e-form-item></e-col>
              <e-col :xs="24" :sm="12"><e-form-item label="每次最多提取候选数">
                <e-input-number v-model:value="settings.extractionMaxCandidates" :min="1" :max="10" :precision="0" :disabled="disabled" />
              </e-form-item></e-col>
              <e-col :xs="24" :sm="12"><e-form-item label="单条记忆字符上限">
                <e-input-number v-model:value="settings.maxMemoryChars" :min="100" :max="8000" :precision="0" :disabled="disabled" />
              </e-form-item></e-col>
            </e-row>
            <e-form-item label="允许经验候选" extra="默认关闭。经验必须有本次工具执行记录和适用范围，仍需人工确认。">
              <e-switch v-model:checked="settings.experienceEnabled" :disabled="disabled" />
            </e-form-item>
          </template>
        </e-form>
</template>
<script setup>
const settings = defineModel({ type:Object, required:true })
defineProps({ disabled:Boolean, modelsReady:Boolean, embeddingReady:Boolean, embeddingOptions:Array, chatOptions:Array })
</script>
