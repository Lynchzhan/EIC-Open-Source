// 语音识别、通话与播报归属当前聊天页面实例。
import { ref, computed, nextTick, watch, onUnmounted } from 'vue';
import { createVoiceWorkScope } from './voiceWorkScope';
import { getAliAsrToken } from '../../api/ai/aliasr.js';
import { AliAsrAdapter } from '../../utils/aliAsrAdapter.js';
import { sanitizeRichHtml } from '../../utils/safeHtml.js';
import { message, warning } from '../../app/ui/feedback';

export function useChatSpeech({ messages, isSending, activeConversation, newMessage, lastSentMessage, scrollToBottom, sendMessage, stopMessage }) {
  const workScope = createVoiceWorkScope();
  const { isCurrent, scheduleTimeout } = workScope;
const isVoiceMode = ref(false); // 语音模式开关
const showTextInVoiceMode = ref(false); // 语音模式下是否显示文字（转文本模式）
const isRecording = ref(false); // 是否正在录音
const isRecognizing = ref(false); // 是否正在识别（延迟停止中）
const micPermissionGranted = ref(false); // 麦克风权限是否已获取
const recognizedPreviewText = ref(''); // 语音识别预览文字
const voiceSentHint = ref(''); // 语音发送后提示
let recordingStartTime = 0; // 录音开始时间戳
const pendingStopTimer = ref(null); // 延迟停止识别的定时器
const recognition = ref(null); // 语音识别实例

// 阿里云实时 ASR 配置
const aliAsrAppKey = ref(''); // 阿里云语音识别 AppKey（从后端获取）
const aliAsrToken = ref(''); // 阿里云临时 Token
const aliAsrTokenExpiry = ref(0); // Token 过期时间戳
const aliAsrFailed = ref(false); // 阿里云 ASR 连接失败后，不再重试，直接走浏览器兜底

// 当前使用的 ASR 引擎（显示用）
const asrProviderLabel = computed(() => {
  if (aliAsrFailed.value) return '浏览器(阿里云降级)';
  return aliAsrToken.value && aliAsrAppKey.value ? '阿里云 ASR' : '浏览器';
});
const isSpeaking = ref(false); // 是否正在播放语音
const currentSpeakingMsgId = ref(null); // 当前正在播放的消息ID
const voiceSpeed = ref(1.0); // 语音播放倍速
const speedOptions = [
  { label: '1x', value: 1.0 },
  { label: '1.5x', value: 1.5 },
  { label: '2x', value: 2.0 },
  { label: '2.5x', value: 2.5 }, 
];

// 设置播放倍速
const setVoiceSpeed = (speed) => {
  voiceSpeed.value = speed;
};

// ========== 电话模式 ==========
const isPhoneMode = ref(false); // 电话模式开关
const phoneModeCollapsed = ref(false); // 电话模式全屏收起标志（收起后后台保持聆听，仅隐藏全屏覆盖层）
const phoneSubtitleEnabled = ref(false); // 字幕开关
const phoneCallStartMsgId = ref(null); // 进入电话模式时最后一条消息的 ID，用于筛选本次通话的消息
// 电话模式字幕：直接复用 messages，与聊天记录保持一致
const phoneMessages = computed(() => {
  if (!phoneCallStartMsgId.value) return messages.value;
  const startIdx = messages.value.findIndex(m => m.id === phoneCallStartMsgId.value);
  return startIdx >= 0 ? messages.value.slice(startIdx + 1) : messages.value;
});
const phoneSubtitlePanel = ref(null); // 字幕面板 DOM 引用
const previousVoiceMode = ref(false); // 进入电话模式前 isVoiceMode 的原值
const phoneCallDuration = ref(0); // 通话时长（秒）
let phoneCallTimer = null; // 通话计时句柄
const PHONE_SILENCE_THRESHOLD = 3000; // 静默3秒后自动发送
let phoneSilenceTimer = null; // 静默检测定时器
let phoneLastSpeechTime = 0; // 最后一次检测到语音的时间

// 通话状态文字（根据录音/识别/发送/播放状态自动计算）
const phoneStatusText = computed(() => {
  if (isRecording.value) return '正在聆听...';
  if (isRecognizing.value) return '识别中...';
  if (isSending.value && !isSpeaking.value) return 'AI 思考中...';
  if (isSpeaking.value) return '正在回复...';
  return '通话中';
});

// ========== 语音功能 ==========

// onresult 节流机制：Chrome interimResults 每 50-100ms 触发一次 onresult，
// 直接更新 Vue ref 会导致大量 DOM 重渲染 → 用户感知卡顿。
// 使用 requestAnimationFrame 合并多次 onresult 的更新，每帧最多触发一次 DOM 更新。
let previewTextBuffer = '';      // 缓存最新的识别文本
let previewTextRafId = null;     // requestAnimationFrame 句柄
let lastPreviewText = '';        // 上一次已渲染的文本（用于对比变化）

const flushPreviewText = () => {
  previewTextRafId = null;
  if (previewTextBuffer !== lastPreviewText) {
    lastPreviewText = previewTextBuffer;
    recognizedPreviewText.value = previewTextBuffer;
  }
};

const updatePreviewText = (text) => {
  if (text === previewTextBuffer) return; // 文本未变化，跳过
  previewTextBuffer = text;
  if (!previewTextRafId) {
    previewTextRafId = requestAnimationFrame(flushPreviewText);
  }
};

const resetPreviewTextBuffer = () => {
  previewTextBuffer = '';
  lastPreviewText = '';
  if (previewTextRafId) {
    cancelAnimationFrame(previewTextRafId);
    previewTextRafId = null;
  }
  recognizedPreviewText.value = '';
};

// 获取阿里云 ASR Token（过期自动刷新）
const fetchAliAsrToken = async () => {
  const current = workScope.capture();
  const now = Date.now();
  if (aliAsrToken.value && now < aliAsrTokenExpiry.value - 60000) {
    return; // Token 还有效（提前 1 分钟刷新）
  }
  try {
    const res = await getAliAsrToken();
    if (!isCurrent(current)) return;
    if (res.code === 200 && res.data) {
      aliAsrAppKey.value = res.data.appKey || '';
      aliAsrToken.value = res.data.token || '';
      // 使用后端返回的过期时间，兜底 24 小时
      aliAsrTokenExpiry.value = res.data.expireTime
        ? Number(res.data.expireTime) * 1000
        : now + 23 * 60 * 60 * 1000;
    }
  } catch (err) {
    if (!isCurrent(current)) return;
    console.error('获取阿里云 ASR Token 失败:', err);
  }
};

// 初始化语音识别（阿里云实时 ASR，兜底浏览器 SpeechRecognition）
const initSpeechRecognition = () => {
  // 优先使用阿里云实时 ASR，如果已失败则跳过
  if (!aliAsrFailed.value && aliAsrToken.value && aliAsrAppKey.value) {
    return new AliAsrAdapter({
      appKey: aliAsrAppKey.value,
      token: aliAsrToken.value,
    });
  }
  // 兜底：浏览器原生 SpeechRecognition
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    message.warning('您的浏览器不支持语音识别，请使用 Chrome 浏览器');
    return null;
  }
  if (!window.isSecureContext) {
    message.error('语音识别需要 HTTPS 环境或 localhost 访问');
    return null;
  }
  const rec = new SpeechRecognition();
  rec.lang = 'zh-CN';
  rec.interimResults = true;
  rec.continuous = true;
  rec.maxAlternatives = 1;
  return rec;
};

// 检查/申请麦克风权限
const ensureMicrophonePermission = async () => {
  const current = workScope.capture();
  if (workScope.disposed) return false;
  // 优先用 Permissions API 查询当前状态
  if (navigator.permissions) {
    try {
      const status = await navigator.permissions.query({ name: 'microphone' });
      if (!isCurrent(current)) return false;
      if (status.state === 'granted') {
        micPermissionGranted.value = true;
        return true;
      }
      if (status.state === 'denied') {
        micPermissionGranted.value = false;
        // 权限已被拒绝，浏览器不会再弹窗，需要引导用户手动开启
        warning({
          title: '麦克风权限被拒绝',
          content: '浏览器已记住您之前的拒绝选择，需要手动重新开启。请点击浏览器地址栏左侧的锁形图标（或设置图标），找到"麦克风"权限并改为"允许"，然后刷新页面重试。',
          okText: '我知道了',
        });
        return false;
      }
      // state === 'prompt': 可以通过 getUserMedia 触发权限弹窗
    } catch (e) {
      // Permissions API 不支持 microphone，降级到 getUserMedia
    }
  }
  // 通过 getUserMedia 触发权限申请弹窗
  if (!isCurrent(current)) return false;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    // 申请成功，立即释放资源（只是用来触发权限）
    stream.getTracks().forEach((t) => t.stop());
    if (!isCurrent(current)) return false;
    micPermissionGranted.value = true;
    return true;
  } catch (err) {
    if (!isCurrent(current)) return false;
    micPermissionGranted.value = false;
    if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
      warning({
        title: '麦克风权限被拒绝',
        content: '您拒绝了麦克风权限。如需重新开启，请点击浏览器地址栏左侧的锁形图标，找到"麦克风"权限并改为"允许"，然后刷新页面重试。',
        okText: '我知道了',
      });
    } else if (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError') {
      message.error('未检测到麦克风设备');
    } else {
      message.error('麦克风访问失败: ' + err.message);
    }
    return false;
  }
};

// 开始录音
const startRecording = (e) => {
  if (workScope.disposed || !isVoiceMode.value || isSending.value) return;

  // 检查麦克风权限是否已获取
  if (!micPermissionGranted.value) {
    message.warning('正在申请麦克风权限，请稍后再试');
    // 异步触发权限申请
    ensureMicrophonePermission();
    return;
  }

  // 在用户手势中解锁语音合成（为后续AI回复自动播放做准备）
  unlockSpeechSynthesis();

  // 开始录音时，停止正在播放的AI语音
  if (isSpeaking.value) {
    staticPlaybackGen.value++; // 使旧回调失效
    streamingTtsActive.value = false;
    staticPlaybackMsgId.value = null;
    staticSentenceList.value = [];
    staticSentenceIndex.value = 0;
    window.speechSynthesis.cancel();
    resetSpeakingState();
  }

  // 每次创建新的识别实例（stop/abort 后旧实例无法复用）
  // 先清理上一轮残留实例，避免旧实例占用语音服务/回调误触发导致识别失效
  if (recognition.value) {
    const old = recognition.value;
    try { old.onend = null; old.onerror = null; old.onresult = null; old.abort(); } catch (e) { /* ignore */ }
  }
  recognition.value = initSpeechRecognition();
  if (!recognition.value) return;
  const rec = recognition.value;
  const current = workScope.capture();

  isRecording.value = true;
  isRecognizing.value = false;
  resetPreviewTextBuffer();
  newMessage.value = '';
  recordingStartTime = Date.now();

  recognition.value.onresult = (event) => {
    if (!isCurrent(current) || recognition.value !== rec) return;
    let finalText = '';
    let interimText = '';
    for (let i = 0; i < event.results.length; i++) {
      const result = event.results[i];
      if (result.isFinal) {
        finalText += result[0].transcript;
      } else {
        interimText = result[0].transcript;
      }
    }
    updatePreviewText(finalText + interimText);
  };

  recognition.value.onerror = (event) => {
    if (!isCurrent(current) || recognition.value !== rec) return;
    console.error('语音识别错误:', event.error);
    if (event.error === 'aborted' || event.error === 'no-speech') {
      return;
    }
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      warning({
        title: '麦克风权限被拒绝',
        content: '请点击浏览器地址栏左侧的锁形图标，找到"麦克风"权限并改为"允许"，然后刷新页面重试。',
        okText: '我知道了',
      });
    } else {
      message.error('语音识别失败: ' + event.error);
    }
    isRecording.value = false;
    isRecognizing.value = false;
  };

  recognition.value.onend = () => {
    if (!isCurrent(current) || recognition.value !== rec) return;
    // 如果不是我们主动 stop 的，才重置 isRecording
    if (isRecording.value) {
      isRecording.value = false;
    }
  };

  try {
    recognition.value.start();
    console.log('语音识别已启动');
  } catch (err) {
    console.error('启动语音识别失败:', err);
    isRecording.value = false;
    isRecognizing.value = false;
  }
};

// 停止录音并发送
const stopRecording = () => {
  if (!isRecording.value) return;
  
  // 检查录音时长（最短 300ms，防止误触）
  const duration = Date.now() - recordingStartTime;
  if (duration < 300) {
    // 录音时间太短，直接取消
    isRecording.value = false;
    if (recognition.value) {
      try { recognition.value.abort(); } catch (e) { /* ignore */ }
    }
    resetPreviewTextBuffer();
    voiceSentHint.value = '说话时间太短';
    scheduleTimeout(() => { voiceSentHint.value = ''; }, PHONE_SILENCE_THRESHOLD);
    return;
  }
  
  // 正常流程：进入识别状态，延迟 500ms 让识别引擎处理最后的语音
  isRecording.value = false;
  isRecognizing.value = true;
  
  pendingStopTimer.value = scheduleTimeout(() => {
    isRecognizing.value = false;
    pendingStopTimer.value = null;
    
    // 停止识别引擎
    if (recognition.value) {
      try { recognition.value.stop(); } catch (e) { /* ignore */ }
    }
    
    // 等 onend 事件处理完最后一批结果后，发送消息
    // 再等一个小延迟确保最后的 final result 已到达
    scheduleTimeout(() => {
      // 立即刷新 buffer，确保读取到最新的识别文本
      if (previewTextRafId) {
        cancelAnimationFrame(previewTextRafId);
        previewTextRafId = null;
      }
      flushPreviewText();
      const text = recognizedPreviewText.value.trim();
      if (text) {
        newMessage.value = text;
        resetPreviewTextBuffer();
        voiceSentHint.value = '已发送 ✓';
        scheduleTimeout(() => { voiceSentHint.value = ''; }, PHONE_SILENCE_THRESHOLD);
        sendMessage();
      } else {
        resetPreviewTextBuffer();
        voiceSentHint.value = '未识别到内容，已取消';
        scheduleTimeout(() => { voiceSentHint.value = ''; }, PHONE_SILENCE_THRESHOLD);
      }
    }, 300);
  }, 500);
};

// 取消录音
const cancelRecording = () => {
  if (!isRecording.value && !isRecognizing.value) return;
  isRecording.value = false;
  isRecognizing.value = false;
  // 取消待执行的停止定时器
  if (pendingStopTimer.value) {
    clearTimeout(pendingStopTimer.value);
    pendingStopTimer.value = null;
  }
  newMessage.value = '';
  resetPreviewTextBuffer();
  voiceSentHint.value = '已取消';
  scheduleTimeout(() => { voiceSentHint.value = ''; }, 1000);
  if (recognition.value) {
    try {
      recognition.value.abort();
    } catch (e) {
      // ignore
    }
  }
};

// ========== 电话模式功能 ==========

// 重置电话模式的播放/识别/计时状态（不切换 isPhoneMode / isVoiceMode）
// 供 hangUpPhone 退出时调用，以及 enterPhoneMode 在"已在电话模式中再点电话"时复用
const resetPhoneCallState = () => {
  workScope.invalidate();
  pendingStopTimer.value = null;
  stopPhoneAutoListen();
  if (isSending.value) {
    stopMessage();
  }
  // 停止 TTS 播放
  staticPlaybackGen.value++;
  window.speechSynthesis.cancel();
  streamingTtsActive.value = false;
  streamingTtsPlayedLength.value = 0;
  staticPlaybackMsgId.value = null;
  staticSentenceList.value = [];
  staticSentenceIndex.value = 0;
  resetSpeakingState();
  userStoppedSending.value = false;
  stopPhoneCallTimer();
};

// 进入电话模式
const enterPhoneMode = async () => {
  if (workScope.disposed) return;
  if (!activeConversation.value) {
    message.warning('请先选择一个对话');
    return;
  }
  if (isPhoneMode.value) {
    // 已在电话模式中（例如收起后切换了对话再点电话）：先彻底重置识别/TTS/计时，再为当前对话重新发起
    resetPhoneCallState();
  } else {
    // 首次进入：记住之前的语音模式状态
    previousVoiceMode.value = isVoiceMode.value;
    // 启用语音模式，复用全部 TTS/录音/自动播放逻辑
    // watch(isVoiceMode) 会自动解锁语音合成 + 申请麦克风权限
    isVoiceMode.value = true;
    isPhoneMode.value = true;
  }
  phoneModeCollapsed.value = false;
  phoneCallDuration.value = 0;
  phoneCallStartMsgId.value = messages.value.length > 0 ? messages.value[messages.value.length - 1].id : null;
  phoneSubtitleEnabled.value = false;
  startPhoneCallTimer();
  scheduleTimeout(() => {
    if (isPhoneMode.value) {
      startPhoneAutoListen();
    }
  }, 1000);
};

// 通话计时开始
const startPhoneCallTimer = () => {
  stopPhoneCallTimer();
  phoneCallTimer = setInterval(() => {
    phoneCallDuration.value++;
  }, 1000);
};

// 通话计时停止
const stopPhoneCallTimer = () => {
  if (phoneCallTimer) {
    clearInterval(phoneCallTimer);
    phoneCallTimer = null;
  }
};

// 格式化通话时长
const formatPhoneCallDuration = (seconds) => {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
};

// 电话模式：启动自动监听（实时对话，无需按住）
const startPhoneAutoListen = () => {
  // 播放中不监听，播放结束后由 watch(isSpeaking) 重新启动
  if (workScope.disposed || !isPhoneMode.value) return;
  if (isRecording.value) return; // 已在监听中，防止重复启动
  if (!micPermissionGranted.value) {
    // 等待权限获取后重试
    scheduleTimeout(() => {
      if (isPhoneMode.value) startPhoneAutoListen();
    }, 1000);
    return;
  }

  unlockSpeechSynthesis();

  // 清理上一轮残留的识别实例：解除回调并 abort，避免旧实例 onend 误重启新实例、
  // 或旧实例占用语音服务导致几轮后识别失效（Chrome Web Speech 多实例互相干扰）
  if (recognition.value) {
    const old = recognition.value;
    try { old.onend = null; old.onerror = null; old.onresult = null; old.abort(); } catch (e) { /* ignore */ }
  }

  const rec = initSpeechRecognition();
  if (!rec) return;
  recognition.value = rec;
  const current = workScope.capture();

  isRecording.value = true;
  isRecognizing.value = false;
  resetPreviewTextBuffer();
  phoneLastSpeechTime = Date.now();

  rec.onresult = (event) => {
    if (!isCurrent(current) || recognition.value !== rec) return;
    phoneLastSpeechTime = Date.now();

    // 从完整的 results 数组重建文本（兼容原生 SpeechRecognition 累积和 AliAsrAdapter 替换两种模式）
    let finalText = '';
    let interimText = '';
    for (let i = 0; i < event.results.length; i++) {
      const result = event.results[i];
      if (result.isFinal) {
        finalText += result[0].transcript;
      } else {
        interimText = result[0].transcript;
      }
    }
    const fullText = finalText + interimText;
    updatePreviewText(fullText);

    // 检测到句末标点（。！？.!?）的 final 结果时立即发送，不等静默超时
    // 使用 fullText（累积全部文本）而非 lastResult 单句，因为长句可能被 VAD 切分，
    // 最后一句不带标点但整体文本末尾有标点
    const lastResult = event.results[event.results.length - 1];
    if (lastResult && lastResult.isFinal && /[。！？.!?]\s*$/.test(fullText)) {
      // 立即刷新 buffer，确保 recognizedPreviewText 是最新的
      if (previewTextRafId) {
        cancelAnimationFrame(previewTextRafId);
        previewTextRafId = null;
      }
      flushPreviewText();
      // 防重复：避免在通话中、TTS 播放中状态下重复触发
      if (!isPhoneMode.value || isSpeaking.value || window.speechSynthesis.speaking) return;
      const text = recognizedPreviewText.value.trim();
      if (text) {
        sendPhoneMessage();
      }
    }
  };

  rec.onerror = (event) => {
    if (!isCurrent(current) || recognition.value !== rec) return;
    console.error('语音识别错误:', event.error);
    if (event.error === 'aborted' || event.error === 'no-speech') return;
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      message.error('麦克风权限被拒绝');
    } else {
      message.error('语音识别失败: ' + (event.error || '未知错误'));
    }
    isRecording.value = false;
    // 电话模式下识别意外失败，自动重建实例恢复监听
    if (isPhoneMode.value) {
      scheduleTimeout(() => {
        if (isPhoneMode.value) startPhoneAutoListen();
      }, 300);
    }
  };

  rec.onend = () => {
    // 旧实例（已被新实例取代）的 onend：直接返回
    if (!isCurrent(current) || recognition.value !== rec) return;
    // 电话模式下自动重启识别（浏览器可能自动停止）
    if (isPhoneMode.value && isRecording.value) {
      // 不调用 rec.start() —— AliAsrAdapter._fireEnd() 已把 this.onend 置 null，
      // 复用旧实例会导致新 ASR 的错误/结束事件无法上报，识别静默死亡。
      isRecording.value = false;
      startPhoneAutoListen();
    } else {
      isRecording.value = false;
    }
  };

  try {
    rec.start();
  } catch (err) {
    console.error('启动语音识别失败:', err);
    isRecording.value = false;
    scheduleTimeout(() => {
      if (isPhoneMode.value && !isSpeaking.value) {
        startPhoneAutoListen();
      }
    }, 500);
  }

  // 启动静默检测：用户停说话1.8秒后自动发送（TTS 播放中不发送）
  stopPhoneSilenceTimer();
  phoneSilenceTimer = setInterval(() => {
    if (!isPhoneMode.value) {
      stopPhoneSilenceTimer();
      return;
    }
    // TTS 播放中不发送消息（播放时录音已关闭，此检查作为安全兜底）
    if (isSpeaking.value || window.speechSynthesis.speaking || window.speechSynthesis.pending) {
      return;
    }
    const silenceDuration = Date.now() - phoneLastSpeechTime;
    // 立即刷新 buffer，确保检测到最新的识别文本
    if (previewTextRafId) {
      cancelAnimationFrame(previewTextRafId);
      previewTextRafId = null;
    }
    flushPreviewText();
    if (silenceDuration > PHONE_SILENCE_THRESHOLD && recognizedPreviewText.value.trim()) {
      sendPhoneMessage();
    }
  }, 300);
};

// 停止静默检测定时器
const stopPhoneSilenceTimer = () => {
  if (phoneSilenceTimer) {
    clearInterval(phoneSilenceTimer);
    phoneSilenceTimer = null;
  }
};

// 停止电话模式自动监听
const stopPhoneAutoListen = () => {
  stopPhoneSilenceTimer();
  isRecording.value = false;
  isRecognizing.value = false;
  if (recognition.value) {
    try { recognition.value.abort(); } catch (e) { /* ignore */ }
  }
  resetPreviewTextBuffer();
};

// 电话模式：静默触发后自动发送消息
const sendPhoneMessage = () => {
  if (workScope.disposed || !isPhoneMode.value) return;
  if (previewTextRafId) {
    cancelAnimationFrame(previewTextRafId);
    previewTextRafId = null;
  }
  flushPreviewText();
  let text = recognizedPreviewText.value.trim();

  // 如果正在发送中，中断上一次并把新旧文本合并一起发送
  if (isSending.value) {
    const prevText = lastSentMessage.value || '';
    stopMessage();
    text = (prevText + text).trim();
  }

  if (phoneSubtitleEnabled.value) {
    // 字幕模式：不停止识别，重置缓冲区让新的识别结果重新累积，实现连续聆听
    if (recognition.value && recognition.value.resetBuffer) {
      recognition.value.resetBuffer();
    }
    resetPreviewTextBuffer();
    phoneLastSpeechTime = Date.now(); // 重置静默计时，避免立即触发再次发送
    // 重新启动静默检测，监听下一段语音
    stopPhoneSilenceTimer();
    phoneSilenceTimer = setInterval(() => {
      if (!isPhoneMode.value) {
        stopPhoneSilenceTimer();
        return;
      }
      if (isSpeaking.value || window.speechSynthesis.speaking || window.speechSynthesis.pending) {
        return;
      }
      const silenceDuration = Date.now() - phoneLastSpeechTime;
      if (previewTextRafId) {
        cancelAnimationFrame(previewTextRafId);
        previewTextRafId = null;
      }
      flushPreviewText();
      if (silenceDuration > PHONE_SILENCE_THRESHOLD && recognizedPreviewText.value.trim()) {
        sendPhoneMessage();
      }
    }, 300);
  } else {
    // 非字幕模式：停止识别，等 AI 回复播报完后再恢复
    stopPhoneSilenceTimer();
    isRecording.value = false;
    if (recognition.value) {
      try { recognition.value.stop(); } catch (e) { /* ignore */ }
    }
    resetPreviewTextBuffer();
  }

  if (!text) {
    if (!phoneSubtitleEnabled.value) {
      scheduleTimeout(() => {
        if (isPhoneMode.value) startPhoneAutoListen();
      }, 300);
    }
    return;
  }

  // 正常发送
  newMessage.value = text;
  sendMessage();
  // 字幕模式下识别已在运行，无需恢复；非字幕模式等 watch(isSpeaking) 播完恢复
};

// 挂断电话
const hangUpPhone = () => {
  resetPhoneCallState();
  // 退出电话模式
  isPhoneMode.value = false;
  phoneModeCollapsed.value = false;
  // 恢复之前的语音模式状态
  isVoiceMode.value = previousVoiceMode.value;
};

// 播放AI语音（TTS）
// 清理文本用于语音播放：去除表情、特殊符号、标点、Markdown语法等
const sanitizeTextForTTS = (text) => {
  if (!text) return '';
  let t = text;
  // 去除表情符号（emoji）
  t = t.replace(/[\u{1F300}-\u{1F9FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{1F600}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}\u{200D}\u{FE00}-\u{FE0F}]/gu, ' ');
  // 去除Markdown代码块 ```...```
  t = t.replace(/```[\s\S]*?```/g, ' ');
  // 去除行内代码 `...`
  t = t.replace(/`[^`]*`/g, ' ');
  // 去除Markdown链接 [text](url) → text
  t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
  // 去除Markdown图片 ![alt](url)
  t = t.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ');
  // 去除Markdown加粗/斜体标记 **text** *text*
  t = t.replace(/\*\*([^*]+)\*\*/g, '$1');
  t = t.replace(/\*([^*]+)\*/g, '$1');
  t = t.replace(/__([^_]+)__/g, '$1');
  t = t.replace(/_([^_]+)_/g, '$1');
  // 去除Markdown标题 # 
  t = t.replace(/^#{1,6}\s+/gm, '');
  // 去除URL
  t = t.replace(/https?:\/\/[^\s]+/g, ' ');
  // 去除中文标点符号
  t = t.replace(/[，。！？、；：""''（）【】《》〈〉…—·！]/g, ' ');
  // 去除英文标点符号
  t = t.replace(/[,.!?;:"'()\[\]{}<>]/g, ' ');
  // 去除多余空白
  t = t.replace(/\s+/g, ' ').trim();
  return t;
};

// 获取最自然的中文语音（普通话，排除粤语/台湾音）
let cachedBestVoice = null;
let speechUnlocked = false; // 语音合成是否已解锁（浏览器自动播放策略）

// 在用户手势中解锁语音合成（解决首次自动播放被阻止的问题）
const unlockSpeechSynthesis = () => {
  if (speechUnlocked) return;
  try {
    const unlocker = new SpeechSynthesisUtterance('');
    unlocker.volume = 0;
    unlocker.lang = 'cmn-Hans-CN';
    window.speechSynthesis.speak(unlocker);
    window.speechSynthesis.cancel();
    speechUnlocked = true;
    console.log('语音合成已解锁');
  } catch (e) {
    // ignore
  }
};
const getBestChineseVoice = () => {
  if (cachedBestVoice) return cachedBestVoice;
  const voices = window.speechSynthesis.getVoices();

  // 打印可用语音用于调试
  console.log('[TTS] 可用语音列表:');
  voices.forEach((v) => {
    console.log(`  - "${v.name}" lang=${v.lang} local=${v.localService}`);
  });

  // 只选择普通话语音（严格排除粤语/台湾/繁体）
  const mandarinVoices = voices.filter((v) => {
    if (!v.lang) return false;
    const lang = v.lang.toLowerCase();
    // 排除所有粤语和台湾音变体
    if (lang.includes('hk') || lang.includes('tw') || lang.includes('yue') || lang.includes('hant')) return false;
    // 必须是普通话：zh-CN 或 cmn-Hans-CN
    if (lang === 'zh-cn' || lang === 'cmn-hans-cn') return true;
    // 其他 zh-* 只在明确排除了粤语的情况下才接受
    return lang.startsWith('zh') && !lang.includes('hk') && !lang.includes('tw');
  });

  console.log('[TTS] 筛选后普通话语音:', mandarinVoices.map(v => `${v.name} (${v.lang})`));

  if (mandarinVoices.length === 0) {
    console.warn('[TTS] 未找到任何普通话语音！将使用 utterance.lang 让浏览器自动选择');
    cachedBestVoice = null;
    return null;
  }

  // 排除 Preview/Experimental 等实验性语音
  const stableVoices = mandarinVoices.filter(
    (v) => !v.name.toLowerCase().includes('preview') && !v.name.toLowerCase().includes('experimental')
  );

  // 优先选择本地服务的语音（响应更快）
  const localVoices = stableVoices.filter((v) => v.localService);

  // 普通话女声优先级关键词
  const preferredKeywords = [
    'yaoyao', 'yaoyao-neural', 'huihui', 'xiaoxiao', 'xiaoyi',
    'xiaomeng', 'xiaorou', 'yunxi', 'yunjian', 'yunyang',
    'yunkang', 'yunxia', 'tingting'
  ];

  // 优先匹配含关键词的本地语音
  let best = localVoices.find((v) =>
    preferredKeywords.some((k) => v.name.toLowerCase().includes(k))
  );

  // 退而求其次：stable 语音中匹配关键词
  if (!best) {
    best = stableVoices.find((v) =>
      preferredKeywords.some((k) => v.name.toLowerCase().includes(k))
    );
  }

  // 再退而求其次：第一个非 Default 非 Preview 的
  if (!best && stableVoices.length > 0) {
    best = stableVoices.find((v) => !v.name.toLowerCase().includes('default')) || stableVoices[0];
  }

  // 最后兜底：第一个普通话语音
  if (!best) best = mandarinVoices[0];

  console.log('[TTS] 选中语音:', best ? `${best.name} (${best.lang})` : '无');
  cachedBestVoice = best;
  return best;
};

// 初始化时加载语音列表（某些浏览器需要异步加载）
const onVoicesChanged = () => {
    if (workScope.disposed) return;
    cachedBestVoice = null;
    getBestChineseVoice();
};
if ('speechSynthesis' in window) {
  window.speechSynthesis.addEventListener('voiceschanged', onVoicesChanged);
}

// ========== 非流式 TTS：逐句播放（支持实时倍速调整）==========

const staticSentenceList = ref([]); // 句子列表
const staticSentenceIndex = ref(0); // 下一句的索引
const staticPlaybackMsgId = ref(null); // 当前播放的消息ID
const staticPlaybackGen = ref(0); // 播换代次（递增使旧回调失效，防止竞争）

// 将文本分割成句子
const splitTextIntoSentences = (text) => {
  if (!text || !text.trim()) return [];
  const sentences = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    if (/[。！？.!?\n]/.test(text[i])) {
      const s = text.substring(start, i + 1).trim();
      if (s) sentences.push(s);
      start = i + 1;
    }
  }
  if (start < text.length) {
    const r = text.substring(start).trim();
    if (r) sentences.push(r);
  }
  return sentences.length > 0 ? sentences : [text.trim()];
};

// 播放下一句（非流式，每句读取最新倍速）
const playNextStaticSentence = (gen) => {
  if (gen !== staticPlaybackGen.value) return; // 被新播放取代
  if (!staticPlaybackMsgId.value || staticSentenceIndex.value >= staticSentenceList.value.length) {
    resetSpeakingState();
    staticPlaybackMsgId.value = null;
    staticSentenceList.value = [];
    staticSentenceIndex.value = 0;
    return;
  }

  const sentence = staticSentenceList.value[staticSentenceIndex.value];
  staticSentenceIndex.value++;

  const utterance = new SpeechSynthesisUtterance(sentence);
  utterance.lang = 'zh-CN';
  const bestVoice = getBestChineseVoice();
  if (bestVoice) {
    utterance.voice = bestVoice; // 有普通话语音时才设置
  }
  // 没有普通话语音时不设置 voice，浏览器会根据 lang='zh-CN' 自动选择
  utterance.pitch = 1.0;
  utterance.rate = voiceSpeed.value; // 每句读取最新倍速
  // 电话模式降低音量，减弱扬声器回声对麦克风识别的干扰（与 speakSentence 保持一致）
  utterance.volume = isPhoneMode.value ? 0.6 : 1.0;

  utterance.onstart = () => {
    if (gen !== staticPlaybackGen.value) return;
    isSpeaking.value = true;
  };
  utterance.onend = () => {
    if (gen !== staticPlaybackGen.value) return;
    playNextStaticSentence(gen);
  };
  utterance.onerror = () => {
    if (gen !== staticPlaybackGen.value) return;
    playNextStaticSentence(gen);
  };

  window.speechSynthesis.speak(utterance);
};

const playAIVoice = (message) => {
  if (workScope.disposed || !message || !message.content) return;

  // 如果正在播放同一条消息，则停止
  if (isSpeaking.value && currentSpeakingMsgId.value === message.id) {
    staticPlaybackGen.value++; // 递增代次，使旧回调失效
    window.speechSynthesis.cancel();
    streamingTtsActive.value = false;
    resetSpeakingState();
    staticPlaybackMsgId.value = null;
    staticSentenceList.value = [];
    staticSentenceIndex.value = 0;
    return;
  }

  // 停止之前的播放（包括流式播放）
  staticPlaybackGen.value++; // 递增代次，使旧回调失效
  window.speechSynthesis.cancel();
  streamingTtsActive.value = false;
  resetSpeakingState();

  // 去除HTML标签获取纯文本
  const tempDiv = document.createElement('template');
  tempDiv.innerHTML = sanitizeRichHtml(message.content);
  const plainText = (tempDiv.content.textContent || '').trim();
  if (!plainText) return;

  // 清理文本：去除表情、标点、特殊符号等，只保留纯文字
  const cleanText = sanitizeTextForTTS(plainText);
  if (!cleanText) return;

  // 分割成句子，逐句播放（支持实时倍速调整）
  staticSentenceList.value = splitTextIntoSentences(cleanText);
  staticSentenceIndex.value = 0;
  staticPlaybackMsgId.value = message.id;

  const gen = staticPlaybackGen.value;
  currentSpeakingMsgId.value = message.id;
  isSpeaking.value = true;

  playNextStaticSentence(gen);
  startSpeakingCheck();
};

// 倍速变化时实时调整：取消当前句子，从当前句重新开始（新倍速）
watch(voiceSpeed, () => {
  // 非流式播放中：立即从当前句子重新播放（新倍速）
  if (isSpeaking.value && staticPlaybackMsgId.value && !streamingTtsActive.value) {
    staticPlaybackGen.value++; // 递增代次，使旧 onend 失效
    staticSentenceIndex.value = Math.max(0, staticSentenceIndex.value - 1); // 回退到当前句
    window.speechSynthesis.cancel();
    const gen = staticPlaybackGen.value;
    scheduleTimeout(() => {
      if (gen === staticPlaybackGen.value && staticPlaybackMsgId.value) {
        playNextStaticSentence(gen);
      }
    }, 50);
  }
  // 流式播放中：当前句子播完后，下一句自动使用新倍速（speakSentence 每次读取 voiceSpeed.value）
});

// ========== 流式 TTS：边接收边播放 ==========

const streamingTtsPlayedLength = ref(0); // 已送入 TTS 的文本长度
const streamingTtsActive = ref(false); // 流式 TTS 是否激活
let ttsUtteranceCount = 0; // 当前排队的 utterance 数量
let streamingSentenceQueue = []; // 流式待播放句子队列：上一句播完后才播下一句，避免 Chrome speechSynthesis 队列堆积导致卡顿
let streamingIsPlaying = false; // 流式是否有句子正在播放
let lastStreamSpeakTime = 0; // 上次调用 speechSynthesis.speak() 的时间戳，用于轮询兜底的宽限期
let speakingCheckTimer = null; // 轮询定时器：检测 TTS 是否已停止
const userStoppedSending = ref(false); // 用户主动停止发送，阻止后续自动播放

// 重置播放状态（统一出口）
const resetSpeakingState = () => {
  isSpeaking.value = false;
  currentSpeakingMsgId.value = null;
  ttsUtteranceCount = 0;
  streamingSentenceQueue = [];
  streamingIsPlaying = false;
  lastStreamSpeakTime = 0;
  stopSpeakingCheck();
};

// 启动轮询：检测 speechSynthesis 是否已实际停止（onend 不可靠的兜底方案）
const startSpeakingCheck = () => {
  stopSpeakingCheck();
  speakingCheckTimer = setInterval(() => {
    // 顺序播放队列兜底：Chrome utterance.onend 在移动端经常不触发，
    // 导致队列卡住（播完一句就停）。检测到当前句子已播完但 onend 没触发时，手动推进。
    // 加 500ms 宽限期避免与 speak() 调用竞态（speak 后 pending 可能未立即置 true）
    // 注意：不检查 streamingTtsActive，因为 finishStreamingTTS 后 streamingTtsActive=false 但队列可能还有剩余句子
    if (streamingIsPlaying && !window.speechSynthesis.speaking && !window.speechSynthesis.pending && (Date.now() - lastStreamSpeakTime > 500)) {
      streamingIsPlaying = false;
      playNextStreamingSentence();
      return;
    }
    // 安全网：队列有积压但没在播放（speakSentence 入队后 playNextStreamingSentence 未触发）
    if (!streamingIsPlaying && streamingSentenceQueue.length > 0) {
      playNextStreamingSentence();
      return;
    }
    // speechSynthesis 既不在播放也没有排队，但 isSpeaking 仍为 true → 状态卡住了
    if (isSpeaking.value && !window.speechSynthesis.speaking && !window.speechSynthesis.pending && !streamingTtsActive.value) {
      resetSpeakingState();
      return;
    }
    // 兜底：speechSynthesis 正在播放但 isSpeaking 未置 true（移动端 utterance.onstart 偶发不触发）
    if (!isSpeaking.value && (window.speechSynthesis.speaking || window.speechSynthesis.pending)) {
      isSpeaking.value = true;
    }
    // Chrome 15秒暂停 bug：长文本会自动暂停，定期 resume 保持播放
    if (window.speechSynthesis.speaking && window.speechSynthesis.paused) {
      window.speechSynthesis.resume();
    }
  }, 300);
};

const stopSpeakingCheck = () => {
  if (speakingCheckTimer) {
    clearInterval(speakingCheckTimer);
    speakingCheckTimer = null;
  }
};

// 开始流式 TTS
const startStreamingTTS = () => {
  if (workScope.disposed || !isVoiceMode.value) return;
  staticPlaybackGen.value++;
  // 字幕模式下不播报语音，保持持续监听
  if (isPhoneMode.value && phoneSubtitleEnabled.value) return;
  // 播放时关闭录音，避免麦克风干扰播放
  if (isPhoneMode.value) {
    stopPhoneAutoListen();
  }
  window.speechSynthesis.cancel();
  streamingTtsPlayedLength.value = 0;
  streamingTtsActive.value = true;
  ttsUtteranceCount = 0;
  streamingSentenceQueue = [];
  streamingIsPlaying = false;
  lastStreamSpeakTime = 0;
  isSpeaking.value = false;
  currentSpeakingMsgId.value = null;
  phoneLastSpeechTime = 0;
  resetPreviewTextBuffer();
  startSpeakingCheck();
};

// 播放队列中的下一句（流式顺序播放：上一句 onend 后才 speak 下一句，避免 utterance 堆积卡顿）
const playNextStreamingSentence = () => {
  if (workScope.disposed) return;
  const current = workScope.capture();
  const generation = staticPlaybackGen.value;
  const valid = () => isCurrent(current) && generation === staticPlaybackGen.value;
  // 队列空：暂无可播放的句子
  if (streamingSentenceQueue.length === 0) {
    streamingIsPlaying = false;
    // 流式已结束且队列空 → 整段播放完成，重置状态
    if (!streamingTtsActive.value) {
      resetSpeakingState();
    }
    return;
  }
  streamingIsPlaying = true;
  const cleanText = streamingSentenceQueue.shift();

  const utterance = new SpeechSynthesisUtterance(cleanText);
  utterance.lang = 'zh-CN';
  const bestVoice = getBestChineseVoice();
  if (bestVoice) {
    utterance.voice = bestVoice; // 有普通话语音时才设置
  }
  // 没有普通话语音时不设置 voice，浏览器会根据 lang='zh-CN' 自动选择
  utterance.pitch = 1.0;
  utterance.rate = voiceSpeed.value; // 每句读取最新倍速
  utterance.volume = isPhoneMode.value ? 0.6 : 1.0;

  utterance.onstart = () => {
    if (!valid()) return;
    isSpeaking.value = true;
    startSpeakingCheck();
  };
  // 上一句播完才播下一句（顺序播放，不依赖 Chrome 队列）
  utterance.onend = () => {
    if (!valid()) return;
    playNextStreamingSentence();
  };
  utterance.onerror = () => {
    if (!valid()) return;
    playNextStreamingSentence();
  };

  window.speechSynthesis.speak(utterance);
  lastStreamSpeakTime = Date.now();
};

// 播放单个句子（入队，等上一句播完再播）
const speakSentence = (text) => {
  if (!text || !text.trim()) return;
  const cleanText = sanitizeTextForTTS(text);
  if (!cleanText) return;

  // 入队，若当前无句子在播则立即开始播放下一句
  streamingSentenceQueue.push(cleanText);
  if (!streamingIsPlaying) {
    playNextStreamingSentence();
  }
};

// 流式播放：从累积文本中提取新的完整句子并播放
const streamTTS = (fullText) => {
  if (!isVoiceMode.value || !streamingTtsActive.value || !fullText) return;

  // 在已播放位置之后，查找待播放的文本
  const remaining = fullText.substring(streamingTtsPlayedLength.value);
  if (!remaining || remaining.length < 2) return;

  // 1. 优先找句子结束符（强分割点：。！？.!?\n）
  const sentenceEndRegex = /[。！？.!?\n]/g;
  let lastSentenceEnd = -1;
  let match;
  while ((match = sentenceEndRegex.exec(remaining)) !== null) {
    lastSentenceEnd = match.index + 1;
  }

  if (lastSentenceEnd > 0) {
    // 有完整句子，立即播放
    const newSentences = remaining.substring(0, lastSentenceEnd);
    speakSentence(newSentences);
    streamingTtsPlayedLength.value += lastSentenceEnd;
    return;
  }

  // 2. 没有句子结束符时，按累积长度决定是否提前播放（避免第一句话太长时等太久）
  const MAX_WAIT_LENGTH = 30; // 超过30字强制播放（无标点也切）
  const MIN_WEAK_BREAK_LENGTH = 8; // 超过8字时，逗号/分号也作为分割点

  if (remaining.length >= MAX_WAIT_LENGTH) {
    // 累积文本已超过最大等待长度，强制播放
    speakSentence(remaining);
    streamingTtsPlayedLength.value += remaining.length;
    return;
  }

  if (remaining.length >= MIN_WEAK_BREAK_LENGTH) {
    // 检查是否有逗号/分号等弱分割点
    const weakBreakRegex = /[，,；;、：:]/g;
    let lastWeakBreak = -1;
    while ((match = weakBreakRegex.exec(remaining)) !== null) {
      lastWeakBreak = match.index + 1;
    }
    if (lastWeakBreak > 0) {
      const partial = remaining.substring(0, lastWeakBreak);
      speakSentence(partial);
      streamingTtsPlayedLength.value += lastWeakBreak;
    }
  }
};

// 完成流式播放：播放剩余的未完成文本
const finishStreamingTTS = (fullText, msgId) => {
  if (!isVoiceMode.value) return;
  // 流式未激活（例如中途被停止/切换），由 watch 自动播放处理
  if (!streamingTtsActive.value) return;

  // 播放剩余文本
  const remaining = fullText.substring(streamingTtsPlayedLength.value);
  if (remaining.trim()) {
    speakSentence(remaining);
  }
  streamingTtsPlayedLength.value = fullText.length;
  streamingTtsActive.value = false;

  // 只有实际有句子在播放或排队时才设置状态（用于语音条动画）
  // 非流式场景（没有收到任何流式文本）队列空且未播放，不设置状态，
  // 由下方 watch 检测到 !isSpeaking 后调用 autoPlayLatestAIVoice 常规播放
  if (streamingIsPlaying || streamingSentenceQueue.length > 0) {
    currentSpeakingMsgId.value = msgId;
    isSpeaking.value = true;
    startSpeakingCheck(); // 确保轮询兜底检测已启动
  }
};

// 自动播放最新AI消息
const autoPlayLatestAIVoice = () => {
  if (!isVoiceMode.value) return;
  if (isSending.value) return;
  // 字幕模式下不播报语音
  if (isPhoneMode.value && phoneSubtitleEnabled.value) return;
  const lastMsg = messages.value[messages.value.length - 1];
  if (lastMsg && !lastMsg.isSend && lastMsg.content && lastMsg.content.trim()) {
    const current = workScope.capture();
    nextTick(() => {
      if (!isCurrent(current) || !isVoiceMode.value || isSending.value) return;
      playAIVoice(lastMsg);
    });
  }
};

// 监听消息变化，自动播放AI语音
watch(
  () => messages.value.length,
  (newLen) => {
    // 流式 TTS 激活时跳过（由 finishStreamingTTS 处理播放）
    // isSpeaking 已为 true 时跳过（finishStreamingTTS 已开始播放，避免 playAIVoice 误判为停止）
    // userStoppedSending 时跳过（用户主动停止发送，阻止 SignalR 延迟完成事件触发自动播放）
    if (newLen > 0 && isVoiceMode.value && !isSending.value && !streamingTtsActive.value && !isSpeaking.value && !userStoppedSending.value) {
      autoPlayLatestAIVoice();
    }
  }
);

// 关闭语音模式时停止所有语音
watch(isVoiceMode, async (newVal) => {
  const current = workScope.capture();
  if (newVal) {
    // 开启语音模式时：解锁语音合成 + 提前获取阿里云 ASR Token + 申请麦克风权限
    unlockSpeechSynthesis();
    await fetchAliAsrToken();
    if (!isCurrent(current) || !isVoiceMode.value) return;
    await ensureMicrophonePermission();
  } else {
    releaseVoiceWork();
    micPermissionGranted.value = false;
  }
}, { flush: 'sync' });

// 转文字开关变化时，保持滚动到最底部（避免内容高度变化导致跳动）
watch(showTextInVoiceMode, () => {
  const current = workScope.capture();
  nextTick(() => {
    nextTick(() => {
      if (!isCurrent(current)) return;
      scrollToBottom();
    });
  });
});

// 电话模式：AI回复播放结束后重置录音，重新开始监听
watch(isSpeaking, (newVal, oldVal) => {
  if (!isPhoneMode.value) return;
  // 字幕模式下识别一直运行，不需要重启
  if (phoneSubtitleEnabled.value) return;
  // TTS 播放结束 → 停止当前识别，重置后重新开始监听
  if (oldVal && !newVal) {
    scheduleTimeout(() => {
      if (isPhoneMode.value && !isSpeaking.value) {
        // 用户正在说话中，不中断识别
        if (Date.now() - phoneLastSpeechTime < PHONE_SILENCE_THRESHOLD) return;
        if (recognition.value) {
          try { recognition.value.abort(); } catch (e) { /* ignore */ }
        }
        isRecording.value = false;
        resetPreviewTextBuffer();
        startPhoneAutoListen();
      }
    }, 300);
  }
});

// 电话模式：非流式场景（isSpeaking从未true），发送结束后恢复监听
watch(isSending, (newVal, oldVal) => {
  if (!isPhoneMode.value) return;
  // 字幕模式下识别一直运行，不需要重启
  if (phoneSubtitleEnabled.value) return;
  // 发送结束且未进入播放状态 → 恢复监听
  if (oldVal && !newVal && !isSpeaking.value) {
    scheduleTimeout(() => {
      if (isPhoneMode.value && !isSending.value && !isSpeaking.value) {
        // 用户正在说话中，不中断识别、不重置 buffer
        if (Date.now() - phoneLastSpeechTime < PHONE_SILENCE_THRESHOLD) return;
        if (recognition.value) {
          try { recognition.value.abort(); } catch (e) { /* ignore */ }
        }
        isRecording.value = false;
        resetPreviewTextBuffer();
        startPhoneAutoListen();
      }
    }, 300);
  }
});

// 字幕开启时立即停止播放中的语音，恢复监听
watch(phoneSubtitleEnabled, (newVal) => {
  if (!isPhoneMode.value || !newVal) return;
  if (isSpeaking.value || window.speechSynthesis.speaking) {
    window.speechSynthesis.cancel();
    streamingTtsActive.value = false;
    streamingTtsPlayedLength.value = 0;
    staticPlaybackMsgId.value = null;
    staticSentenceList.value = [];
    staticSentenceIndex.value = 0;
    resetSpeakingState();
    // 恢复监听
    if (recognition.value) {
      try { recognition.value.abort(); } catch (e) { /* ignore */ }
    }
    isRecording.value = false;
    resetPreviewTextBuffer();
    scheduleTimeout(() => {
      if (isPhoneMode.value) startPhoneAutoListen();
    }, 300);
  }
});

// 字幕面板自动滚动到底部
const phoneSubtitleScrollRaf = ref(null);
watch([() => phoneMessages.value.length, () => {
  const msgs = phoneMessages.value;
  return msgs.length ? msgs[msgs.length - 1].content : '';
}], () => {
  if (!phoneSubtitleEnabled.value) return;
  if (phoneSubtitleScrollRaf.value) return;
  phoneSubtitleScrollRaf.value = requestAnimationFrame(() => {
    phoneSubtitleScrollRaf.value = null;
    if (phoneSubtitlePanel.value) {
      phoneSubtitlePanel.value.scrollTop = phoneSubtitlePanel.value.scrollHeight;
    }
  });
});


  function releaseVoiceWork() {
    workScope.invalidate();
    pendingStopTimer.value = null;
    stopPhoneCallTimer();
    stopPhoneSilenceTimer();
    staticPlaybackGen.value++;
    streamingTtsActive.value = false;
    streamingTtsPlayedLength.value = 0;
    staticPlaybackMsgId.value = null;
    staticSentenceList.value = [];
    staticSentenceIndex.value = 0;
    resetSpeakingState();
    window.speechSynthesis?.cancel();
    if (recognition.value) {
      const rec = recognition.value;
      recognition.value = null;
      rec.onend = null; rec.onerror = null; rec.onresult = null;
      try { rec.abort(); } catch (e) { /* ignore */ }
    }
    isRecording.value = false;
    isRecognizing.value = false;
    voiceSentHint.value = '';
    if (phoneSubtitleScrollRaf.value) cancelAnimationFrame(phoneSubtitleScrollRaf.value);
    phoneSubtitleScrollRaf.value = null;
    resetPreviewTextBuffer();
  }
  watch(() => activeConversation.value?.id, () => {
    isPhoneMode.value = false;
    phoneModeCollapsed.value = false;
    isVoiceMode.value = false;
    releaseVoiceWork();
  }, { flush: 'sync' });
  onUnmounted(() => {
    workScope.dispose();
    releaseVoiceWork();
    window.speechSynthesis?.removeEventListener('voiceschanged', onVoicesChanged);
  });
  return { isVoiceMode, showTextInVoiceMode, isRecording, isRecognizing, recognizedPreviewText, voiceSentHint, pendingStopTimer, recognition, asrProviderLabel, isSpeaking, currentSpeakingMsgId, voiceSpeed, speedOptions, setVoiceSpeed, isPhoneMode, phoneModeCollapsed, phoneSubtitleEnabled, phoneMessages, phoneSubtitlePanel, phoneCallDuration, phoneStatusText, startRecording, stopRecording, cancelRecording, enterPhoneMode, stopPhoneCallTimer, formatPhoneCallDuration, stopPhoneSilenceTimer, sendPhoneMessage, hangUpPhone, staticSentenceList, staticSentenceIndex, staticPlaybackMsgId, staticPlaybackGen, playAIVoice, streamingTtsPlayedLength, streamingTtsActive, userStoppedSending, resetSpeakingState, stopSpeakingCheck, startStreamingTTS, streamTTS, finishStreamingTTS, phoneSubtitleScrollRaf };
}
