<template>
        <e-form :model="settings" :label-col="{ span: 8 }" :wrapper-col="{ span: 16 }">
          <e-row :gutter="16">
            <e-col :span="12">
              <e-form-item label="短期上下文" extra="关闭后不自动加载当前会话历史；长期记忆独立设置。">
                <e-switch v-model:checked="shortTermEnabled" :disabled="disabled" />
              </e-form-item>
            </e-col>
            <e-col :span="12">
              <e-form-item label="上下文策略">
                <e-select v-model:value="settings.mode" :disabled="disabled">
                  <e-select-option value="trim">仅裁剪</e-select-option>
                  <e-select-option value="summary">近期对话与结构化摘要</e-select-option>
                  <e-select-option value="retrieval">按需检索历史</e-select-option>
                </e-select>
              </e-form-item>
            </e-col>
          </e-row>
          <template v-if="settings.mode !== 'trim'">
            <e-row :gutter="16"> 
              <e-col :span="12">
                <e-form-item label="自动压缩">
                  <e-tooltip title="达到预算触发比例时自动准备摘要；额外模型调用计入运行预算。">
                    <e-switch v-model:checked="settings.automatic" :disabled="disabled" />
                  </e-tooltip>
                </e-form-item>
              </e-col>
              <e-col :span="12"> 
                <e-form-item label="保留完整轮次">
                  <e-input-number
                    v-model:value="settings.keepTurns"
                    :min="4"
                    :max="20"
                    :precision="0"
                    style="width: 100%"
                    placeholder="默认保留6轮"
                    :disabled="disabled"
                  />
                </e-form-item>
              </e-col>
            </e-row>
            <e-row :gutter="16">
              <e-col :span="12">
                <e-form-item label="预算触发比例(%)">
                  <e-input-number v-model:value="settings.triggerPercent" :min="50" :max="90" :precision="0" style="width: 100%" :disabled="disabled" />
                </e-form-item>
              </e-col>
              <e-col :span="12">
                <e-form-item label="摘要模型" extra="留空使用会话模型。">
                  <e-select v-model:value="summaryModelId" :options="chatOptions" allow-clear show-search optionFilterProp="label" placeholder="使用会话模型" :disabled="disabled || !modelsReady" />
                </e-form-item>
              </e-col>
            </e-row>
            <e-row :gutter="16">
              <e-col :span="4">
              压缩策略提示词：
              </e-col>
              <e-col :span="18">
                <e-textarea
                    v-model:value="settings.summaryPrompt"
                    :rows="16"
                    :placeholder="defaultSummaryPrompt"
                    :disabled="disabled"
                    style="text-align: left;"
                  />
              </e-col>
            </e-row>
          </template>
        </e-form>
</template>
<script setup>
const settings = defineModel('settings', { type:Object, required:true })
const shortTermEnabled = defineModel('shortTermEnabled', { type:Boolean })
const summaryModelId = defineModel('summaryModelId')
defineProps({ disabled:Boolean, modelsReady:Boolean, chatOptions:Array, defaultSummaryPrompt:String })
</script>
