<template>
  <e-form layout="vertical" :disabled="disabled">
    <e-alert type="info" show-icon message="预算按整个聊天会话累计，包含回答、查询向量化和重排。文档入库单独统计；硬预算耗尽后停止。" />
    <e-row :gutter="16">
      <e-col v-for="field in fields" :key="field.key" :xs="24" :md="12">
        <e-form-item :label="field.label">
          <e-input-number :value="policy[field.key]" :min="1" :max="field.max" :precision="0" style="width:100%"
            @update:value="value => update({ [field.key]: value })" />
        </e-form-item>
      </e-col>
    </e-row>
    <e-form-item label="档位阈值或模型故障时">
      <e-select :value="policy.action" @update:value="value => update({ action: value })" :options="[
        { value:'stop', label:'停止后续调用' }, { value:'degrade', label:'按档位顺序切换模型' },
      ]" />
    </e-form-item>
    <template v-if="policy.action === 'degrade'">
      <p>按从上到下顺序使用模型，第一档即初始模型；每档阈值独立累计。触发后仅切换下一次调用，不重生成已有输出。放宽硬限制只影响新会话。</p>
      <fieldset v-for="(tier, index) in policy.tiers" :key="index" style="margin:12px 0;padding:12px">
        <legend>第 {{ index + 1 }} 档</legend>
        <e-form-item label="模型"><e-select :value="tier.modelId" :options="options" placeholder="选择聊天模型"
          @update:value="value => updateTier(index, { modelId:value })" /></e-form-item>
        <e-form-item label="本档调用次数阈值"><e-input-number :value="tier.maxCalls" :min="1" :max="policy.maxCalls" :precision="0"
          @update:value="value => updateTier(index, { maxCalls:value })" /></e-form-item>
        <e-form-item label="本档 Token 阈值"><e-input-number :value="tier.maxTokens" :min="1" :max="policy.maxTokens" :precision="0"
          @update:value="value => updateTier(index, { maxTokens:value })" /></e-form-item>
        <e-space wrap><e-button :disabled="disabled || index === 0" @click="move(index, -1)">上移</e-button>
          <e-button :disabled="disabled || index === policy.tiers.length - 1" @click="move(index, 1)">下移</e-button>
          <e-button danger @click="remove(index)">删除</e-button></e-space>
      </fieldset>
      <e-button :disabled="disabled || policy.tiers.length >= 10" @click="add">添加降级档位</e-button>
    </template>
  </e-form>
</template>
<script setup>
import { computed, watch } from 'vue';
const props = defineProps({ modelValue: Object, models: { type:Array, default:() => [] }, disabled:Boolean });
const emit = defineEmits(['update:modelValue']);
const defaults = { maxCalls:100, maxTokens:1000000, maxSeconds:1800, maxOutputTokens:4096,
  callTimeoutSeconds:120, maxSteps:8, action:'stop', tiers:[] };
const policy = computed(() => ({ ...defaults, ...props.modelValue }));
const options = computed(() => props.models.map(model => ({ value:String(model.id), label:model.modelDescription || model.modelName || String(model.id) })));
const fields = [
  { key:'maxCalls', label:'会话累计调用上限', max:1000 }, { key:'maxTokens', label:'会话累计 Token 上限', max:10000000 },
  { key:'maxSeconds', label:'会话累计调用时长（秒）', max:86400 }, { key:'maxOutputTokens', label:'单次最大输出 Token', max:32768 },
  { key:'callTimeoutSeconds', label:'单次调用超时（秒）', max:600 }, { key:'maxSteps', label:'每轮图执行最大模型步骤', max:8 },
];
const update = patch => emit('update:modelValue', { ...policy.value, ...patch });
watch(() => props.modelValue, value => {
  if (value == null && !props.disabled) update({ tiers:[] });
}, { immediate:true });
const updateTier = (index, patch) => update({ tiers:policy.value.tiers.map((item, i) => i === index ? { ...item, ...patch } : { ...item }) });
const add = () => update({ tiers:[...policy.value.tiers, { modelId:undefined, maxCalls:10, maxTokens:Math.min(100000, policy.value.maxTokens) }] });
const remove = index => update({ tiers:policy.value.tiers.filter((_, i) => i !== index) });
const move = (index, direction) => {
  const tiers = policy.value.tiers.slice();
  [tiers[index], tiers[index + direction]] = [tiers[index + direction], tiers[index]];
  update({ tiers });
};
</script>
