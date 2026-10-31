<template>
  <section class="experience-preview">
    <h2>{{ profile ? '个人中心' : codegen ? '业务代码生成器' : '运行观测' }}</h2>
    <p>虚构展示预览，所有示例均不读取正式账号或运行记录。</p>
    <n-card v-if="profile" title="示例访客资料">
      <n-form label-placement="top" @submit.prevent="limited">
        <n-form-item label="姓名"><n-input value="示例访客" readonly /></n-form-item>
        <n-form-item label="组织"><n-input value="示例研发部" readonly /></n-form-item>
        <n-button @click="limited">保存资料</n-button>
      </n-form>
    </n-card>
    <n-card v-else-if="codegen" title="业务代码生成器">
      <n-form-item label="业务区域"><n-select value="Demo" :options="[{label:'Demo', value:'Demo'}]" /></n-form-item>
      <n-data-table :columns="[{title:'实体', key:'entityName'},{title:'说明', key:'description'},{title:'字段', key:'fields'}]"
        :data="[{entityName:'DemoItem', description:'虚构示例实体', fields:'id、name'}]" />
      <n-button @click="limited">下载示例代码包</n-button>
    </n-card>
    <n-card v-else title="示例运行摘要">
      <p>检索 → 生成 → 完成 · 示例耗时 1.2 秒</p>
      <p>来源：虚构员工手册；无真实 Trace、模型请求或外部日志。</p>
    </n-card>
  </section>
</template>
<script setup>
import { computed } from 'vue';
import { NCard, NForm, NFormItem, NInput, NButton, NSelect, NDataTable } from 'naive-ui';
import { experienceContext } from './runtime';
import { message } from '@/app/ui/feedback';
const profile = computed(() => experienceContext.value?.page === 'profile');
const codegen = computed(() => experienceContext.value?.page === 'codegen');
const limited = () => message.info('体验账号不可操作');
</script>
