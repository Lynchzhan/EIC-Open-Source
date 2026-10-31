// 文件预览独占请求、版本和 Blob URL；ZIP 编辑由上传组件另行管理。
import { ref, watch, onBeforeUnmount } from 'vue'

export function useFilePreview() {
  const previewModalVisible = ref(false), previewVersion = ref(0), previewFile = ref(null)
  const previewContent = ref(''), previewHtml = ref(''), previewLoading = ref(false)
  const previewType = ref('text'), previewObjectUrl = ref('')
  let controller = null
  let disposed = false
  function releasePreview() {
    previewVersion.value++
    controller?.abort()
    controller = null
    if (previewObjectUrl.value) URL.revokeObjectURL(previewObjectUrl.value)
    previewObjectUrl.value = ''
    previewContent.value = ''
    previewHtml.value = ''
    previewLoading.value = false
  }
  function beginPreview(file) {
    if (disposed) return null
    releasePreview()
    controller = new AbortController()
    previewFile.value = file
    return { version:previewVersion.value, controller }
  }
  watch(previewModalVisible, open => { if (!open) releasePreview() })
  onBeforeUnmount(() => { disposed = true; releasePreview() })
  return { previewModalVisible, previewVersion, previewFile, previewContent, previewHtml,
    previewLoading, previewType, previewObjectUrl, beginPreview, releasePreview }
}
