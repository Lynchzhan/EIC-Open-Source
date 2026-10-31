<template>
  <img v-if="resolvedSrc" :src="resolvedSrc" :alt="alt" @error="failed" />
  <span v-else :title="errorText || '暂无头像'">{{ errorText ? '头像暂不可用' : '暂无头像' }}</span>
</template>

<script setup>
import { ref, watch, onUnmounted } from 'vue';
import { loadPrivateFile, inertPreviewBlob } from '@/utils/privateFiles';
import defaultImage from '@/assets/eic-avatar.svg';

const props = defineProps({ source: [String, Number, Object], alt: { type: String, default: '头像' } });
const resolvedSrc = ref('');
const errorText = ref('');
let revision = 0;
let controller = null;
let objectUrl = '';
const release = () => {
  controller?.abort();
  controller = null;
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = '';
};
const failed = () => {
  release();
  resolvedSrc.value = '';
  errorText.value = '头像加载失败，请刷新或重新上传';
};
watch(() => props.source, async (source) => {
  const version = ++revision;
  release();
  resolvedSrc.value = '';
  errorText.value = '';
  // 默认标识来自随版本发布的可信资源；上传和远端图片仍走原授权校验。
  if (!source || source === '/img/hede.4f50e202.png' || source === '/assets/hede-Fa0gjV0i.png' || source === defaultImage) {
    resolvedSrc.value = defaultImage;
    return;
  }
  if (typeof source === 'string' && /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/=]+$/.test(source) && source.length <= 3 * 1024 * 1024) {
    resolvedSrc.value = source;
    return;
  }
  const current = new AbortController();
  controller = current;
  try {
    const { blob, metadata } = await loadPrivateFile(source, { maxBytes: 2 * 1024 * 1024, signal: current.signal });
    const safe = await inertPreviewBlob(blob, metadata.name);
    if (!safe.type.startsWith('image/')) throw new Error('头像需要有效图片文件');
    if (version !== revision || current.signal.aborted) return;
    objectUrl = URL.createObjectURL(safe);
    resolvedSrc.value = objectUrl;
  } catch (error) {
    if (version === revision && !current.signal.aborted) errorText.value = error.message || '头像加载失败';
  }
}, { immediate: true });
onUnmounted(() => { revision++; release(); });
</script>
