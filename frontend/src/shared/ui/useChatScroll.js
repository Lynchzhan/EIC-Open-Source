import { nextTick, onUnmounted, ref, watch } from 'vue';

// 滚动状态归属当前页面；会话切换或卸载后，旧帧不能改写新的阅读位置。
export function useChatScroll({ container, conversationId, isActiveConversation, onNearTop }) {
  const followLatest = ref(true);
  const hasUnreadUpdates = ref(false);
  let pendingFrame = null;

  function cancelPendingFrame() {
    if (pendingFrame !== null) cancelAnimationFrame(pendingFrame);
    pendingFrame = null;
  }
  watch(conversationId, cancelPendingFrame, { flush: 'sync' });

  function findTurn(id) {
    return [...(container.value?.querySelectorAll('[id^="chat-turn-"]') || [])]
      .find(element => element.id === `chat-turn-${id}`);
  }

  function scrollToBottom() {
    if (!followLatest.value) { hasUnreadUpdates.value = true; return; }
    if (pendingFrame !== null) return;
    const currentId = conversationId.value;
    const currentContainer = container.value;
    pendingFrame = requestAnimationFrame(() => {
      pendingFrame = null;
      if (isActiveConversation(currentId) && followLatest.value && currentContainer === container.value && currentContainer) {
        currentContainer.scrollTop = currentContainer.scrollHeight;
      }
    });
  }

  function jumpToLatest() {
    followLatest.value = true;
    hasUnreadUpdates.value = false;
    nextTick(scrollToBottom);
  }

  function jumpToTurn(id) {
    const target = findTurn(id);
    if (!target || !container.value) return;
    followLatest.value = false;
    container.value.scrollTop += target.getBoundingClientRect().top - container.value.getBoundingClientRect().top;
  }

  function handleScroll() {
    if (!container.value) return;
    const { scrollTop, scrollHeight, clientHeight } = container.value;
    followLatest.value = scrollHeight - scrollTop - clientHeight < 48;
    if (followLatest.value) hasUnreadUpdates.value = false;
    if (scrollTop < 50) onNearTop();
  }

  function captureReadingPosition() {
    const current = container.value;
    if (!current || followLatest.value) return null;
    const top = current.getBoundingClientRect().top;
    const anchor = [...current.querySelectorAll('[id^="chat-turn-"]')]
      .find(element => element.getBoundingClientRect().bottom > top);
    return { container: current, scrollTop: current.scrollTop, anchorId: anchor?.id,
      offset: anchor ? anchor.getBoundingClientRect().top - top : 0 };
  }

  function restoreReadingPosition(position, chatId) {
    if (!position || !isActiveConversation(chatId) || followLatest.value || container.value !== position.container) return;
    const current = position.container;
    const anchor = [...current.querySelectorAll('[id^="chat-turn-"]')].find(element => element.id === position.anchorId);
    if (anchor) current.scrollTop += anchor.getBoundingClientRect().top - current.getBoundingClientRect().top - position.offset;
    else current.scrollTop = position.scrollTop;
  }

  onUnmounted(cancelPendingFrame);

  return { followLatest, hasUnreadUpdates, scrollToBottom, jumpToLatest, jumpToTurn,
    handleScroll, captureReadingPosition, restoreReadingPosition };
}
