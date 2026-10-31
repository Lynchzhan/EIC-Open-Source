import { computed, onMounted, onUnmounted, ref, watch } from 'vue';

const DEFAULT_WIDTH = 220;
const MIN_WIDTH = 200;
const MAX_WIDTH = 360;
const STORAGE_KEY = 'eic:chat-sidebar-width:v1';

export function useConversationResize({ layout, narrow, open, persist = true }) {
  const preferredWidth = ref(DEFAULT_WIDTH);
  const availableWidth = ref(MAX_WIDTH);
  const resizing = ref(false);
  const maximum = computed(() => Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, availableWidth.value)));
  const width = computed(() => Math.max(MIN_WIDTH, Math.min(maximum.value, preferredWidth.value)));
  let observer;
  let drag;

  function save() {
    if (!persist) return;
    try { localStorage.setItem(STORAGE_KEY, String(preferredWidth.value)); } catch { /* 浏览器可禁用偏好存储。 */ }
  }
  function setWidth(value) {
    if (!Number.isFinite(value)) return;
    preferredWidth.value = Math.max(MIN_WIDTH, Math.min(maximum.value, value));
  }
  function finish() {
    if (!drag) return;
    const current = drag;
    drag = null;
    resizing.value = false;
    if (current.target.hasPointerCapture?.(current.id)) current.target.releasePointerCapture(current.id);
    save();
  }
  function start(event) {
    if (narrow.value || !open.value || event.button !== 0 || !event.isPrimary) return;
    finish();
    event.preventDefault();
    event.currentTarget.focus();
    drag = { id: event.pointerId, target: event.currentTarget, x: event.clientX, width: width.value };
    event.currentTarget.setPointerCapture(event.pointerId);
    resizing.value = true;
  }
  function move(event) {
    if (drag?.id === event.pointerId) setWidth(drag.width + event.clientX - drag.x);
  }
  function end(event) {
    if (drag?.id === event.pointerId) finish();
  }
  function adjust(delta) {
    setWidth(width.value + delta);
    save();
  }
  function reset() {
    setWidth(DEFAULT_WIDTH);
    save();
  }
  function keydown(event) {
    if (event.key === 'Escape') { finish(); return; }
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Home') setWidth(MIN_WIDTH);
    else if (event.key === 'End') setWidth(maximum.value);
    else setWidth(width.value + (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 32 : 8));
    save();
  }

  watch([narrow, open], finish);
  onMounted(() => {
    if (persist) {
      try {
        const stored = Number(localStorage.getItem(STORAGE_KEY));
        if (Number.isFinite(stored) && stored >= MIN_WIDTH && stored <= MAX_WIDTH) preferredWidth.value = stored;
      } catch { /* 使用默认宽度。 */ }
    }
    const measure = () => {
      if (layout.value) availableWidth.value = layout.value.clientWidth - 406;
    };
    measure();
    observer = new ResizeObserver(measure);
    if (layout.value) observer.observe(layout.value);
  });
  onUnmounted(() => { finish(); observer?.disconnect(); });

  return { width, maximum, resizing, start, move, end, keydown, adjust, reset, minimum: MIN_WIDTH };
}
