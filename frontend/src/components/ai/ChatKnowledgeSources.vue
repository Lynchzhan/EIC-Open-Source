<template>
  <div class="knowledge-citations" aria-label="知识库检索来源">
    <button v-for="(source, index) in sources" :key="index" type="button" @click="openSource(source)">
      {{ source.documentName || `知识库来源 ${index + 1}` }}<span v-if="pages(source).length"> · 第 {{ pages(source).join('、') }} 页</span>
    </button>
  </div>
  <e-modal :open="!!selected" title="知识库检索来源" :width="900" :footer="null" @cancel="close">
    <template v-if="selected">
      <h3>{{ selected.documentName || '历史检索记录' }}</h3>
      <p v-if="selected.knowledgeBaseName">知识库：{{ selected.knowledgeBaseName }}</p>
      <p class="citation-excerpt">{{ selected.content || selected.logContent }}</p>
      <p v-if="pages(selected).length">引用页码：{{ pages(selected).join('、') }}</p>
      <p v-else>原引用未提供页码</p>
      <e-button v-if="selected.documentId && requestId" :loading="loading" @click="preview">查看原文件</e-button>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="pdfBlob">当前原文件版本；历史引用片段以本窗口上方记录为准。PDF 无法显示时，可下载后按引用页码查看。</p>
      <e-button v-if="originalBlob" @click="download">下载原文件</e-button>
      <CitationPdfPreview v-if="pdfBlob" :blob="pdfBlob" :pages="pages(selected)" />
      <pre v-if="textContent" class="citation-text">{{ textContent }}</pre>
    </template>
  </e-modal>
</template>
<script setup>
import { defineAsyncComponent, onBeforeUnmount, ref, shallowRef } from 'vue'
import http from '@/utils/http'
import { loadPrivateFile, inertPreviewBlob, savePrivateBlob } from '@/utils/privateFiles'
const props = defineProps({ sources: { type: Array, default: () => [] }, requestId: String })
const CitationPdfPreview = defineAsyncComponent(() => import('./CitationPdfPreview.vue'))
const selected = ref(null), loading = ref(false), error = ref(''), textContent = ref('')
const pdfBlob = shallowRef(null)
const originalBlob = ref(null)
let originalName = ''
let sequence = 0
let controller
const pages = source => [...new Set((Array.isArray(source?.pageNumbers) ? source.pageNumbers : [])
  .filter(page => Number.isSafeInteger(page) && page > 0))]
function close() {
  sequence++; controller?.abort(); loading.value = false
  pdfBlob.value = null; textContent.value = ''; error.value = ''; selected.value = null
  originalBlob.value = null; originalName = ''
}
function openSource(source) { close(); selected.value = source }
function download() { if (originalBlob.value) savePrivateBlob(originalBlob.value, originalName) }
async function preview() {
  if (loading.value || !selected.value?.documentId || !props.requestId) return
  const ticket = ++sequence
  controller = new AbortController()
  loading.value = true; error.value = ''
  pdfBlob.value = null; textContent.value = ''
  try {
    const response = await http.get('/api/AIChatHistorys/CitationSource', {
      params: { requestId: props.requestId, documentId: selected.value.documentId }, signal: controller.signal,
    })
    if (ticket !== sequence) return
    if (response?.code !== 200 || !response.data?.fileId) throw new Error('该引用没有可访问的原文件')
    const { blob, metadata } = await loadPrivateFile(response.data.fileId, { signal: controller.signal })
    if (ticket !== sequence) return
    originalBlob.value = blob; originalName = metadata.name
    if (/\.pdf$/i.test(metadata.name)) {
      const safe = await inertPreviewBlob(blob, metadata.name)
      if (ticket !== sequence) return
      pdfBlob.value = safe
    } else if (/\.(txt|md)$/i.test(metadata.name)) {
      const text = await blob.text()
      if (ticket === sequence) textContent.value = text.slice(0, 100000)
    } else throw new Error('此格式暂不支持内嵌预览，请从知识库查看原文件')
  } catch (cause) { if (ticket === sequence) error.value = cause?.message || '原文件预览失败' }
  finally { if (ticket === sequence) loading.value = false }
}
onBeforeUnmount(close)
</script>
<style scoped>
.knowledge-citations { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
.knowledge-citations button { padding:8px 12px; min-height:44px; border:1px solid var(--eic-line-strong); border-radius:10px; background:var(--eic-surface-muted); color:var(--eic-primary-dark); font:inherit; cursor:pointer; }
.knowledge-citations button:focus-visible { outline:3px solid var(--eic-primary); outline-offset:3px; }
.citation-excerpt,.citation-text { white-space:pre-wrap; overflow-wrap:anywhere; }
.citation-text { max-height:55vh; overflow:auto; }
</style>
