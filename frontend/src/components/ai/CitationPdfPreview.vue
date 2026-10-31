<template>
  <section ref="container" class="pdf-preview" aria-label="引用 PDF 原文件">
    <div class="pdf-toolbar">
      <button type="button" :disabled="!document || pageNumber <= 1 || pageNumber > pageCount" @click="showPage(pageNumber - 1)">上一页</button>
      <span aria-live="polite">第 {{ pageNumber }} / {{ pageCount || '—' }} 页</span>
      <button type="button" :disabled="!document || pageNumber >= pageCount" @click="showPage(pageNumber + 1)">下一页</button>
      <button v-for="page in citationPages" :key="page" type="button" :disabled="!document || page > pageCount" @click="showPage(page)">引用第 {{ page }} 页</button>
    </div>
    <p v-if="loading" role="status">正在渲染 PDF 页面…</p>
    <p v-if="error" role="alert">{{ error }}；可下载原文件查看。</p>
    <p v-if="renderedPage" role="status">已显示第 {{ renderedPage }} 页</p>
    <div class="pdf-canvas-scroll">
      <canvas ref="canvas" v-show="renderedPage" :data-rendered-page="renderedPage" :aria-label="`PDF 第 ${renderedPage || pageNumber} 页`" />
    </div>
    <details v-if="pageText"><summary>本页文字（辅助阅读）</summary><pre>{{ pageText }}</pre></details>
  </section>
</template>
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
const props = defineProps({ blob: { type: Blob, required: true }, pages: { type: Array, default: () => [] } })
const container = ref(null), canvas = ref(null), document = shallowRef(null)
const pageNumber = ref(1), pageCount = ref(0), renderedPage = ref(0), pageText = ref('')
const loading = ref(false), error = ref('')
const citationPages = computed(() => [...new Set(props.pages.filter(page => Number.isSafeInteger(page) && page > 0))])
let loadTask, renderTask, observer, timer, generation = 0, renderSequence = 0, lastWidth = 0
function dispose() {
  generation++; renderSequence++; clearTimeout(timer)
  renderTask?.cancel(); renderTask = null
  const previous = loadTask
  loadTask = null; document.value = null
  if (previous) void previous.destroy().catch(() => {})
}
async function render() {
  const doc = document.value, target = canvas.value, currentPage = pageNumber.value
  if (!doc || !target) return
  if (currentPage < 1 || currentPage > doc.numPages) {
    loading.value = false
    error.value = '引用页码超过当前原文件页数，请核对文件版本'
    return
  }
  const ticket = ++renderSequence
  const previous = renderTask
  previous?.cancel()
  loading.value = true; error.value = ''; renderedPage.value = 0; pageText.value = ''
  try {
    if (previous) await previous.promise.catch(() => {})
    const page = await doc.getPage(currentPage)
    if (ticket !== renderSequence) return
    const base = page.getViewport({ scale: 1 })
    const width = Math.max(200, Math.min((container.value?.clientWidth || 780) - 24, 1200))
    const scale = Math.min(width / base.width, 4096 / base.width, 4096 / base.height,
      Math.sqrt(6000000 / (base.width * base.height)))
    const viewport = page.getViewport({ scale })
    target.width = Math.ceil(viewport.width); target.height = Math.ceil(viewport.height)
    renderTask = page.render({ canvasContext: target.getContext('2d'), viewport })
    await renderTask.promise
    if (ticket !== renderSequence) return
    renderedPage.value = currentPage
    const text = await page.getTextContent()
    if (ticket === renderSequence) pageText.value = text.items.map(item => item.str || '').join(' ').slice(0, 100000)
  } catch (cause) {
    if (ticket === renderSequence && cause?.name !== 'RenderingCancelledException') error.value = 'PDF 页面渲染失败'
  } finally { if (ticket === renderSequence) loading.value = false }
}
function showPage(page) {
  if (!Number.isSafeInteger(page) || page < 1 || page > pageCount.value) return
  pageNumber.value = page; void render()
}
async function load() {
  dispose()
  const ticket = generation
  loading.value = true; error.value = ''; renderedPage.value = 0; pageText.value = ''; pageCount.value = 0
  pageNumber.value = citationPages.value[0] || 1
  try {
    const pdf = await import('pdfjs-dist/build/pdf.mjs')
    const { default: workerUrl } = await import('pdfjs-dist/build/pdf.worker.min.mjs?url')
    const bytes = new Uint8Array(await props.blob.arrayBuffer())
    if (ticket !== generation) return
    pdf.GlobalWorkerOptions.workerSrc = workerUrl
    loadTask = pdf.getDocument({ data: bytes, isEvalSupported: false, useSystemFonts: true,
      cMapUrl: `${import.meta.env.BASE_URL}assets/pdf/cmaps/`, cMapPacked: true,
      standardFontDataUrl: `${import.meta.env.BASE_URL}assets/pdf/standard_fonts/`,
      wasmUrl: `${import.meta.env.BASE_URL}assets/pdf/wasm/` })
    const doc = await loadTask.promise
    if (ticket !== generation) return
    document.value = doc; pageCount.value = doc.numPages
    if (pageNumber.value > doc.numPages) throw new Error('引用页码超过当前原文件页数，请核对文件版本')
    await render()
  } catch (cause) {
    if (ticket === generation) error.value = cause?.message === '引用页码超过当前原文件页数，请核对文件版本'
      ? cause.message : 'PDF 加载失败（文件可能受密码保护或格式不完整）'
  } finally { if (ticket === generation) loading.value = false }
}
watch(() => props.blob, load, { flush: 'post' })
onMounted(() => {
  void load()
  observer = new ResizeObserver(entries => {
    const width = Math.round(entries[0].contentRect.width)
    if (width === lastWidth) return
    lastWidth = width; clearTimeout(timer)
    timer = setTimeout(() => { if (document.value) void render() }, 150)
  })
  observer.observe(container.value)
})
onBeforeUnmount(() => { observer?.disconnect(); dispose() })
</script>
<style scoped>
.pdf-preview { margin-top:12px; border:1px solid var(--eic-line); padding:12px; border-radius:8px; }
.pdf-toolbar { display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
.pdf-toolbar button { min-height:40px; padding:6px 10px; font:inherit; cursor:pointer; }
.pdf-toolbar button:disabled { cursor:default; opacity:.5; }
.pdf-canvas-scroll { max-height:55vh; overflow:auto; background:#e5e7eb; }
canvas { display:block; margin:0 auto; max-width:100%; background:white; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; max-height:180px; overflow:auto; }
</style>
