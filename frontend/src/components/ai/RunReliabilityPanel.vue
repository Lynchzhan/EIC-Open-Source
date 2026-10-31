<template>
  <section class="run-reliability" aria-label="恢复、预算与上下文">
    <h4>恢复与运行边界</h4>
    <dl class="reliability-grid">
      <div><dt>当前节点</dt><dd>{{ view.node }}</dd></div>
      <div><dt>Checkpoint 序号</dt><dd data-testid="checkpoint-seq">{{ view.checkpoint }}</dd></div>
      <div><dt>物理模型调用</dt><dd>{{ view.invocations }}</dd></div>
      <div><dt>显式重试次数</dt><dd>{{ view.retries }}</dd></div>
    </dl>
    <p>Checkpoint 仅表示服务端持久化序号，不表示结果已确认或允许自动重放。此面板只读，不触发模型或工具。</p>
    <p v-if="view.hint" class="reliability-alert" role="alert">{{ view.hint }} <code v-if="view.errorCode">{{ view.errorCode }}</code></p>
    <h4>本请求预算与降级</h4>
    <dl class="reliability-grid">
      <div><dt>输入预算（估算单位）</dt><dd data-testid="budget-limit">{{ view.inputLimit }}</dd></div>
      <div><dt>已装配输入（估算单位）</dt><dd>{{ view.inputEstimate }}</dd></div>
      <div><dt>工具 Schema 预留</dt><dd>{{ view.toolReserve }}</dd></div>
      <div><dt>裁减历史消息</dt><dd>{{ view.dropped }}</dd></div>
      <div><dt>降级状态</dt><dd>{{ view.degraded }}</dd></div>
      <div><dt>降级路径</dt><dd>{{ view.fallback }}</dd></div>
    </dl>
    <p>预算来自当前 requestId 的持久历史元数据，采用 UTF-8 字节与协议开销保守估算，不是提供方真实 Token 或账单。实时并发、成本余额及熔断状态查询：未提供。</p>
    <details class="context-provenance">
      <summary>上下文来源与记忆边界</summary>
      <p>系统约束与本轮问题不可裁剪；历史按完整轮次裁减，检索、附件和记忆按预算选择。这里仅展示服务端来源计数，不展示提示词、正文或工具原始结果。</p>
      <ul v-if="view.sources.length">
        <li v-for="source in view.sources" :key="source.source">{{ source.label }}：保留 {{ source.retained }}，裁减 {{ source.dropped }}，状态未提供 {{ source.unknown }}</li>
      </ul>
      <p v-else>本请求上下文 manifest：未提供。</p>
      <code v-if="view.hash">Manifest SHA-256 {{ view.hash }}</code>
      <p>会话历史与持久记忆不同；过期时间、来源优先级及当前记忆权限未由本接口提供，不推断长期记忆已写入。当前实现为裁减，不声称摘要压缩已经启用。</p>
    </details>
  </section>
</template>
<script setup>
import { computed } from 'vue';
import { reliabilityView } from '@/shared/ui/runReliability';
const props = defineProps({ trace: { type: Object, default: () => ({}) }, usage: { type: Object, default: () => ({}) } });
const view = computed(() => reliabilityView(props.trace, props.usage));
</script>
<style scoped>
.run-reliability { margin:12px 0; padding:16px; background:var(--eic-surface,#fff); border:1px solid var(--eic-border,#dbe6f3); border-radius:12px; color:var(--eic-text,#14233e); }
h4 { margin:0 0 12px; font-size:var(--eic-font-ui); font-weight:650; }
h4:not(:first-child) { margin-top:20px; }
.reliability-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin:0 0 12px; }
dt { color:var(--eic-text-muted,#5a6b83); font-size:var(--eic-font-meta); } dd { margin:4px 0 0; font-size:var(--eic-font-ui); overflow-wrap:anywhere; }
p,li { font-size:12px; line-height:1.7; } p { margin:10px 0; }
code { display:block; font-size:11px; overflow-wrap:anywhere; }
.reliability-alert { padding:12px; background:var(--eic-warning-soft); border:1px solid var(--eic-warning); border-radius:8px; }
summary { cursor:pointer; color:var(--eic-primary,#2563eb); font-size:13px; padding:8px 0; }
summary:focus-visible { outline:2px solid var(--eic-primary,#2563eb); outline-offset:3px; }
ul { padding-left:18px; } @media(max-width:767.98px) { .run-reliability { padding:12px; } }
</style>
