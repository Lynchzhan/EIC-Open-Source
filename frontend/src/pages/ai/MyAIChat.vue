<template>
  <div class="ai-chat-container eic-chat-workspace" :class="{ 'embedded-agent-chat': embedded }">
    <div v-if="embedded && !activeConversation" class="embedded-trial-start" :aria-busy="trialStarting">
      <p>{{ trialError || (trialReady ? '准备就绪；开始后创建当前智能体的个人试用会话。' : '正在核对智能体与能力…') }}</p>
      <n-button v-if="trialReady" type="primary" :loading="trialStarting" @click="startEmbeddedTrial">开始试用会话</n-button>
      <n-button v-else-if="trialError" @click="loadTrialAgent">重试核对</n-button>
    </div>
    <div ref="chatLayout" class="chat-layout" :class="{ 'conversations-expanded': conversationsOpen, 'is-resizing': sidebarResize.resizing.value }"
      :style="{ '--chat-sidebar-width': `${sidebarResize.width.value}px` }">
      <button v-if="!embedded && conversationsOpen" type="button" class="conversation-backdrop"
        aria-label="关闭会话列表" @click="closeConversations" />
      <!-- 左侧对话列表 -->
      <aside v-if="!embedded" v-show="conversationsOpen" id="chat-conversations" ref="conversationPanel" class="chat-sidebar" :class="{ 'is-open': conversationsOpen }"
        :role="conversationNarrow ? 'dialog' : undefined" :aria-modal="conversationNarrow && conversationsOpen ? true : undefined"
        aria-label="会话列表" @keydown="handleConversationPanelKey" @keydown.esc.stop="closeConversations">
        <div class="sidebar-header">
          <div class="sidebar-title-group">
            <div class="sidebar-icon">
              <MessageOutlined />
            </div>
            <h3>对话中心</h3>
            <n-button v-if="conversationNarrow" size="small" style="margin-left:auto" @click="closeConversations">关闭</n-button>
          </div>
          <div class="sidebar-actions">
            <ConversationLabels v-if="!experience" ref="labelManager" @updated="applyConversationLabels" />
            <n-button
              type="primary"
              @click="() => showAgentSelectionModal()"
              size="small"
              class="add-button"
            >
              <template #icon>
                <PlusOutlined />
              </template>
              新建对话
            </n-button>
            <n-button type="error" secondary
              size="small"
              :disabled="!conversations.length"
              @click="confirmClearAllConversations"
            >
              一键清除
            </n-button>
          </div>
          <div v-if="!conversationNarrow" class="sidebar-width-controls" aria-label="会话列表宽度">
            <button type="button" aria-label="缩窄会话列表" :disabled="sidebarResize.width.value <= sidebarResize.minimum" @click="sidebarResize.adjust(-16)">缩窄</button>
            <button type="button" aria-label="加宽会话列表" :disabled="sidebarResize.width.value >= sidebarResize.maximum.value" @click="sidebarResize.adjust(16)">加宽</button>
            <button type="button" @click="sidebarResize.reset">重置</button>
          </div>
        </div>
        <div class="conversation-list" :aria-busy="loadingConversations">
          <div v-if="loadingConversations" class="chat-load-state" role="status">正在加载会话…</div>
          <div v-if="conversationError" class="chat-load-state is-error" role="alert">
            <span>{{ conversationError }}</span>
            <n-button size="small" :disabled="loadingConversations" @click="loadConversations()">重新加载</n-button>
          </div>
          <div v-if="!loadingConversations && !conversationError && !conversations.length" class="empty-conversations">
            <UiState description="暂无对话记录，选择智能体开始新对话" />
          </div>
          <div v-if="conversations.length" class="conversation-items">
              <div v-for="item in conversations" :key="String(item.id)"
                class="conversation-item"
                :class="{ active: item.id === activeConversationId, generating: isConversationSending(item.id) }"
              >
                <button type="button" class="conversation-content"
                  :aria-pressed="String(item.id) === String(activeConversationId)"
                  :aria-label="`打开会话：${item.title || '新对话'}`" @click="selectConversation(item)">
                  <span class="conversation-title">
                    {{ item.title || "新对话" }}
                    <span v-if="isConversationSending(item.id)" class="generating-badge">生成中</span>
                  </span>
                  <span class="conversation-preview" :title="item.lastMessage">
                    {{ item.lastMessage || "开始新的对话..." }}
                  </span>
                  <span class="conversation-time">
                    {{ formatDate(item.updatedAt) }}
                  </span>
                </button>
                <div class="conversation-actions" @click.stop>
                  <n-button text size="small" aria-label="管理会话标题与摘要" @click="labelManager?.open(item)">标题</n-button>
                  <span v-if="['failed','uncertain','cancelled'].includes(item.labelStatus)" class="conversation-label-status" title="标题生成未完成，可手动重新生成">总结未完成</span>
                  <span v-else-if="['pending','running','provider_started','provider_done'].includes(item.labelStatus)" class="conversation-label-status" role="status">总结处理中</span>
                  <n-button
                    text
                    class="delete-btn"
                    :aria-label="`删除会话：${item.title || '新对话'}`"
                    @click="(event) => deleteConversation(item.id, event)"
                  >
                    <template #icon>
                      <DeleteOutlined />
                    </template>
                  </n-button>
                </div>
              </div>
          </div>
        </div>
      </aside>
      <div v-if="!embedded && !conversationNarrow && conversationsOpen" class="conversation-resizer" role="separator" tabindex="0"
        aria-label="调整会话列表宽度，左右方向键调整，Home 最窄，End 最宽，双击重置"
        aria-orientation="vertical" aria-controls="chat-conversations"
        :aria-valuemin="sidebarResize.minimum" :aria-valuemax="sidebarResize.maximum.value" :aria-valuenow="Math.round(sidebarResize.width.value)"
        @pointerdown="sidebarResize.start" @pointermove="sidebarResize.move" @pointerup="sidebarResize.end"
        @pointercancel="sidebarResize.end" @lostpointercapture="sidebarResize.end" @keydown="sidebarResize.keydown" @dblclick="sidebarResize.reset" />

      <!-- 右侧聊天区域 -->
      <div class="chat-main" :inert="!embedded && conversationNarrow && conversationsOpen">
        <div class="chat-header" v-if="activeConversation || !embedded">
          <button v-if="!embedded" ref="conversationToggle" type="button" class="conversation-toggle"
            :aria-expanded="conversationsOpen" aria-controls="chat-conversations"
            @click="conversationsOpen = !conversationsOpen">
            <MessageOutlined aria-hidden="true" />
            <span>{{ conversationsOpen ? '收起会话' : '展开会话' }}</span>
          </button>
          <div class="header-left">
            <div class="chat-context">
              <h2 :title="activeConversation?.title">{{ activeConversation?.title || '选择或新建对话' }}</h2>
              <div class="agent-info" v-if="activeConversation?.appId">
                <RobotOutlined aria-hidden="true" />
                <n-select
                  class="agent-switcher"
                  :value="activeConversation.appId == null ? undefined : String(activeConversation.appId)"
                  :options="aiApps"
                  placeholder="选择智能体"
                  :disabled="embedded || isSending || activeInspection.agentSwitchBusy || !aiApps.length"
                  @update:value="switchConversationAgent"
                />
              </div>
            </div>
          </div>
          <div v-if="activeConversation" class="header-status execution-status" :class="`state-${historyUnavailable ? (loadingMessages ? 'pending' : 'error') : activeExecution.key}`" role="status" aria-live="polite">
            <span class="status-dot"></span>
            <span class="status-text">{{ historyUnavailable ? (loadingMessages ? '正在加载记录' : '记录加载失败') : activeExecution.label }}</span>
          </div>
        </div>

        <nav v-if="activeConversation" class="conversation-navigation" aria-label="会话导航">
          <details>
            <summary>轮次目录（已加载 {{ conversationTurns.length }} 轮）</summary>
            <div class="conversation-turns">
              <button v-for="(turn, index) in conversationTurns" :key="turn.id" type="button"
                @click="jumpToTurn(turn.id)">{{ index + 1 }}. {{ turn.summary }} · {{ turn.status }}</button>
              <n-button v-if="hasMoreMessages" size="small" :loading="loadingMessages" @click="loadMoreMessages">加载更早轮次</n-button>
            </div>
          </details>
          <MemoryPanel :session-id="activeConversationId" :key="String(activeConversationId)" />
          <details v-if="experience && experience.session.pages.includes('observability')">
            <summary>体验运行摘要</summary>
            <p>{{ experience.stage || '尚未开始运行' }}</p>
            <p>{{ experience.session.mode === 'public' ? '虚构演示，不调用真实模型，不生成真实 Trace。' : '只展示本次体验状态，不读取正式业务 Trace 或导出 Langfuse。' }}</p>
            <p v-if="experience.session.quota">累计调用 {{ experience.session.quota.usedCalls }}/{{ experience.session.quota.calls }} · Token 预留 {{ experience.session.quota.usedTokens }}/{{ experience.session.quota.tokens }}</p>
          </details>
        </nav>
        <div class="chat-messages" ref="messagesContainer" v-if="activeConversation" :aria-busy="loadingMessages" @scroll="handleScroll">
          <div v-if="loadingMessages && (loadingHistoryPage !== 1 || !messages.length)" class="chat-load-state" role="status">正在加载聊天记录…</div>
          <div v-if="historyError" class="chat-load-state is-error" role="alert">
            <span>{{ historyError.text }}</span>
            <n-button size="small" :disabled="loadingMessages"
              @click="loadChatHistory(activeConversationId, historyError.page)">重试记录</n-button>
          </div>
          <div v-if="!loadingMessages && !historyError && !messages.length && !isSending"
            class="chat-load-state" role="status">暂无消息，可以发送第一条消息。</div>
          <section v-if="activeInspection.requestId" class="run-inspection" aria-label="当前请求执行记录">
            <div class="inspection-heading">
              <button type="button" class="inspection-toggle" :aria-expanded="inspectionOpen"
                @click="inspectionOpen = !inspectionOpen">{{ inspectionOpen ? '收起运行详情' : '展开运行详情' }}</button>
              <n-button size="small" :loading="activeInspection.inspectionLoading"
                :disabled="activeInspection.approvalBusy || isSending" @click="refreshActiveInspection">刷新状态</n-button>
            </div>
            <div v-show="inspectionOpen">
            <p v-if="activeInspection.inspectionLoading && !activeInspection.trace" role="status">正在读取审批与 Trace…</p>
            <p v-if="activeInspection.inspectionError" class="inspection-error" role="alert">{{ activeInspection.inspectionError }}</p>
            <div v-if="activeInspection.trace" class="trace-summary">
              <span>状态：{{ executionPresentation({ status: activeInspection.trace.status }).label }}</span>
              <span>事件：{{ activeInspection.trace.eventCount }}</span>
              <span>模型调用：{{ activeInspection.trace.providerInvocations ?? '未提供' }}</span>
              <span>Token：{{ activeInspection.trace.usage?.totalTokens ?? '未提供' }}</span>
              <code>Trace {{ activeInspection.trace.traceId }}</code>
              <span v-if="activeInspection.trace.errorCode">错误标识：{{ activeInspection.trace.errorCode }}</span>
            </div>
            <RunReliabilityPanel v-if="activeInspection.trace" :trace="activeInspection.trace" :usage="activeReliabilityUsage" />
            </div>
            <div v-if="activeApproval" class="approval-card" role="status">
              <div class="approval-heading">
                <strong>{{ activeApproval.toolName }}</strong>
                <span>{{ approvalExpired ? '审批已过期' : executionPresentation({ status: activeApproval.status }).label }}</span>
              </div>
              <p>{{ activeApproval.inputSummary }}</p>
              <code class="approval-digest">SHA-256 {{ activeApproval.inputDigest }}</code>
              <p class="approval-note">批准仅记录决定，不会执行工具。执行前会重新校验权限、参数与上下文。</p>
              <p v-if="approvalExpired">此票据已过期，待审批工具未执行。请重新发起请求。</p>
              <p v-else-if="!activeApproval.canDecide && !activeApproval.canContinue">当前票据只读，请刷新确认权限或执行状态。</p>
              <div class="approval-actions">
                <n-button v-if="activeApproval.canDecide" type="primary"
                  :disabled="approvalExpired || isSending || !!activeInspection.approvalBusy || activeInspection.inspectionLoading"
                  :loading="activeInspection.approvalBusy === 'approve'" @click="performApproval('approve')">批准</n-button>
                <n-button v-if="activeApproval.canDecide" type="error" secondary
                  :disabled="approvalExpired || isSending || !!activeInspection.approvalBusy || activeInspection.inspectionLoading"
                  @click="performApproval('reject')">拒绝</n-button>
                <n-button v-if="activeApproval.canContinue" type="primary"
                  :disabled="approvalExpired || isSending || !!activeInspection.approvalBusy || activeInspection.inspectionLoading"
                  :loading="activeInspection.approvalBusy === 'continue'" @click="performApproval('continue')">执行并续跑</n-button>
              </div>
            </div>
            <p v-else-if="approvalPending && !activeInspection.inspectionLoading" role="status">等待人工确认，请刷新读取当前审批票据。</p>
            <p v-if="activeInspection.trace?.status === 'manual_review'" role="alert">结果需要人工复核，系统未自动重放工具，请勿重复发起相同操作。</p>
            <div v-show="inspectionOpen">
            <n-button v-if="activeInspection.trace?.canReadDetail" size="small"
              :loading="activeInspection.detailBusy" @click="toggleTraceDetails">管理员脱敏详情</n-button>
            <div v-if="activeInspection.detailOpen" class="trace-detail">
              <p>仅包含事件类型、状态、摘要标识；不展示参数、提示词或工具原始结果。</p>
              <p>分页累计最多 3000 条事件；工具列表为接口返回的最多 100 条摘要，不代表全量审计。工具超时值及幂等重试票据：本查询未提供。</p>
              <p v-if="activeInspection.detailError" class="inspection-error" role="alert">{{ activeInspection.detailError }}</p>
              <ol>
                <li v-for="event in activeInspection.detailEvents" :key="event.sequence">
                  <span>{{ event.sequence }} · {{ event.node }} · {{ event.eventType }}</span>
                  <span>{{ event.status }} · {{ event.createdAt }}</span>
                </li>
              </ol>
              <div v-for="tool in activeInspection.detailTools" :key="tool.callId" class="trace-tool">
                <strong>{{ tool.toolName }}</strong><span>{{ tool.status }} / {{ tool.effectStatus }}</span>
                <code>{{ tool.callId }}</code>
                <code v-if="tool.inputDigest">输入 SHA-256 {{ tool.inputDigest }}</code>
                <code v-if="tool.outputDigest">输出 SHA-256 {{ tool.outputDigest }}</code>
                <span v-if="tool.errorCode">错误标识：{{ tool.errorCode }}</span>
              </div>
              <n-button v-if="activeInspection.detailHasMore || activeInspection.detailError" size="small"
                :loading="activeInspection.detailBusy" @click="loadTraceDetails(activeConversationId)">继续读取</n-button>
            </div>
            </div>
          </section>
          <div
            v-for="message in messages"
            :key="message.id"
            :id="`chat-turn-${message.id}`"
            class="message-item"
            :class="{
              'user-message': message.isSend === true,
              'ai-message': message.isSend === false,
            }"
          >
            <div class="message-avatar" :class="{ 'avatar-ai': message.isSend === false, 'avatar-user': message.isSend === true }">
              <UserOutlined v-if="message.isSend === true" />
              <RobotOutlined v-else />
            </div>
            <div class="message-content">
              <div class="message-role">{{ message.isSend ? '我' : 'Agent' }}</div>
              <!-- 非语音模式：文字正常显示 -->
              <ChatMessageBody v-if="message.isSend === false && !isVoiceMode" :content="message.content" />
              <ChatMessageBody v-else-if="message.isSend === true" :content="message.content" is-user />
              <!-- AI 语音条 -->
              <div
                v-if="isVoiceMode && message.isSend === false && message.content"
                class="voice-msg-bar"
              >
                <div class="voice-msg-left" @click.stop="playAIVoice(message)">
                  <SoundOutlined class="voice-msg-icon" />
                  <div :class="['voice-msg-wave', { playing: isSpeaking && currentSpeakingMsgId === message.id }]">
                    <span v-for="i in 8" :key="i" class="voice-wave-bar" :style="{ animationDelay: (i * 0.1) + 's' }"></span>
                  </div>
                  <span v-if="isSpeaking && currentSpeakingMsgId === message.id" class="voice-msg-duration">播放中...</span>
                </div>
              </div>
              <!-- 语音模式 + 转文字：文字显示在语音条下方 -->
              <ChatMessageBody v-if="isVoiceMode && showTextInVoiceMode && message.isSend === false && message.content" class="voice-expanded-text" :content="message.content" />
              <n-collapse v-if="message.aiReasoningContent" class="message-collapse" :default-expanded-names="expandedReasoning ? ['reasoning'] : []">
                <n-collapse-item name="reasoning" title="思考过程">
                  <div class="collapse-content">
                    <div v-if="message.aiReasoningContent.length > 350">{{ truncateContent(message.aiReasoningContent) }}<a @click="showDetailModal('思考过程详情', message.aiReasoningContent)">点击查看详情</a></div>
                    <div v-else>{{ message.aiReasoningContent }}</div>
                  </div>
                </n-collapse-item>
              </n-collapse>
              <n-collapse v-if="message.aiToolsContent" class="message-collapse tool-event-panel" :default-expanded-names="expandedTools ? ['tools'] : []">
                <n-collapse-item name="tools" title="工具调用">
                  <div class="collapse-content">
                    <div v-if="message.aiToolsContent.length > 350">{{ truncateContent(message.aiToolsContent) }}<a @click="showDetailModal('工具调用详情', message.aiToolsContent)">点击查看详情</a></div>
                    <div v-else>{{ message.aiToolsContent }}</div>
                  </div>
                </n-collapse-item>
              </n-collapse>
              <p v-if="message.eventHistoryError" class="chat-load-state is-error" role="alert">
                {{ message.eventHistoryError }}
                <n-button size="small" :loading="loadingMessages"
                  @click="loadChatHistory(activeConversationId, 1)">重试记录</n-button>
              </p>
              <ChatKnowledgeSources v-if="!message.isSend && knowledgeSources(message).length" :sources="knowledgeSources(message)"
                :request-id="message.requestId" />
              <ChatCitations v-if="(message.citations || message.tokenUsage?.citations || []).some(source => source.type === 'web')"
                :sources="(message.citations || message.tokenUsage?.citations || []).filter(source => source.type === 'web')" />
              <n-collapse v-if="message.attachments?.length" class="message-collapse">
                <n-collapse-item name="files" title="附件">
                  <div class="collapse-content file-list-content">
                    <div v-for="file in message.attachments" :key="file.id" class="file-item">
                      <FileTextOutlined />
                      <a href="#" @click.prevent="downloadChatFile(file)" class="file-link">
                        {{ file.name }}
                      </a>
                    </div>
                  </div>
                </n-collapse-item>
              </n-collapse>
              <n-collapse v-if="message.aIChatHistorysBindLogs && message.aIChatHistorysBindLogs.length > 0" class="message-collapse" ghost>
                <n-collapse-item name="logs" title="AI相关日志">
                  <div class="collapse-content">
                    <div v-for="(log, index) in message.aIChatHistorysBindLogs" :key="index" class="log-item">
                      <span class="log-type-tag">{{ getLogTypeName(log.logType) }}</span>
                      <template v-if="log.logContent && log.logContent.length > 200">
                        {{ truncateContent(log.logContent) }}<a @click="showDetailModal('AI相关日志详情', log.logContent)">点击查看详情</a>
                      </template>
                      <template v-else>{{ log.logContent }}</template>
                    </div>
                  </div>
                </n-collapse-item>
              </n-collapse>
              <details v-if="!message.isSend && message.tokenUsage?.historyContext" class="message-collapse">
                <summary>本轮记忆与上下文来源</summary>
                <p>会话摘要：{{ message.tokenUsage.historyContext.summaryVersion ? `v${message.tokenUsage.historyContext.summaryVersion}` : '未使用' }}；
                  覆盖至消息 {{ message.tokenUsage.historyContext.summaryThroughId || '无' }}。</p>
                <p>已装入的长期记忆 ID：{{ message.tokenUsage.memoryRecall?.retainedMemoryIds?.join(', ') || '无' }}。可在“记忆与候选”中查看、纠正或遗忘。</p>
                <p>载入历史 {{ message.tokenUsage.historyContext.loadedMessages || 0 }} 条；
                  预算裁剪 {{ message.tokenUsage.budget?.droppedHistoryMessages || 0 }} 条。估算并非精确 token 数。</p>
              </details>
              <div class="message-actions">
                <n-button
                  text
                  size="small"
                  aria-label="复制消息"
                  @click="copyMessageContent(message.content)"
                  class="copy-button">
                  <template #icon>
                    <CopyOutlined />
                  </template>
                </n-button>
              </div>
              <div class="message-time">
                {{ formatTime(message.createdAt) }}
                <span v-if="message.status === 'failed' && message.tokenUsage?.reason !== 'approval_required'" class="generation-status state-error" role="status">
                  {{ message.tokenUsage?.partial ? '部分回答，生成未完成。' : '' }}{{ reliabilityHint(message.tokenUsage?.reason, message.status) || '生成失败，请查看调用链路中的错误记录。' }}
                </span>
                <span v-else-if="message.status === 'cancelled'" class="generation-status state-neutral" role="status">已停止生成</span>
                <span v-else-if="message.isSend === false && message.status" class="generation-status"
                  :class="`state-${executionPresentation({ status: message.status, reason: message.tokenUsage?.reason }).key}`" role="status">
                  {{ executionPresentation({ status: message.status, reason: message.tokenUsage?.reason }).label }}
                </span>
              </div>
              <TurnUsage v-if="showTurnUsage(messages, message) && turnStatistics[message.requestId]"
                :turn="turnStatistics[message.requestId]" @trace="openUsageTrace" />
            </div>
          </div>

          <div v-if="isSending || hasUnconfirmedContent(activeInspection)" class="message-item ai-message">
            <div class="message-avatar avatar-ai">
              <RobotOutlined />
            </div>
            <div class="message-content">
              <div v-if="isSending" class="message-role" role="status"><n-spin size="small" /> Agent · {{ activeInspection.stopRequested ? '正在请求停止，等待后台确认' : activeExecution.label }} · {{ runningSeconds }} 秒</div>
              <div v-else class="message-role" role="status">连接已断开，以下为已接收内容；请恢复原请求连接确认后台结果。</div>
              <p v-if="activeInspection.connectionState === 'reconnecting'" role="status">连接中断，正在恢复原请求；后台状态尚待确认。</p>
              <details class="live-execution-details">
                <summary>执行过程与阶段耗时</summary>
                <p>只展示服务端阶段事件、工具结果摘要及模型实际返回的推理摘要。</p>
                <ol>
                  <li v-for="(stage, index) in activeInspection.stages || []" :key="index">
                    {{ stage.text }} · {{ Math.max(0, Math.floor((((activeInspection.stages || [])[index + 1]?.at || approvalClock) - stage.at) / 1000)) }} 秒
                  </li>
                </ol>
              </details>
              <div v-if="isSending" class="typing-indicator">
                <span></span>
                <span></span>
                <span></span>
              </div>
             <div v-if="!isVoiceMode || showTextInVoiceMode" class="message-text message-text-stream">{{ aimessage2 }}</div>
              <!-- 流式播放中的语音条 -->
              <div v-if="isVoiceMode && isSpeaking && streamingTtsActive" class="voice-msg-bar voice-msg-bar-streaming">
                <div class="voice-msg-left">
                  <SoundOutlined class="voice-msg-icon" />
                  <div class="voice-msg-wave playing">
                    <span v-for="i in 8" :key="i" class="voice-wave-bar" :style="{ animationDelay: (i * 0.1) + 's' }"></span>
                  </div>
                  <span class="voice-msg-duration">播放中...</span>
                </div>
              </div>
              <div class="message-time stream-status">{{ aimessage}}</div>
                <n-collapse v-model:expanded-names="reasoningActiveKey" class="message-collapse" v-if="aIReasoningContentMsg">
                <n-collapse-item name="reasoning" title="模型提供的推理摘要">
                  <div class="collapse-content">
                    <div v-if="aIReasoningContentMsg.length > 350">{{ truncateContent(aIReasoningContentMsg) }}<a @click="showDetailModal('思考过程详情', aIReasoningContentMsg)">点击查看详情</a></div>
                    <div v-else>{{ aIReasoningContentMsg }}</div>
                  </div>
                </n-collapse-item>
              </n-collapse>
              <n-collapse v-model:expanded-names="toolsActiveKey" class="message-collapse tool-event-panel" v-if="aIToolsContentMsg">
                <n-collapse-item name="tools" title="工具调用">
                  <div class="collapse-content">
                    <div v-if="aIToolsContentMsg.length > 350">{{ truncateContent(aIToolsContentMsg) }}<a @click="showDetailModal('工具调用详情', aIToolsContentMsg)">点击查看详情</a></div>
                    <div v-else>{{ aIToolsContentMsg }}</div>
                  </div>
                </n-collapse-item>
              </n-collapse>
            </div>
          </div>
        </div>

        <div class="chat-input-area" v-if="activeConversation">
          <button v-if="!followLatest" type="button" class="latest-message-button" :class="{ 'has-updates': isSending || hasUnreadUpdates }" @click="jumpToLatest">
            <span v-if="isSending" class="latest-generating-dot" aria-hidden="true"></span>
            {{ isSending ? '正在生成 · 回到最新' : hasUnreadUpdates ? '有新内容 · 回到最新' : '回到最新' }} ↓
          </button>
          <div class="composer-information">
          <details v-if="conversationBudget?.configured" class="composer-detail">
            <summary>预算 · 第 {{ conversationBudget.tier }} 档</summary>
            <section class="conversation-usage" aria-label="会话硬预算">
            <span>调用 {{ conversationBudget.totals.calls }}/{{ conversationBudget.limits.maxCalls }}</span>
            <span>Token（含未知预留）{{ conversationBudget.totals.tokens }}/{{ conversationBudget.limits.maxTokens }}</span>
            <span>调用时长 {{ Math.ceil(conversationBudget.totals.seconds) }}/{{ conversationBudget.limits.maxSeconds }} 秒</span>
            <span>当前第 {{ conversationBudget.tier }} 档</span>
            <span v-for="(item, kind) in conversationBudget.byKind" :key="kind">{{ { model:'回答模型', embedding:'查询向量化', rerank:'重排' }[kind] || kind }}：{{ item.calls }} 次 / {{ item.tokens }} Token<span v-if="item.unknown">（含未知预留）</span></span>
            <span v-if="conversationBudget.reason" role="status">{{ conversationBudget.reason }}</span>
            <span v-if="conversationBudget.blocked?.requestedTokens != null">本次预留 {{ conversationBudget.blocked.requestedTokens }} Token / 剩余 {{ conversationBudget.blocked.remainingTokens }} Token（保守预留，非实报用量）</span>
            <span v-if="conversationBudget.historical?.unknown">历史用量有 {{ conversationBudget.historical.unknown }} 项待核实，未按零计算；可联系管理员核对，或点击左侧“新建对话”继续。</span>
          </section>
          </details>
          <p v-if="budgetNearLimit" role="status">会话预算已使用至少 80%，请展开预算详情查看剩余 Token、调用次数和时长。</p>
          <p v-if="budgetReadError" role="status">{{ budgetReadError }}</p>
          <p v-if="experience" role="status">{{ experience.stage }}<span v-if="experience.busy"> · 已等待 {{ runningSeconds }} 秒</span></p>
          <details v-if="!experience" class="composer-detail">
            <summary>会话统计 · {{ sessionStatistics?.summary?.roundCount ?? '—' }} 轮</summary>
            <div class="conversation-usage" aria-label="会话累计" data-testid="conversation-usage">
            <span>本会话 <strong>{{ sessionStatistics?.summary?.roundCount ?? '未采集' }}</strong> 轮</span>
            <span>累计 Token <strong>{{ tokenValue(sessionStatistics?.summary?.usage?.totalTokens) }}</strong></span>
            <span>累计处理 <strong>{{ seconds(sessionStatistics?.summary?.processingMs) }}</strong></span>
            <span v-if="sessionStatistics && !sessionStatistics.summary.timingComplete">耗时统计不完整</span>
            <span v-if="statisticsError" role="status">{{ statisticsError }}</span>
            <n-button text size="small" :loading="statisticsLoading" aria-label="刷新会话统计" title="刷新会话统计"
              @click="refreshStatistics(activeConversationId)"><template #icon><ReloadOutlined /></template></n-button>
          </div>
          </details>
          <p v-if="activeInspection.reasoningConfig" class="actual-reasoning" role="status">
            实际模型 {{ activeInspection.reasoningConfig.modelName || activeInspection.reasoningConfig.modelId }} · 思考 {{ reasoningLabel(activeInspection.reasoningConfig.effort) }}
            <span v-if="activeInspection.reasoningConfig.adjusted">（配置或模型已变化，采用当前模型默认档位）</span>
          </p>
          </div>
          <ChatComposerFrame>
            <div v-if="composerAttachments.length" class="composer-attachments">
              <span v-if="!experience" class="attachment-budget-hint">附件按会话保留；每次回读合计最多 {{ chatCapabilities.attachmentMaxFiles }} 个、{{ Math.floor((chatCapabilities.attachmentMaxBytes || 0) / 1048576) }} MiB、{{ chatCapabilities.attachmentMaxChars }} 字符。图片仅识别文字。</span>
              <n-tag v-for="(file, index) in composerAttachments" :key="file.id || index" :closable="!experience || (experienceAttachmentsAllowed && !experienceAttachmentBusy && !isSending)" @close="removeComposerAttachment(file, index)">{{ file.name }}</n-tag>
            </div>
            <!-- 文字输入框（仅语音模式关闭时显示） -->
            <n-input type="textarea"
              v-if="!isVoiceMode"
              v-model:value="newMessage"
              :placeholder="'输入消息...'"
              aria-label="聊天消息"
              class="message-input"
              @keydown.enter="handlePressEnter"
              :disabled="isSending"
              :autosize="{ minRows: 2, maxRows: composerMaxRows }"
              clearable
            />
            <!-- 语音模式: 录音预览 + 发送提示 -->
            <div v-if="isVoiceMode" class="voice-input-area">
              <!-- 录音中显示识别文字预览 -->
              <div v-if="isRecording || isRecognizing" class="voice-preview">
                <span class="voice-preview-label">{{ isRecording ? '录音中...' : '正在识别...' }}</span>
                <span class="voice-preview-text">{{ recognizedPreviewText || '...' }}</span>
              </div>
              <!-- 发送后提示 -->
              <div v-else-if="voiceSentHint" class="voice-sent-hint">
                <CheckCircleFilled v-if="voiceSentHint.includes('已发送')" class="voice-sent-icon success" />
                <InfoCircleFilled v-else class="voice-sent-icon warning" />
                <span>{{ voiceSentHint }}</span>
              </div>
              <div v-else class="voice-hint-text">按住下方按钮开始说话，说完松开自动发送</div>
            </div>
            <!-- 语音录制按钮（按住说话，松开自动发送） -->
            <div v-if="isVoiceMode" class="voice-recorder-area">
              <div class="voice-recorder-wrapper">
                <div
                  :class="['voice-record-btn', { recording: isRecording, recognizing: isRecognizing, disabled: isSending }]"
                  @mousedown="startRecording"
                  @mouseup="stopRecording"
                  @mouseleave="cancelRecording"
                  @touchstart.prevent="startRecording"
                  @touchend.prevent="stopRecording"
                  @touchcancel.prevent="cancelRecording"
                >
                  <AudioOutlined class="voice-record-icon" />
                  <span class="voice-record-text">{{ isRecording ? '松开 结束' : '按住 说话' }}</span>
                </div>
              </div>
              <div class="asr-provider-tag">{{ asrProviderLabel }}</div>
              <div v-if="isRecording || isRecognizing" class="recording-indicator">
                <span class="recording-dot" :class="{ recognizing: isRecognizing }"></span>
                <span class="recording-text">{{ isRecording ? '正在录音...' : '正在识别...' }}</span>
              </div>
            </div>
            <div class="input-options">
              <div class="input-options-left">
                <div v-if="experience" data-experience-chat-attachment>
                  <input ref="experienceFileInput" type="file" hidden :disabled="!experienceAttachmentsAllowed || isSending || experienceAttachmentBusy"
                    accept=".pdf,.docx,.txt,.md,.json,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff,.gif,.xlsx,.xlsm,.xls,.csv,.pptx" @change="uploadExperienceAttachment" />
                  <n-button :disabled="!experienceAttachmentsAllowed || isSending || experienceAttachmentBusy" :loading="experienceAttachmentBusy" @click="experienceFileInput?.click()">上传文件</n-button>
                </div>
                <FileUpload v-else
                  class="chat-attachment-upload"
                  ref="fileUploadRef"
                  business="AIChat"
                  :key-value="activeConversationId || ''"
                  sign="chat"
                  :multiple="true"
                  :disabled="isSending || !chatCapabilities.attachments"
                  :accept="(chatCapabilities.attachmentFormats || []).map(format => '.' + format).join(',')"
                  :max-count="chatCapabilities.attachmentMaxFiles"
                  :max-file-bytes="chatCapabilities.attachmentMaxBytes"
                  :validate-file="validateChatAttachment"
                  :show-upload-list="false"
                  upload-button-text="上传文件"
                  @upload-success="handleFileUploadSuccess"
                  @upload-error="handleFileUploadError"
                />
                <ChatComposerTools class="composer-secondary-tools" :restricted="!!experience" v-model:online-search="composerOnlineSearch" :search-disabled="isSending || (experience ? !experienceSearchAllowed : !chatCapabilities.onlineSearch)"
                  v-model:voice-mode="isVoiceMode" v-model:show-text="showTextInVoiceMode"
                  :voice-speed="voiceSpeed" :speed-options="speedOptions" @speed="setVoiceSpeed"
                  @phone="enterPhoneMode" @restricted="message.info('体验账号不可操作')" />
                <n-popover trigger="click" placement="top-start" :style="{ maxWidth: 'min(360px, calc(100vw - 32px))', maxHeight: '45dvh', overflow: 'auto' }">
                  <template #trigger><n-button class="composer-more-trigger" aria-label="更多聊天工具">更多</n-button></template>
                  <ChatComposerTools :restricted="!!experience" v-model:online-search="composerOnlineSearch" :search-disabled="isSending || (experience ? !experienceSearchAllowed : !chatCapabilities.onlineSearch)"
                    v-model:voice-mode="isVoiceMode" v-model:show-text="showTextInVoiceMode"
                    :voice-speed="voiceSpeed" :speed-options="speedOptions" @speed="setVoiceSpeed"
                    @phone="enterPhoneMode" @restricted="message.info('体验账号不可操作')" />
                </n-popover>
              </div>
              <div class="composer-send-actions">
              <ChatReasoningSelector v-if="!experience" :session-id="activeConversationId" :app-id="activeConversation.appId"
                :disabled="isSending || activeInspection.agentSwitchBusy" @busy="reasoningBusy = $event" />
              <n-button class="composer-resume-button" v-if="ensureChatRun(activeConversationId)?.resumeRequestId" :disabled="isSending || activeInspection.agentSwitchBusy" @click="resumeChatStream">
                恢复原请求连接
              </n-button>
              <n-button
                type="primary"
                @click="isSending ? (canStopSend ? stopMessage() : null) : sendMessage()"
                :disabled="!isSending && (experienceAttachmentBusy || reasoningBusy || activeInspection.agentSwitchBusy || historyUnavailable || (!newMessage.trim() && composerAttachments.length === 0))"
                :class="['send-button', { stopping: isSending && canStopSend }]"
                :aria-label="isSending ? (canStopSend ? '停止生成' : '正在生成') : '发送消息'"
              >
                <template #icon>
                  <SendOutlined v-if="!isSending" />
                  <StopOutlined v-else />
                </template>
              </n-button>
              </div>
            </div>
          </ChatComposerFrame>
        </div>

        <div class="chat-placeholder" v-else-if="!embedded">
          <UiState title="从一个问题开始" description="选择已有会话继续工作，或选择智能体开启一段新对话。">
            <n-button type="primary" size="large" @click="() => showAgentSelectionModal()">
              <template #icon>
                <PlusOutlined />
              </template>
              新建对话
            </n-button>
          </UiState>
        </div>

        <n-modal v-model:show="detailModalVisible" preset="card" :title="detailModalTitle" class="chat-detail-modal">
          <div class="detail-modal-body">{{ detailModalContent }}</div>
        </n-modal>
      </div>
    </div>

    </div>

    <ObservationTraceDrawer :open="!!usageTrace" :trace-id="usageTrace" :query="usageTraceQuery"
      :admin="false" @close="usageTrace = ''" />
    <!-- 电话模式全屏覆盖 -->
    <Teleport to="body">
      <div v-if="isPhoneMode && !phoneModeCollapsed" class="phone-mode-overlay cloud-blue-phone">
        <div class="phone-call-container">
          <!-- 收起按钮：隐藏全屏覆盖层，后台保持聆听 -->
          <div class="phone-collapse-btn" @click="phoneModeCollapsed = true">
            <span class="phone-collapse-text">收起</span>
          </div>
          <!-- 字幕按钮 -->
          <div class="phone-subtitle-btn" :class="{ active: phoneSubtitleEnabled }" @click="phoneSubtitleEnabled = !phoneSubtitleEnabled">
            <span class="phone-subtitle-btn-text">字幕</span>
          </div>
          <!-- 顶部：通话计时 -->
          <div class="phone-timer">
            <span class="phone-timer-text">{{ formatPhoneCallDuration(phoneCallDuration) }}</span>
          </div>

          <!-- 中部：AI头像 + 名称 + 状态 + 波形 -->
          <div class="phone-main">
            <div class="phone-avatar">
              <div class="phone-avatar-ring" :class="{ active: isSpeaking }"></div>
              <RobotOutlined class="phone-robot" />
            </div>
            <div class="phone-name">{{ getAiAppName(activeConversation?.appId) || 'AI 助手' }}</div>
            <div class="phone-status">{{ phoneStatusText }}</div>
            <div class="phone-asr-tag">{{ asrProviderLabel }}</div>
            <!-- 播放波形（AI说话时显示） -->
            <div class="phone-wave" v-if="isSpeaking">
              <span v-for="i in 24" :key="i" class="phone-wave-bar" :style="{ animationDelay: (i * 0.05) + 's' }"></span>
            </div>
          </div>

          <!-- 底部：识别预览 + 麦克风指示器 + 挂断 -->
          <div class="phone-controls">
            <div class="phone-recognize-preview" v-if="isRecording || isRecognizing">
              {{ recognizedPreviewText || '...' }}
            </div>
            <div class="phone-buttons">
              <!-- 麦克风指示器（自动监听中） -->
              <div class="phone-mic-wrapper">
                <div :class="['phone-mic-indicator', { listening: isRecording }]">
                  <AudioOutlined class="phone-mic-icon" />
                </div>
                <span class="phone-mic-label">{{ isRecording ? '聆听中' : (isSending || isSpeaking ? '等待中' : '准备中') }}</span>
              </div>
              <!-- 手动发送按钮 -->
              <div class="phone-send-btn" @click="sendPhoneMessage" v-if="isRecording">
                <SendOutlined class="phone-send-icon" />
              </div>
              <!-- 挂断按钮 -->
              <div class="phone-hangup-btn" @click="hangUpPhone">
                <PhoneOutlined class="phone-hangup-icon" />
              </div>
            </div>
          </div>
          <!-- 字幕面板（提升到 phone-call-container 层级） -->
          <div v-if="phoneSubtitleEnabled" class="phone-subtitle-panel" ref="phoneSubtitlePanel">
            <div v-if="phoneMessages.length === 0" class="phone-subtitle-empty">开始说话后，字幕将显示在这里</div>
            <div v-for="(msg) in phoneMessages" :key="msg.id" :class="['phone-subtitle-item', msg.isSend ? 'user' : 'ai']">
              <span class="phone-subtitle-role">{{ msg.isSend ? '你' : 'AI' }}</span>
              <span class="phone-subtitle-content">{{ msg.content }}</span>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 电话模式收起后的悬浮恢复按钮：点击恢复全屏 -->
    <Teleport to="body">
      <div v-if="isPhoneMode && phoneModeCollapsed" class="phone-restore-btn cloud-blue-phone" @click="phoneModeCollapsed = false">
        <PhoneOutlined class="phone-restore-icon" :class="{ 'restore-pulse': isSpeaking }" />
        <div class="phone-restore-info">
          <span class="phone-restore-name">{{ getAiAppName(activeConversation?.appId) || 'AI 助手' }}</span>
          <span class="phone-restore-text">{{ isSpeaking ? '播放中' : (isRecording ? '聆听中' : '电话中') }}</span>
        </div>
        <span class="phone-restore-dot" :class="{ active: isRecording }"></span>
      </div>
    </Teleport>
</template>

<script setup>
import { useChatSpeech } from '../../shared/ui/useChatSpeech';

/* eslint-disable */
import { ref, reactive, onMounted, nextTick, watch, h, onUnmounted, computed } from "vue";
import http from '@/utils/http';
import { experienceContext } from '@/app/experience/runtime';
const experience = experienceContext.value;
import { loadPrivateFile, savePrivateBlob } from '@/utils/privateFiles';
import {
  AddOutline as PlusOutlined, PersonOutline as UserOutlined, HardwareChipOutline as RobotOutlined,
  ChatbubblesOutline as MessageOutlined, TrashOutline as DeleteOutlined, CopyOutline as CopyOutlined,
  ReloadOutline as LoadingOutlined, StopOutline as StopOutlined, DocumentTextOutline as FileTextOutlined,
  SendOutline as SendOutlined, MicOutline as AudioOutlined, VolumeHighOutline as SoundOutlined,
  CheckmarkCircle as CheckCircleFilled, InformationCircle as InfoCircleFilled, CallOutline as PhoneOutlined,
  ReloadOutline as ReloadOutlined,
} from '@vicons/ionicons5';
import { NButton, NSelect, NInput, NSwitch, NSpin, NCollapse, NCollapseItem, NTag, NModal, NPopover } from 'naive-ui';
import FileUpload from "../../components/FileUpload.vue";
import RunReliabilityPanel from "../../components/ai/RunReliabilityPanel.vue";
import MemoryPanel from "../../components/ai/MemoryPanel.vue";
import UiState from "../../shared/ui/components/UiState.vue";
import ChatMessageBody from "../../components/ai/ChatMessageBody.vue";
import ChatCitations from "../../components/ai/ChatCitations.vue";
import ChatKnowledgeSources from "../../components/ai/ChatKnowledgeSources.vue";
import ChatComposerFrame from "../../components/ai/ChatComposerFrame.vue";
import ChatComposerTools from "../../components/ai/ChatComposerTools.vue";
const composerViewport = window.matchMedia('(max-height: 480px)');
const composerMaxRows = ref(composerViewport.matches ? 2 : 5);
const onComposerViewport = event => { composerMaxRows.value = event.matches ? 2 : 5; };
onMounted(() => composerViewport.addEventListener('change', onComposerViewport));
onUnmounted(() => composerViewport.removeEventListener('change', onComposerViewport));
import ConversationLabels from '../../components/ai/ConversationLabels.vue';
const labelManager = ref(null);
const applyConversationLabels = labels => {
  for (const label of labels) {
    const item = conversations.value.find(v => String(v.id) === label.id);
    if (item) Object.assign(item, { title: label.title, summary: label.summary, lastMessage: label.summary || item.lastMessage, labelStatus: label.status });
    if (String(activeConversation.value?.id) === label.id) Object.assign(activeConversation.value, { title: label.title });
  }
};
import ChatReasoningSelector from "../../components/ai/ChatReasoningSelector.vue";
import { useConversationResize } from '../../shared/ui/useConversationResize';
import { useChatScroll } from '../../shared/ui/useChatScroll';
import { useConversationBudget } from '../../shared/ui/useConversationBudget';
import TurnUsage from '../../components/ai/TurnUsage.vue';
import ObservationTraceDrawer from '../../components/ai/ObservationTraceDrawer.vue';
import { useChatRuns } from '../../shared/ui/useChatRuns';
import { useChatInspection } from '../../shared/ui/useChatInspection';
import { useConversationStatistics } from '../../shared/ui/useConversationStatistics';
import { seconds, tokenValue, showTurnUsage } from '../../shared/ui/conversationObservation';
import { message, confirm, warning } from '@/app/ui/feedback';
import { getMyAIAppsALLList } from "../../api/ai/aiapps.js";
import { getAIChatsMyPageData, addAIChats, updateAIChats, deleteAIChats, clearMyAIChats } from "../../api/ai/aichats.js";
import {
  addAIChatHistorys,
  cancelAIChatRequest,
  decideChatApproval,
  continueChatApproval,
  getChatEvents,
} from "../../api/ai/aichathistorys.js";
import { startChatStream } from '../../utils/chatStream';
import { acceptRunTerminal, isConfirmedTerminal, cancelRunRequest, hasUnconfirmedContent } from '../../shared/ui/chatRunState';
import { GetSnowflakeId } from '../../api/baseapi';


import { safeContentUrl } from '../../utils/safeHtml.js';
import { executionPresentation, knowledgeSources } from '../../shared/ui/agentPresentation.js';
import { approvalTicket, createApprovalIdempotencyKey, continuationCursor } from '../../shared/ui/chatApproval.js';
import { useChatHistory } from '../../shared/ui/useChatHistory';
import { reliabilityHint } from '../../shared/ui/runReliability.js';
const inspectionOpen = ref(false);
const props = defineProps({ embedded: Boolean, agentId: { type: String, default: '' } });
const emit = defineEmits(['runtime-state']);
const trialReady = ref(false), trialStarting = ref(false), trialError = ref('');
let componentAlive = true;

// 会话数据仅来自当前用户 API。
const conversations = ref([]);
const activeConversationId = ref(null);
const activeConversation = ref(null);
const reasoningBusy = ref(false);
const reasoningLabel = effort => ({ none:'关闭', off:'关闭', on:'开启', minimal:'最低', low:'低', medium:'中', high:'高', xhigh:'最高' }[effort] || '上游默认');
const messages = ref([]);
const { sessionStatistics, turnStatistics, statisticsError, statisticsLoading, usageTrace, usageTraceQuery,
  refreshStatistics, openUsageTrace, invalidateStatistics } = useConversationStatistics({ activeConversationId, messages, experience });
watch(activeConversationId, () => { inspectionOpen.value = false; });
const newMessage = ref("");
const loadingConversations = ref(false);
const conversationError = ref('');
const conversationMedia = window.matchMedia('(max-width: 768px)');
const conversationNarrow = ref(conversationMedia.matches);
const conversationsOpen = ref(!props.embedded && !conversationNarrow.value);
const chatLayout = ref(null);
const sidebarResize = useConversationResize({ layout: chatLayout, narrow: conversationNarrow, open: conversationsOpen, persist: !experience && !props.embedded });
const onConversationViewport = event => {
  conversationNarrow.value = event.matches;
  conversationsOpen.value = !props.embedded && !event.matches;
};
onMounted(() => conversationMedia.addEventListener('change', onConversationViewport));
onUnmounted(() => conversationMedia.removeEventListener('change', onConversationViewport));
const conversationToggle = ref(null);
const conversationPanel = ref(null);
const handleConversationPanelKey = event => {
  if (!conversationNarrow.value || event.key !== 'Tab') return;
  const controls = [...(conversationPanel.value?.querySelectorAll('button:not(:disabled), input:not(:disabled), [tabindex="0"]') || [])]
    .filter(element => element.getClientRects().length > 0);
  const first = controls[0], last = controls[controls.length - 1];
  if (!first) { event.preventDefault(); return; }
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
};
watch(conversationsOpen, async open => {
  if (!open || !conversationNarrow.value) return;
  await nextTick();
  conversationPanel.value?.querySelector('button:not(:disabled)')?.focus();
});
const closeConversations = () => {
  if (!window.matchMedia('(max-width: 768px)').matches) return;
  const wasOpen = conversationsOpen.value;
  conversationsOpen.value = false;
  if (wasOpen && window.matchMedia('(max-width: 768px)').matches) {
    nextTick(() => conversationToggle.value?.focus());
  }
};
const loadErrorText = (error, fallback) => {
  const status = error?.cause?.response?.status || error?.response?.status;
  if (status === 403) return `${fallback}：没有访问权限（HTTP 403）。`;
  if (status === 404) return `${fallback}：记录不存在或已删除（HTTP 404）。`;
  return `${fallback}，请检查连接后重试。`;
};
const MAX_PARALLEL_CHAT_RUNS = 5;
const { chatRuns, deletedConversationIds, isLiveRun, ensureChatRun, parallelRunCount, isConversationSending, bindActiveField } =
  useChatRuns({ isAlive: () => componentAlive });
const persistActiveRunBuffers = () => {
  const run = ensureChatRun(activeConversationId.value);
  if (!run) return;
  run.messagesSnapshot = messages.value.slice();
  run.historyPage = currentPage.value;
  run.historyHasMore = hasMoreMessages.value;
  run.draftText = newMessage.value;
  run.draftFiles = uploadedFileList.value.map((file) => ({ ...file }));
};
const restoreRunBuffers = (conversationId) => {
  const run = ensureChatRun(conversationId);
  if (!run) return;
  newMessage.value = run.draftText;
  uploadedFileList.value = run.draftFiles.map((file) => ({ ...file }));
  pendingFileNames.value = uploadedFileList.value.map((file) => file.name);
  pendingFileUrls.value = uploadedFileList.value.map((file) => file.url);
};
const isSending = computed({
  get: () => Boolean(ensureChatRun(activeConversationId.value)?.sending),
  set: (val) => {
    const run = ensureChatRun(activeConversationId.value);
    if (run) run.sending = Boolean(val);
  },
});
const canStopSend = computed({
  get: () => Boolean(ensureChatRun(activeConversationId.value)?.canStopSend),
  set: (val) => {
    const run = ensureChatRun(activeConversationId.value);
    if (run) run.canStopSend = Boolean(val);
  },
});
const activeExecution = computed(() => {
  const run = chatRuns[String(activeConversationId.value)];
  const lastAssistant = [...messages.value].reverse().find((item) => item.isSend === false);
  return executionPresentation({
    sending: run?.sending,
    resumeRequestId: run?.resumeRequestId,
    status: run?.approval?.status || run?.trace?.status || run?.terminal?.status || lastAssistant?.status,
    reason: run?.terminal?.reason || lastAssistant?.tokenUsage?.reason,
  });
});
const approvalPending = computed(() => {
  const run = chatRuns[String(activeConversationId.value)];
  const lastAssistant = [...messages.value].reverse().find((item) => item.isSend === false);
  return !run?.sending && [run?.approval?.status, run?.terminal?.status, run?.terminal?.reason,
    lastAssistant?.status, lastAssistant?.tokenUsage?.reason].some((value) =>
      ['approval_required', 'waiting_approval', 'approval_approved'].includes(value));
});
const activeInspection = computed(() => chatRuns[String(activeConversationId.value)] || {});
const activeReliabilityUsage = computed(() => {
  const requestId = activeInspection.value.requestId;
  if (!requestId) return {};
  return [...messages.value].reverse().find((item) => String(item.requestId) === String(requestId)
    && item.tokenUsage && Object.keys(item.tokenUsage).length)?.tokenUsage || {};
});
const activeApproval = computed(() => activeInspection.value.approval);
const approvalClock = ref(Date.now());
const approvalClockTimer = setInterval(() => { approvalClock.value = Date.now(); }, 1000);
const runningSeconds = computed(() => Math.max(0, Math.floor((approvalClock.value - (activeInspection.value.startedAt || approvalClock.value)) / 1000)));
const { conversationBudget, budgetReadError, refreshConversationBudget } = useConversationBudget({
  conversationId: activeConversationId, isSending, http, disabled: !!experience,
});
const budgetNearLimit = computed(() => {
  const value = conversationBudget.value;
  if (!value?.configured) return false;
  return [['tokens', 'maxTokens'], ['calls', 'maxCalls'], ['seconds', 'maxSeconds']]
    .some(([used, limit]) => value.limits[limit] > 0 && value.totals[used] / value.limits[limit] >= 0.8);
});
onUnmounted(() => clearInterval(approvalClockTimer));
const approvalExpired = computed(() => activeApproval.value?.status === 'expired'
  || (activeApproval.value && approvalClock.value >= activeApproval.value.expiresAt * 1000));
const { resetInspection, refreshInspection, refreshActiveInspection, loadTraceDetails, toggleTraceDetails } =
  useChatInspection({ ensureChatRun, isLiveRun, activeConversationId, activeInspection });
const performApproval = async (action) => {
  const conversationId = activeConversationId.value, run = ensureChatRun(conversationId);
  const approval = run?.approval;
  if (!approval || run.approvalBusy || run.sending || run.inspectionLoading) return;
  if (Date.now() >= approval.expiresAt * 1000) { await refreshInspection(conversationId, run.requestId); return; }
  if (action === 'continue' ? !approval.canContinue : !approval.canDecide) return;
  const requestId = run.requestId;
  const identity = `${approval.runId}:${approval.callId}:${approval.checkpointSeq}:${action}`;
  run.approvalBusy = action;
  try {
    run.approvalKeys[identity] ||= createApprovalIdempotencyKey();
    const payload = approvalTicket(approval, run.approvalKeys[identity], action === 'continue' ? undefined : action);
    const response = await (action === 'continue' ? continueChatApproval(payload) : decideChatApproval(payload));
    if (!isLiveRun(conversationId, run) || run.requestId !== requestId) return;
    if (response?.code !== 200) throw new Error('审批操作未确认，请刷新状态后复核');
    await refreshInspection(conversationId, requestId);
    if (!isLiveRun(conversationId, run)) return;
    if (action === 'continue' && response.data?.status === 'running') {
      run.lastEventId = continuationCursor(requestId, run.lastEventId, response.data.afterEventId);
      run.resumeRequestId = requestId; run.pendingSubmission = null;
      await resumeChatStream(conversationId);
    } else if (action === 'reject' && isActiveConversation(conversationId)) {
      run.terminal = { status: 'failed', reason: 'approval_rejected' };
      await loadChatHistory(conversationId, 1);
    }
  } catch (error) {
    if (isLiveRun(conversationId, run) && run.requestId === requestId) {
      await refreshInspection(conversationId, requestId);
      run.inspectionError = error.message || '操作结果待确认，请刷新状态后复核；未自动重复执行';
    }
  } finally { run.approvalBusy = ''; }
};
const disarmStopSend = (conversationId = activeConversationId.value) => {
  const run = ensureChatRun(conversationId);
  if (!run) return;
  run.canStopSend = false;
  if (run.stopArmTimer) {
    clearTimeout(run.stopArmTimer);
    run.stopArmTimer = null;
  }
};
const armStopSend = (conversationId = activeConversationId.value) => {
  const run = ensureChatRun(conversationId);
  if (!run) return;
  disarmStopSend(conversationId);
  run.stopArmTimer = setTimeout(() => {
    run.canStopSend = true;
  }, 1200);
};
const messagesContainer = ref(null);
const { followLatest, hasUnreadUpdates, scrollToBottom, jumpToLatest, jumpToTurn,
  handleScroll, captureReadingPosition, restoreReadingPosition } = useChatScroll({
  container: messagesContainer, conversationId: activeConversationId,
  isActiveConversation: id => isActiveConversation(id),
  onNearTop: () => {
    if (!loadingMessages.value && hasMoreMessages.value) loadMoreMessages();
  },
});
const conversationTurns = computed(() => messages.value.filter(item => item.isSend === true).map(item => ({
  id: item.id,
  status: item.requestId === activeInspection.value.requestId && isSending.value ? activeExecution.value.label : '历史轮次',
  summary: String(item.content || '附件消息').replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').slice(0, 64),
})));

// 粒子背景样式预计算（避免每次渲染 Math.random() 触发 Vue 重算）
const particleStyles = Array.from({ length: 30 }, () => ({
  left: (Math.random() * 100) + '%',
  top: (Math.random() * 100) + '%',
  animationDelay: (Math.random() * 5) + 's',
  animationDuration: (3 + Math.random() * 6) + 's',
  width: (2 + Math.random() * 3) + 'px',
  height: (2 + Math.random() * 3) + 'px',
}));
const aimessage = bindActiveField(activeConversationId, 'aimessage', '');
const aimessage2 = bindActiveField(activeConversationId, 'aimessage2', '');
const aIToolsContentMsg = bindActiveField(activeConversationId, 'aIToolsContentMsg', '');
const aIReasoningContentMsg = bindActiveField(activeConversationId, 'aIReasoningContentMsg', '');
const reasoningActiveKey = ref(['reasoning']);
const toolsActiveKey = ref(['tools']);
const currentReceivingMsgId = bindActiveField(activeConversationId, 'currentReceivingMsgId', null);
const expandedReasoning = ref(false);
const expandedTools = ref(false);
const lastSentMessage = bindActiveField(activeConversationId, 'lastSentMessage', '');
const lastSentMessageId = bindActiveField(activeConversationId, 'lastSentMessageId', null);
let abortController = null;

// 文件上传相关
const uploadedFileList = ref([]);
const pendingFileNames = ref([]);
const pendingFileUrls = ref([]);
const fileBackups = ref([]);
const fileUploadRef = ref(null);

// 自动收起标志位
const reasoningAutoCollapsed = ref(false);
const toolsAutoCollapsed = ref(false);

// 监听思考过程内容长度
watch(aIReasoningContentMsg, (newVal, oldVal) => {
  if (!newVal) return;
  // 内容首次出现且不超过300字，自动展开
  if (!oldVal) {
    reasoningActiveKey.value = ['reasoning'];
    reasoningAutoCollapsed.value=false;
  }
  // 内容超过300字且之前没超过，自动收起一次
});

// 监听工具调用内容长度
watch(aIToolsContentMsg, (newVal, oldVal) => {
  if (!newVal) return;
  // 内容首次出现且不超过350字，自动展开
  if (!oldVal) {
    toolsActiveKey.value = ['tools'];
    toolsAutoCollapsed.value=false;
  }
  // 内容超过300字且之前没超过，自动收起一次
});

const detailModalVisible = ref(false);
const detailModalContent = ref("");
const detailModalTitle = ref("");
// 添加联网搜索开关变量
const isOnlineSearch = ref(false); // 默认为关闭状态
const chatCapabilities = reactive({ attachments: false, onlineSearch: false, skillExecution: false, mcpExecution: false });
const experienceFileInput = ref(null);
const experienceAttachmentBusy = ref(false);
const experienceAttachmentsAllowed = computed(() => experience?.session?.mode === 'real'
  && experience.session.allow_chat_attachments === true && experience.session.usablePages?.includes('chat'));
const experienceSearchAllowed = computed(() => experience?.session?.mode === 'real'
  && experience.session.allow_web_search === true && experience.session.web_search_available === true
  && experience.session.usablePages?.includes('chat'));
const composerAttachments = computed(() => experience ? experience.attachments || [] : uploadedFileList.value);
const composerOnlineSearch = computed({
  get: () => experience ? experienceSearchAllowed.value && experience.webSearch : isOnlineSearch.value,
  set: value => {
    if (experience) { if (experienceSearchAllowed.value && !experience.busy) experience.webSearch = value; }
    else isOnlineSearch.value = value;
  },
});
const uploadExperienceAttachment = async event => {
  const input = event.target, file = input.files?.[0];
  if (!file || !experienceAttachmentsAllowed.value || experience.busy || experienceAttachmentBusy.value) { input.value = ''; return; }
  experienceAttachmentBusy.value = true;
  try { await experience.uploadAttachment(file); }
  catch (error) { message.error(error.message || '附件上传失败'); }
  finally { experienceAttachmentBusy.value = false; input.value = ''; }
};
const removeComposerAttachment = async (file, index) => {
  if (!experience) { removeUploadedFile(index); return; }
  if (!experienceAttachmentsAllowed.value || experience.busy || experienceAttachmentBusy.value) return;
  experienceAttachmentBusy.value = true;
  try { await experience.removeAttachment(file.id); }
  catch (error) { message.error(error.message || '附件移除失败'); }
  finally { experienceAttachmentBusy.value = false; }
};
const downloadChatFile = async (file) => {
  try {
    const { blob, metadata } = await loadPrivateFile(file, { maxBytes: 64 * 1024 * 1024 });
    savePrivateBlob(blob, metadata.name);
  } catch (error) { message.error(error.message || '认证下载失败'); }
};

// 语音模式相关变量
const { isVoiceMode, showTextInVoiceMode, isRecording, isRecognizing, recognizedPreviewText, voiceSentHint, pendingStopTimer, recognition, asrProviderLabel, isSpeaking, currentSpeakingMsgId, voiceSpeed, speedOptions, setVoiceSpeed, isPhoneMode, phoneModeCollapsed, phoneSubtitleEnabled, phoneMessages, phoneSubtitlePanel, phoneCallDuration, phoneStatusText, startRecording, stopRecording, cancelRecording, enterPhoneMode, stopPhoneCallTimer, formatPhoneCallDuration, stopPhoneSilenceTimer, sendPhoneMessage, hangUpPhone, staticSentenceList, staticSentenceIndex, staticPlaybackMsgId, staticPlaybackGen, playAIVoice, streamingTtsPlayedLength, streamingTtsActive, userStoppedSending, resetSpeakingState, stopSpeakingCheck, startStreamingTTS, streamTTS, finishStreamingTTS, phoneSubtitleScrollRaf } = useChatSpeech({
  messages, isSending, activeConversation, newMessage, lastSentMessage, scrollToBottom,
  sendMessage: (...args) => sendMessage(...args), stopMessage: (...args) => stopMessage(...args),
});

// 分页相关变量
const { currentPage, hasMoreMessages, loadingMessages, loadingHistoryPage, historyError,
  historyUnavailable, loadChatHistory, loadMoreMessages, invalidateHistory } = useChatHistory({
    activeConversationId, messages,
    isActiveConversation: (...args) => isActiveConversation(...args),
    captureReadingPosition, restoreReadingPosition, ensureChatRun, resetInspection,
    refreshInspection, refreshStatistics, loadErrorText,
  });

// 智能体相关
const aiApps = ref([]); // 存储智能体列表
const selectedAiApp = ref(null); // 存储选中的智能体

// 复制功能
const copyMessageContent = async (content) => {
  if (!content) {
    message.warning('没有可复制的内容');
    return;
  }

  try {
    // 优先使用 clipboard API
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(content);
      message.success('内容已复制到剪贴板');
    } else {
      // 降级方案：使用 textarea
      const textarea = document.createElement('textarea');
      textarea.value = content;
      textarea.style.position = 'fixed';
      textarea.style.left = '-9999px';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
      message.success('内容已复制到剪贴板');
    }
  } catch (err) {
    console.error('复制失败:', err);
    message.error('复制失败');
  }
};

// 将消息内容中的 URL 转换为可点击的链接
const formatMessageContent = (content) => {
  if (!content) return '';

  const urlRegex = /https?:\/\/[^\s<>"']+/gi;

  const escapeHtml = (str) =>
    str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  return content.replace(urlRegex, (url) => {
    let cleanUrl = url;

    // 1. 去掉末尾的 .,;:!? 和 Markdown 符号 * _ ~ `
    cleanUrl = cleanUrl.replace(/[.,;:!?*_~`]+$/, '');
    // 2. 循环去掉不成对的 ) ] } ' "
    while (/[)}\]'"`]$/.test(cleanUrl)) {
      const last = cleanUrl.slice(-1);
      const openMap = { ')': '(', ']': '[', '}': '{', "'": "'", '"': '"', '`': '`' };
      if (cleanUrl.includes(openMap[last])) break;
      cleanUrl = cleanUrl.slice(0, -1);
    }

    // 必须仍是 http(s) 开头
    if (!/^https?:\/\//i.test(cleanUrl)) return url;

    const safeUrl = escapeHtml(cleanUrl);

    const docContent = `<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><style>body{margin:0;font:14px/1.5 sans-serif;}a{color:#1a73e8;text-decoration:none;word-break:break-all;}a:hover{text-decoration:underline;}</style></head>
<body><a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${safeUrl}</a></body>
</html>`;

    const safeSrcdoc = escapeHtml(docContent);

    // 高度设为 auto，并设最小高度，避免截断
   return `<iframe srcdoc="${safeSrcdoc}" sandbox="allow-same-origin allow-popups" style="border:none;width:100%;height:1.6em;display:inline-block;vertical-align:middle;" title="URL 预览"></iframe>`;
  });
};

// 文件上传成功处理
const validateChatAttachment = (file) => {
  const files = uploadedFileList.value;
  if (chatCapabilities.attachmentMaxFiles && files.length >= chatCapabilities.attachmentMaxFiles) {
    throw new Error(`每轮最多 ${chatCapabilities.attachmentMaxFiles} 个附件`);
  }
  if (chatCapabilities.attachmentMaxBytes && files.reduce((total, item) => total + (item.size || 0), file.size) > chatCapabilities.attachmentMaxBytes) {
    throw new Error('本轮附件总大小超过预算，请减少附件');
  }
};
const handleFileUploadSuccess = (result) => {
  const context = result.uploadContext;
  const conversationId = String(context?.keyValue || '');
  if (!componentAlive || context?.business !== 'AIChat' || context?.sign !== 'chat') return;
  const run = ensureChatRun(conversationId);
  if (!run) return;
  const file = {
    id: result.fileId,
    name: result.fileName,
    size: result.fileSize,
    url: result.url,
    path: result.path
  };
  if (isActiveConversation(conversationId)) {
    if (!uploadedFileList.value.some(item => String(item.id) === String(file.id))) uploadedFileList.value.push(file);
    run.draftFiles = uploadedFileList.value.map(item => ({ ...item }));
    pendingFileNames.value = uploadedFileList.value.map(item => item.name);
    pendingFileUrls.value = uploadedFileList.value.map(item => item.url);
    const uploader = fileUploadRef.value;
    nextTick(() => {
      if (isActiveConversation(conversationId) && fileUploadRef.value === uploader) uploader?.clearUploadedFiles();
    });
  } else if (!run.draftFiles.some(item => String(item.id) === String(file.id))) {
    run.draftFiles.push(file);
  }
  message.success('文件上传成功');
};

// 文件上传失败处理
const handleFileUploadError = (error) => {
  console.error('文件上传失败:', error);
  message.error(error?.error?.message || '文件上传失败');
};

// 移除已上传文件
const removeUploadedFile = (index) => {
  uploadedFileList.value.splice(index, 1);
  pendingFileNames.value.splice(index, 1);
  pendingFileUrls.value.splice(index, 1);
};

// 清空文件列表
const clearUploadedFiles = () => {
  fileBackups.value = [...uploadedFileList.value];
  uploadedFileList.value = [];
  pendingFileNames.value = [];
  pendingFileUrls.value = [];
};

// 恢复文件列表（中止时）
const restoreUploadedFiles = () => {
  if (fileBackups.value.length > 0) {
    uploadedFileList.value = [...fileBackups.value];
    pendingFileNames.value = uploadedFileList.value.map(f => f.name);
    pendingFileUrls.value = uploadedFileList.value.map(f => f.url);
    fileBackups.value = [];
  }
};

// 获取对话列表
let conversationLoadSeq = 0;
const loadConversations = async (options = {}) => {
  if (props.embedded || !componentAlive) return;
  const seq = ++conversationLoadSeq;
  if (!options.silent) loadingConversations.value = true;
  try {
    const response = await getAIChatsMyPageData({
      pageNum: 1,
      pageSize: 200,
    });
    if (!componentAlive || seq !== conversationLoadSeq) return;

    if (response && response.code === 200 && response.data) {
      conversationError.value = '';
      conversations.value = (response.data.data || []).filter(item => !deletedConversationIds.has(String(item.id))).map((item) => ({
        id: item.id,
        title: item.name,
        lastMessage: item.lastMessage,
        summary: item.labelSummary || '',
        createdAt: item.createTime,
        updatedAt: item.updateTime,
        appId: item.appId == null || item.appId === "" ? item.appId : String(item.appId),
      }));
      labelManager.value?.refresh().catch(() => {});
      const preferredId = options.preferredId;
      const match = preferredId != null
        ? conversations.value.find((item) => String(item.id) === String(preferredId))
        : null;
      const sameActive = (item) => item && String(item.id) === String(activeConversationId.value);
      if (match) {
        if (options.keepActive && sameActive(match)) {
          activeConversation.value = match;
        } else {
          await selectConversation(match);
        }
      } else if (options.selectFirst && conversations.value[0]) {
        await selectConversation(conversations.value[0]);
      } else if (!activeConversationId.value && conversations.value[0]) {
        await selectConversation(conversations.value[0]);
      } else if (activeConversationId.value) {
        const current = conversations.value.find((item) => String(item.id) === String(activeConversationId.value));
        if (current) {
          activeConversation.value = current;
        }
      }
    } else {
      throw new Error(response?.msg || "获取对话列表失败");
    }
  } catch (error) {
    if (seq !== conversationLoadSeq) return;
    console.error("加载对话列表失败:", error);
    conversationError.value = loadErrorText(error, '会话列表加载失败');
  } finally {
    if (seq === conversationLoadSeq) loadingConversations.value = false;
  }
};

// 加载智能体列表
const loadAIApps = async () => {
  try {
    const response = await getMyAIAppsALLList();
    if (!componentAlive) return false;
    if (response && response.code === 200 && response.data) {
      // 格式化数据以适应Select组件
      aiApps.value = response.data.map((app) => ({
        label: app.name,
        value: String(app.id),
      }));
      return true;
    }
  } catch (error) {
    console.error("加载智能体列表失败:", error);
    message.error("加载智能体列表失败");
  }
  return false;
};
const loadTrialAgent = async () => {
  trialError.value = ''; trialReady.value = false;
  const loaded = await loadAIApps();
  if (!componentAlive) return;
  trialReady.value = loaded && aiApps.value.some((app) => app.value === props.agentId);
  if (!trialReady.value) trialError.value = '当前智能体不可用或访问权限不足，请重试核对';
};
const startEmbeddedTrial = async () => {
  if (!props.embedded || !trialReady.value || trialStarting.value || activeConversationId.value) return;
  trialStarting.value = true; trialError.value = '';
  try {
    await loadTrialAgent();
    if (!componentAlive || !trialReady.value) return;
    await createNewConversation(props.agentId);
  } catch (error) { if (componentAlive) trialError.value = `试用会话创建失败：${error.message || '请重试'}`; }
  finally { if (componentAlive) trialStarting.value = false; }
};

// 显示智能体选择模态框
const showAgentSelectionModal = (defaultAppId = null) => {
  if (props.embedded) return;
  // 设置默认选中的智能体：优先使用传入的appId，其次使用当前对话的appId，最后使用第一个智能体
  const pickedAppId = defaultAppId || activeConversation.value?.appId || aiApps.value[0]?.value;
  selectedAiApp.value = pickedAppId == null || pickedAppId === "" ? null : String(pickedAppId);

  // 创建模态框
  const modal = confirm({
    title: "选择智能体",
    content: () =>
      h("div", { class: "agent-selection-modal" }, [
        h(NSelect, {
          placeholder: "请选择智能体",
          options: aiApps.value,
          value: selectedAiApp.value,
          "onUpdate:value": (value) => {
            selectedAiApp.value = value;
          },
          style: { width: "100%" },
        }),
      ]),
    okText: "确认",
    cancelText: "取消",
    onOk: async () => {
      if (!selectedAiApp.value) {
        message.warning("请选择智能体");
        return Promise.reject();
      }

      // 创建新对话
      let loadingMsg;
      try {
        // 禁用取消按钮和确认按钮，防止重复点击
        modal.negativeButtonProps = { disabled: true };
        modal.positiveButtonProps = { disabled: true };

        // 显示加载提示
        loadingMsg = message.loading('正在创建对话...', { duration: 0 });

        // 执行创建对话操作
        await createNewConversation(selectedAiApp.value);

        // 关闭加载提示
        loadingMsg.destroy();

        // 成功后关闭模态框
        return Promise.resolve();
      } catch (error) {
        // 关闭加载提示
        loadingMsg?.destroy();

        // 重新启用按钮
        modal.negativeButtonProps = { disabled: false };
        modal.positiveButtonProps = { disabled: false };

        message.error("创建对话失败: " + (error.message || error));
        return Promise.reject();
      }
    },
  });
};

// 创建新对话
const createNewConversation = async (appId) => {
  persistActiveRunBuffers();
  try {
    const snowflakeId = (await GetSnowflakeId()).data;
    if (!componentAlive) return;
    const response = await addAIChats({
      id: snowflakeId,
      appId: appId,
      name: "新对话",
      lastMessage: "",
    });
    if (!componentAlive) return;

    if (response && response.code === 200 && response.data) {
      const created = {
        id: snowflakeId,
        title: "新对话",
        lastMessage: "",
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        appId: appId == null || appId === "" ? appId : String(appId),
      };
      conversations.value = [
        created,
        ...conversations.value.filter((item) => String(item.id) !== String(snowflakeId)),
      ];
      await selectConversation(created);
      loadConversations({ preferredId: snowflakeId, keepActive: true, silent: true });
    } else {
      throw new Error(response?.msg || "创建对话失败");
    }
  } catch (error) {
    console.error("创建对话失败:", error);
    message.error("创建对话失败: " + (error.message || error));
    throw error;
  }
};

// 获取智能体名称
const getAiAppName = (appId) => {
  if (!appId) return "";
  const aiApp = aiApps.value.find((app) => app.value === appId);
  return aiApp ? aiApp.label : "";
};

// 选择对话
const selectConversation = async (conversation) => {
  if (experience) { closeConversations(); return; }
  if (!conversation || deletedConversationIds.has(String(conversation.id))) return;
  if (!componentAlive || (props.embedded && String(conversation.appId) !== props.agentId)) return;
  closeConversations();
  if (String(activeConversationId.value) === String(conversation.id)) {
    activeConversation.value = conversation;
    const currentRun = ensureChatRun(conversation.id);
    if (!currentRun?.sending) {
      await loadChatHistory(conversation.id, 1);
    }
    return;
  }
  persistActiveRunBuffers();
  activeConversationId.value = conversation.id;
  followLatest.value = true;
  hasUnreadUpdates.value = false;
  activeConversation.value = conversation;
  historyError.value = null;
  // 切到运行中会话也要使上一会话的历史请求失效。
  invalidateHistory();
  loadingMessages.value = false;

  currentPage.value = 1;
  hasMoreMessages.value = true;
  messages.value = [];
  restoreRunBuffers(conversation.id);
  const run = ensureChatRun(conversation.id);
  if (run?.sending && run.messagesSnapshot?.length) {
    messages.value = run.messagesSnapshot.slice();
    currentPage.value = run.historyPage;
    hasMoreMessages.value = run.historyHasMore;
  } else {
    await loadChatHistory(conversation.id, 1);
  }

  nextTick(() => {
    if (isActiveConversation(conversation.id)) scrollToBottom();
  });
};

// 处理回车键按下
const handlePressEnter = (e) => {
  if (e.isComposing || e.keyCode === 229) return;
  // 如果按下了 Shift+Enter，则允许换行
  if (e.shiftKey) {
    return; // 继续执行默认的换行行为
  }

  // 阻止默认行为（换行）
  e.preventDefault();
  // 调用发送消息方法
  sendMessage();
};

// 发送消息
const sendRunCancellation = async (conversationId, run) => {
  try {
    await cancelRunRequest(run, cancelAIChatRequest);
  } catch (error) {
    if (isLiveRun(conversationId, run)) {
      run.stopRequested = false;
      if (run.sending) armStopSend(conversationId);
      message.warning(error.message || '停止指令未确认，请恢复原请求连接核对后台状态');
    }
  }
};

const sendMessage = async () => {
  if (reasoningBusy.value) {
    message.info('正在同步思考设置，请稍后发送');
    return;
  }
  if (experience) {
    if (!newMessage.value.trim() || experience.busy || experienceAttachmentBusy.value) return;
    const text = newMessage.value.trim(); newMessage.value = '';
    await experience.send(text, {
      attachment_ids: experienceAttachmentsAllowed.value ? experience.attachments.map(file => file.id) : [],
      web_search: experienceSearchAllowed.value && experience.webSearch,
    });
    return;
  }
  const conversationId = activeConversationId.value;
  if (!conversationId || !newMessage.value.trim()) return;
  if (chatRuns[String(conversationId)]?.agentSwitchBusy) {
    message.warning('智能体切换中，请稍后发送，草稿已保留');
    return;
  }
  if (historyUnavailable.value) {
    message.warning('请先完成聊天记录加载，再发送消息');
    return;
  }
  const run = ensureChatRun(conversationId);
  if (!run) return;
  if (run.approvalBusy || (run.approval && run.approval.status !== 'expired'
      && Date.now() < run.approval.expiresAt * 1000)) {
    message.warning('请先处理当前工具审批，草稿已保留');
    return;
  }
  if (run.cancelBusy || (run.resumeRequestId && !run.sending)) {
    message.warning('原请求结果待确认，请先恢复原请求连接，不要重复发送');
    return;
  }
  if (run.sending) {
    run.queued = { text: newMessage.value, files: uploadedFileList.value.map((file) => ({ ...file })) };
    message.info("当前会话正在生成，下一条将排队，草稿保留至请求接收");
    return;
  }
  if ((isVoiceMode.value || isPhoneMode.value) && parallelRunCount.value > 0) {
    message.warning("语音/电话模式同时只能生成一路");
    return;
  }
  if (parallelRunCount.value >= MAX_PARALLEL_CHAT_RUNS) {
    message.warning("最多同时进行 5 路对话生成，请等待其中一路完成");
    return;
  }
  const messageToSend = newMessage.value.trim();
  run.stopRequested = false;
  const draftText = newMessage.value;
  const draftFiles = uploadedFileList.value.map((file) => ({ ...file }));
  const currentFileNames = draftFiles.map((file) => file.name);
  const currentFileUrls = draftFiles.map((file) => file.url);
  const currentFileIds = draftFiles.map((file) => file.id);
  const sameFiles = (files) => files.map((file) => String(file.id)).join(',') === currentFileIds.map(String).join(',');
  let snowflakeId = null;
  let accepted = false;
  const conversationSnapshot = activeConversation.value;
  run.draftText = draftText;
  run.draftFiles = draftFiles;
  run.sending = true;
  run.startedAt = Date.now();
  run.stages = [];
  run.connectionState = 'connecting';
  run.terminal = null;
  run.reasoningConfig = null;
  run.webCitations = [];
  try {
  armStopSend(conversationId);
  userStoppedSending.value = false;
  run.userStopped = false;
  expandedReasoning.value = false;
  expandedTools.value = false;
  currentReceivingMsgId.value = null;
  run.currentReceivingMsgId = null;
  aimessage.value='正在思考....'
  aimessage2.value='';
  aIToolsContentMsg.value='';
  aIReasoningContentMsg.value='';
  run.aimessage = aimessage.value;
  run.aimessage2 = '';
  run.aIToolsContentMsg = '';
  run.aIReasoningContentMsg = '';
  lastSentMessage.value = messageToSend;
  run.lastSentMessage = messageToSend;
  run.abortController = new AbortController();
  if (String(activeConversationId.value) === String(conversationId)) {
    abortController = run.abortController;
  }

    snowflakeId = (await GetSnowflakeId()).data;
    if (!/^[1-9]\d{0,18}$/.test(String(snowflakeId || ''))) throw new Error('消息 ID 获取失败，草稿已保留');
    if (!isLiveRun(conversationId, run) || run.userStopped || run.stopRequested || !run.abortController || run.abortController.signal.aborted) return;
    if (currentFileIds.some((id) => !/^[1-9]\d{0,18}$/.test(String(id ?? '')))) {
      throw new Error('附件尚未准备完成，草稿和附件已保留');
    }
    // 添加用户消息到列表
    const userMessage = {
      id: snowflakeId,
      conversationId,
      isSend: true,
      content: messageToSend,
      fileNames: currentFileNames.join(','),
      contentFileUrls: currentFileUrls.join(','),
      fileIds: currentFileIds,
      attachments: draftFiles,
      totalTokenCount: 0,
      createdAt: new Date().toISOString(),
    };
    if (isActiveConversation(conversationId)) lastSentMessageId.value = userMessage.id;
    run.lastSentMessageId = userMessage.id;
     // 滚动到底部以显示最新内容
        if (isActiveConversation(conversationId)) scrollToBottom();
         //监听流式输出
    // 语音模式下：启动流式 TTS（边接收边播放）
    if (isVoiceMode.value) {
      startStreamingTTS();
    }
    var reulst;
    run.pendingSubmission = {
      id: snowflakeId, text: draftText, files: draftFiles, userMessage,
      payload: { aIChatsId: conversationId, id: snowflakeId, content: messageToSend,
        appId: conversationSnapshot?.appId, isOnlineSearch: isOnlineSearch.value, fileIds: currentFileIds },
    };
    try {
      run.submissionBusy = true;
      try { reulst = await addAIChatHistorys(run.pendingSubmission.payload, run.abortController.signal); }
      finally { run.submissionBusy = false; }
      if (!isLiveRun(conversationId, run)) return;
      if (reulst?.code !== 200 || !reulst?.data?.id || String(reulst.data.id) !== String(snowflakeId)) {
        throw new Error('请求尚未确认接收，草稿已保留');
      }
      const streamId = reulst.data.id;
      accepted = true;
      resetInspection(run, String(streamId));
      userMessage.requestId = String(streamId);
      run.resumeRequestId = String(streamId);
      run.lastEventId = '';
      if (run.draftText === draftText && sameFiles(run.draftFiles)) {
        run.draftText = '';
        run.draftFiles = [];
      }
      if (isActiveConversation(conversationId)) {
        if (newMessage.value === draftText && sameFiles(uploadedFileList.value)) {
          newMessage.value = '';
          uploadedFileList.value = [];
          pendingFileNames.value = [];
          pendingFileUrls.value = [];
        }
        labelManager.value?.refresh().catch(() => {});
        currentReceivingMsgId.value = streamId;
        messages.value.push(userMessage);
        run.messagesSnapshot = messages.value.slice();
      } else {
        run.messagesSnapshot = [...(run.messagesSnapshot || []), userMessage];
      }
      run.currentReceivingMsgId = streamId;
      if (run.stopRequested) await sendRunCancellation(conversationId, run);
      if (!isLiveRun(conversationId, run)) return;
      run.terminal = await getAiMySignalRHubMsg(streamId, conversationId);
      if (!isLiveRun(conversationId, run)) return;
      if (run.terminal?.status === 'uncertain') {
        message.warning('连接中断，已保留收到的内容；请恢复原请求连接');
        return;
      }
      acceptRunTerminal(run, run.terminal);
      await refreshInspection(conversationId, run.requestId);
      // 终态已持久化；后台任务可能稍后入队，再刷新以跟踪生成结果。
      labelManager.value?.refreshAfterTurn().catch(() => {});
      if (!isLiveRun(conversationId, run)) return;
      if (run.userStopped || ((isVoiceMode.value || isPhoneMode.value) && userStoppedSending.value)) {
        run.sending = false;
        return;
      }
    } catch (error) {
      if (!isLiveRun(conversationId, run)) return;
      const status = error?.cause?.response?.status ?? error?.response?.status;
      if (!accepted && run.pendingSubmission && (!status || status >= 500 || status === 409)) {
        run.resumeRequestId = String(snowflakeId);
        message.warning('请求接收结果待确认，草稿已保留；恢复连接将复用原 ID');
      } else if (!accepted) {
        run.pendingSubmission = null;
      }
      if (error.name === 'AbortError' || error.name === 'CanceledError' || error.message?.includes('cancel')) {
        message.info('已中止发送');
        stopAiMySignalRHubMsg(snowflakeId, conversationId);
        disarmStopSend(conversationId);
        run.sending = false;
        return;
      }
      const errorMsg = error.message || '';
      if (errorMsg.includes('聊天记录已达上限')) {
        confirm({
          title: '提示',
          content: '聊天记录已达上限，为了更好的体验，请新建对话？',
          okText: '新建对话',
          cancelText: '取消',
          onOk: () => {
            showAgentSelectionModal(activeConversation.value?.appId);
          }
        });
      } else {
        message.error(errorMsg || "发送失败");
      }
      stopAiMySignalRHubMsg(snowflakeId, conversationId);
      disarmStopSend(conversationId);
      run.sending = false;
      if (accepted) message.warning('请求已接收，连接中断；请恢复原请求连接，不要重新发送');
      return;
    }
    if (isActiveConversation(conversationId)) scrollToBottom();
    let stillActive = isActiveConversation(conversationId);
    if (stillActive) {
      lastSentMessage.value = "";
      lastSentMessageId.value = null;
    }
    run.lastSentMessage = "";
    run.lastSentMessageId = null;
    const conversation = conversations.value.find(
      (c) => String(c.id) === String(conversationId)
    );
    if (conversation) {
      if (!conversation.summary) conversation.lastMessage = messageToSend;
      conversation.updatedAt = new Date().toISOString();
      if (!conversation.title || conversation.title === '新对话') {
        conversation.title =
          messageToSend.substring(0, 20) + (messageToSend.length > 20 ? "..." : "");
      }
    }
    const streamedText = stillActive ? aimessage2.value : run.aimessage2;
    const streamedReasoning = stillActive ? aIReasoningContentMsg.value : run.aIReasoningContentMsg;
    const streamedTools = stillActive ? aIToolsContentMsg.value : run.aIToolsContentMsg;
    const localUserMessage = stillActive
      ? (messages.value.find((item) => item.id === lastSentMessageId.value)
        || messages.value.filter((item) => item.isSend).slice(-1)[0])
      : null;
    const assistantId = (stillActive ? currentReceivingMsgId.value : run.currentReceivingMsgId) || snowflakeId;
    stopAiMySignalRHubMsg(snowflakeId, conversationId);
    disarmStopSend(conversationId);
    if (stillActive) {
      loadingMessages.value = false;
      await loadChatHistory(conversationId, 1);
      if (!isLiveRun(conversationId, run)) return;
      stillActive = isActiveConversation(conversationId);
    }
    const hasCurrentUser = messages.value.some((item) => {
      if (!item.isSend) return false;
      if (localUserMessage && String(item.id) === String(localUserMessage.id)) return true;
      return String(item.content || '').trim() === messageToSend;
    });
    if (stillActive && !hasCurrentUser && localUserMessage) {
      messages.value = [...messages.value, localUserMessage];
    }
    const hasCurrentAssistant = messages.value.some((item) => {
      if (item.isSend !== false) return false;
      if (assistantId && String(item.id) === String(assistantId)) return true;
      return Boolean(streamedText) && String(item.content || '').trim() === String(streamedText).trim();
    });
    const assistantMessage = {
      id: assistantId || Date.now(),
      requestId: run.requestId,
      conversationId,
      isSend: false,
      content: streamedText || '',
      status: run.terminal?.status || 'failed',
      tokenUsage: { partial: run.terminal?.partial === true, reason: run.terminal?.reason },
      citations: run.webCitations || [],
      aiReasoningContent: streamedReasoning || '',
      aiToolsContent: streamedTools || '',
      createdAt: new Date().toISOString(),
      totalTokenCount: 0,
    };
    if (stillActive && (streamedText || streamedReasoning || streamedTools || run.terminal?.status === 'failed') && !hasCurrentAssistant) {
      messages.value.push(assistantMessage);
      run.messagesSnapshot = messages.value.slice();
    } else if (!stillActive && (streamedText || streamedReasoning || streamedTools)) {
      run.messagesSnapshot = [...(run.messagesSnapshot || []), assistantMessage];
    }
    const lastAiMsg = stillActive ? messages.value.filter((item) => !item.isSend).slice(-1)[0] : null;
    if (stillActive) scrollToBottom();
    if (stillActive && isVoiceMode.value) {
      finishStreamingTTS(streamedText, lastAiMsg?.id);
    }
  } catch (error) {
    if (!isLiveRun(conversationId, run)) return;
    if (error.name === 'AbortError' || error.name === 'CanceledError' || error.message?.includes('cancel')) {
      message.info('已中止发送');
      stopAiMySignalRHubMsg(snowflakeId, conversationId);
      disarmStopSend(conversationId);
      run.sending = false;
      return;
    }
    stopAiMySignalRHubMsg(snowflakeId, conversationId);
    message.error(error.message || "发送消息失败，草稿已保留");
    disarmStopSend(conversationId);
    run.sending = false;
    if (accepted) message.warning('请求已接收，请恢复原请求连接，不要重新发送');
  } finally {
    disarmStopSend(conversationId);
    run.sending = false;
    run.abortController = null;
    const queued = run.queued;
    run.queued = null;
    if (run.terminal?.status === 'completed' && queued && isActiveConversation(conversationId)
        && newMessage.value === queued.text
        && uploadedFileList.value.map((file) => String(file.id)).join(',') === queued.files.map((file) => String(file.id)).join(',')) {
      queueMicrotask(() => { if (isActiveConversation(conversationId) && !run.sending) sendMessage(); });
    }
  }
};

const resumeChatStream = async (conversationId = activeConversationId.value) => {
  // 模板事件参数不得被误当作会话 ID；异步续跑显式传入绑定的会话。
  if (typeof conversationId === 'object') conversationId = activeConversationId.value;
  const run = ensureChatRun(conversationId);
  const requestId = run?.resumeRequestId;
  if (!requestId || run.sending || run.cancelBusy || run.agentSwitchBusy) return;
  run.sending = true;
  run.userStopped = false;
  run.abortController = new AbortController();
  armStopSend(conversationId);
  try {
    try {
      const response = await http.post('/api/AIChatHistorys/Resume', null, { params: { requestId }, timeout: 30000,
        signal: run.abortController.signal });
      if (!isLiveRun(conversationId, run)) return;
      run.lastEventId = continuationCursor(requestId, run.lastEventId, response.data?.afterEventId);
    } catch (error) {
      if (!isLiveRun(conversationId, run)) return;
      const status = error?.cause?.response?.status ?? error?.response?.status;
      if (status !== 404 || !run.pendingSubmission || run.userStopped || run.stopRequested || !run.abortController) throw error;
      // 请求可能已提交而确认响应丢失；相同 ID/内容重试，由服务端幂等去重。
      run.submissionBusy = true;
      let result;
      try { result = await addAIChatHistorys(run.pendingSubmission.payload, run.abortController.signal); }
      finally { run.submissionBusy = false; }
      if (!isLiveRun(conversationId, run)) return;
      if (result?.code !== 200 || String(result.data?.id) !== String(requestId)) {
        throw new Error('原请求尚未确认，草稿继续保留');
      }
    }
    const submitted = run.pendingSubmission;
    if (submitted) {
      const matches = (files) => files.map((f) => String(f.id)).join(',') === submitted.files.map((f) => String(f.id)).join(',');
      if (run.draftText === submitted.text && matches(run.draftFiles)) {
        run.draftText = ''; run.draftFiles = [];
      }
      if (isActiveConversation(conversationId) && newMessage.value === submitted.text && matches(uploadedFileList.value)) {
        newMessage.value = ''; uploadedFileList.value = []; pendingFileNames.value = []; pendingFileUrls.value = [];
      }
    }
    run.currentReceivingMsgId = requestId;
    if (run.userStopped) return;
    if (run.stopRequested) await sendRunCancellation(conversationId, run);
    if (!isLiveRun(conversationId, run)) return;
    run.terminal = await getAiMySignalRHubMsg(requestId, conversationId);
    if (!isLiveRun(conversationId, run)) return;
    // 非活动会话尚未载入展示记录，保留缓冲与原请求身份供切回后恢复。
    if (isConfirmedTerminal(run.terminal) && !isActiveConversation(conversationId)
        && hasUnconfirmedContent(run)) {
      await refreshInspection(conversationId, requestId);
      return;
    }
    if (isActiveConversation(conversationId) && isConfirmedTerminal(run.terminal)) {
      await loadChatHistory(conversationId, 1);
      if (!isLiveRun(conversationId, run)) return;
      const restored = messages.value.some(item => item.isSend === false
        && String(item.requestId) === String(requestId) && !item.eventHistoryError);
      if (historyError.value || (!restored && hasUnconfirmedContent(run))) {
        message.warning('后台已返回终态，但历史内容未完整恢复；保留当前内容，请重试原请求连接');
        return;
      }
    }
    acceptRunTerminal(run, run.terminal);
    await refreshInspection(conversationId, requestId);
    if (isConfirmedTerminal(run.terminal)) {
      stopAiMySignalRHubMsg(requestId, conversationId);
    }
  } catch (error) { if (isLiveRun(conversationId, run)) message.error(error.message || '恢复连接失败，原请求保留'); }
  finally { run.sending = false; run.abortController = null; disarmStopSend(conversationId); }
};

const showDetailModal = (title, content) => {
  detailModalTitle.value = title;
  detailModalContent.value = content;
  detailModalVisible.value = true;
};

const truncateContent = (content, maxLength = 350) => {
  if (!content || content.length <= maxLength) return content;
  return content.substring(0, maxLength) + '...';
};

// 获取日志类型名称
const getLogTypeName = (logType) => {
  const types = {
    1: '知识库',
    2: '网络搜索',
    3: '系统提示词',
    4: '文件内容'
  };
  return Object.hasOwn(types, logType) ? types[logType] : '未知';
};

var connectionServer=null;
let connectionTimeout = null;

const isActiveConversation = (conversationId) => componentAlive && !deletedConversationIds.has(String(conversationId))
  && String(activeConversationId.value) === String(conversationId);

const getAiMySignalRHubMsg=(id, conversationId = activeConversationId.value)=>{
  const run = ensureChatRun(conversationId);
  if (!run || !isLiveRun(conversationId, run)) return Promise.resolve({ status: 'cancelled' });
  return new Promise((resolve) => {
    const connection = startChatStream(id, {
      citations: (_text, payload) => {
        if (isLiveRun(conversationId, run)) run.webCitations = payload.sources || [];
      },
      reasoningConfig: (_text, payload) => {
        if (isLiveRun(conversationId, run)) run.reasoningConfig = payload;
      },
      aimsg: (msg) => {
        if (run) run.aimessage2 += msg;
        if (isActiveConversation(conversationId)) {
          nextTick(() => {
            if (!isActiveConversation(conversationId) || !isLiveRun(conversationId, run)) return;
            scrollToBottom();
            if (isVoiceMode.value && streamingTtsActive.value) {
              streamTTS(aimessage2.value);
            }
          });
        }
      },
      processmsg: (msg) => {
        if (run) {
          run.stages ||= [];
          if (run.stages.at(-1)?.text !== msg) run.stages.push({ text:msg, at:Date.now() });
          if (run.stages.length > 200) run.stages.shift();
        }
        if (run) run.aimessage = msg;
      },
      retrieval: (msg) => {
        if (run) {
          run.stages ||= [];
          run.stages.push({ text:msg, at:Date.now() });
          if (run.stages.length > 200) run.stages.shift();
        }
        if (run) run.aimessage = msg;
      },
      aIToolsContentMsg: (msg) => {
        if (run) run.aIToolsContentMsg += msg;
        if (isActiveConversation(conversationId)) {
          nextTick(() => {
            if (isActiveConversation(conversationId) && isLiveRun(conversationId, run)) scrollToBottom();
          });
        }
      },
      aIReasoningContentMsg: (msg) => {
        if (run) run.aIReasoningContentMsg += msg;
        if (isActiveConversation(conversationId)) {
          nextTick(() => {
            if (isActiveConversation(conversationId) && isLiveRun(conversationId, run)) scrollToBottom();
          });
        }
      },
      generationerror: (msg) => {
        if (run) run.aimessage = msg;
      },
      cursor: (eventId) => { run.lastEventId = eventId; },
      connection: (state) => { if (run) run.connectionState = state; },
      done: (terminal) => resolve(terminal),
      error: (terminal) => {
        if (run && !run.aimessage2) {
          run.aimessage = run.aimessage || '流式连接已中断';
        }
        resolve(terminal);
      },
    }, { lastEventId: run.lastEventId });
    if (run) run.connection = connection;
    if (isActiveConversation(conversationId)) connectionServer = connection;
  });
}

const stopAiMySignalRHubMsg=(id, conversationId = activeConversationId.value)=>{
  const run = ensureChatRun(conversationId);
  // 游标与已接收内容必须一起保留，否则重连跳过的旧事件会永久丢失。
  const preserveReceived = Boolean(run?.resumeRequestId);
  if (run) {
    if (!preserveReceived) {
      run.aimessage = '';
      run.aimessage2 = '';
      run.aIReasoningContentMsg = '';
      run.aIToolsContentMsg = '';
      run.currentReceivingMsgId = null;
    }
    run.connection?.close?.();
    run.connection = null;
  }
  if (isActiveConversation(conversationId)) {
    if (!preserveReceived) {
      aimessage.value='';
      aimessage2.value='';
      aIReasoningContentMsg.value='';
      aIToolsContentMsg.value='';
      expandedReasoning.value = false;
      expandedTools.value = false;
      currentReceivingMsgId.value = null;
    }
    connectionServer?.close?.();
    connectionServer = null;
  }
  if (connectionTimeout) {
    clearTimeout(connectionTimeout);
    connectionTimeout = null;
  }
}

const stopMessage = async () => {
  if (experience) { experience.abort(); return; }
  if (!isSending.value || !canStopSend.value) return;
  const conversationId = activeConversationId.value;
  const run = ensureChatRun(conversationId);
  if (!run || run.cancelBusy || run.stopRequested) return;
  // Add 确认前保留停止意图，接收原 ID 后取消；不通过断开订阅假装已停止。
  run.stopRequested = true;
  run.queued = null;
  // 停止流式 TTS
  streamingTtsActive.value = false;
  streamingTtsPlayedLength.value = 0;
  staticPlaybackGen.value++; // 使旧回调失效
  staticPlaybackMsgId.value = null;
  staticSentenceList.value = [];
  staticSentenceIndex.value = 0;
  window.speechSynthesis.cancel();
  resetSpeakingState();
  disarmStopSend();
  if (run.resumeRequestId) await sendRunCancellation(conversationId, run);
}


// 格式化日期
const formatDate = (dateString) => {
  if (!dateString) return "";
  const date = new Date(dateString);
  const now = new Date();

  const targetDate = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const currentDate = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diffTime = currentDate - targetDate;
  const diffDays = Math.floor(diffTime / (1000 * 60 * 60 * 24));

  if (diffDays === 0) {
    return "今天";
  } else if (diffDays === 1) {
    return "昨天";
  } else if (diffDays <= 7) {
    return `${diffDays}天前`;
  } else {
    return date.toLocaleDateString("zh-CN");
  }
}; 

const formatTime = (dateString) => {
  if (!dateString) return "";
  const date = new Date(dateString);
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
};

const formatTokenCount = (count) => {
  if (!count || count === 0) return "";
  if (count >= 10000) {
    return (count / 10000).toFixed(2) + "万";
  }
  return count.toString();
};

// 监听消息变化，自动滚动到底部
watch(messages, () => {
  scrollToBottom();
});

// 组件挂载时加载对话列表
const consumeQuickChatDraft = async () => {
  const raw = sessionStorage.getItem("eic-quick-chat");
  if (!raw) return;
  sessionStorage.removeItem("eic-quick-chat");
  let draft;
  try {
    draft = JSON.parse(raw);
  } catch {
    return;
  }
  const content = String(draft?.content || "").trim();
  if (!content) return;
  const matched = aiApps.value.find((app) => String(app.value) === String(draft.appId))
    || aiApps.value.find((app) => app.label === draft.appName)
    || aiApps.value[0];
  if (!matched) {
    message.warning("暂无可用智能体，请先在智能体管理中配置");
    return;
  }
  await createNewConversation(matched.value);
  newMessage.value = content;
  await sendMessage();
};

const consumeOpenChat = async () => {
  const raw = sessionStorage.getItem("eic-open-chat");
  if (!raw) return;
  sessionStorage.removeItem("eic-open-chat");
  let draft;
  try {
    draft = JSON.parse(raw);
  } catch {
    return;
  }
  const chatId = draft?.chatId || draft?.id;
  if (!chatId) return;
  await loadConversations({ preferredId: chatId });
};

const switchConversationAgent = async (appId) => {
  if (experience) {
    experience.selectAgent(appId);
    if (activeConversation.value) activeConversation.value.appId = appId;
    return;
  }
  if (props.embedded) return;
  if (!activeConversation.value || !appId) return;
  const target = activeConversation.value;
  const conversationId = String(target.id);
  const run = ensureChatRun(conversationId);
  if (!run || run.agentSwitchBusy || run.sending) return;
  const seq = run.agentSwitchSeq = (run.agentSwitchSeq || 0) + 1;
  run.agentSwitchBusy = true;
  try {
    const response = await updateAIChats({
      id: conversationId,
      appId,
      lastMessage: target.lastMessage || "",
    });
    if (!isLiveRun(conversationId, run) || run.agentSwitchSeq !== seq) return;
    if (response?.code !== 200 || response.isSuccess === false || response.data === false) throw new Error('切换未生效');
    const found = conversations.value.find((item) => String(item.id) === conversationId);
    if (found) found.appId = String(appId);
    if (isActiveConversation(conversationId)) activeConversation.value.appId = String(appId);
  } catch (error) {
    if (!isLiveRun(conversationId, run) || run.agentSwitchSeq !== seq) return;
    message.error("切换智能体失败: " + (error.message || error));
  } finally {
    if (run.agentSwitchSeq === seq) run.agentSwitchBusy = false;
  }
};

// 删除标记先于关闭连接，确保关闭事件及迟到响应不能重建目标 run。
const forgetConversation = (conversationId) => {
  const id = String(conversationId);
  const wasActive = String(activeConversationId.value) === id;
  deletedConversationIds.add(id);
  ++conversationLoadSeq;
  loadingConversations.value = false;
  const run = chatRuns[id];
  if (run) {
    run.userStopped = true;
    run.queued = null;
    run.inspectionSeq += 1;
    run.detailSeq += 1;
    clearTimeout(run.stopArmTimer);
    run.abortController?.abort();
    if (abortController === run.abortController) abortController = null;
    if (connectionServer === run.connection) connectionServer = null;
    run.connection?.close?.();
    run.connection = null;
    run.abortController = null;
    run.sending = false;
    delete chatRuns[id];
  }
  conversations.value = conversations.value.filter(item => String(item.id) !== id);
  if (!wasActive) return;
  invalidateHistory();
  invalidateStatistics();
  activeConversationId.value = null;
  activeConversation.value = null;
  messages.value = [];
  newMessage.value = '';
  uploadedFileList.value = [];
  pendingFileNames.value = [];
  pendingFileUrls.value = [];
  fileBackups.value = [];
  aimessage.value = ''; aimessage2.value = '';
  aIToolsContentMsg.value = ''; aIReasoningContentMsg.value = '';
  lastSentMessage.value = ''; lastSentMessageId.value = null;
  currentReceivingMsgId.value = null;
  historyError.value = null;
  loadingMessages.value = false;
  hasMoreMessages.value = false;
  currentPage.value = 1;
  fileUploadRef.value?.clearUploadedFiles();
};

const confirmClearAllConversations = () => {
  let phrase = "";
  confirm({
    title: "一键清除历史会话",
    content: () => h("div", [
      h("p", "将删除当前账号下的全部对话，且不可恢复。请输入「确认一键删除」："),
      h(NInput, {
        placeholder: "确认一键删除",
        "onUpdate:value": (value) => { phrase = value; },
      }),
    ]),
    okText: "删除",
    okType: "danger",
    cancelText: "取消",
    onOk: async () => {
      if (String(phrase).trim() !== "确认一键删除") {
        message.warning("请输入确认一键删除");
        return Promise.reject();
      }
      const response = await clearMyAIChats({ confirmText: "确认一键删除" });
      if (!componentAlive) return;
      if (response?.code !== 200 || response.isSuccess === false || response.data === false) throw new Error('清除未生效');
      ++conversationLoadSeq;
      loadingConversations.value = false;
      const ids = new Set([...Object.keys(chatRuns), ...conversations.value.map(item => String(item.id))]);
      if (activeConversationId.value) ids.add(String(activeConversationId.value));
      ids.forEach(forgetConversation);
      conversations.value = [];
      activeConversationId.value = null;
      activeConversation.value = null;
      messages.value = [];
      message.success("已清除当前账号的历史会话");
    },
  });
};

watch([isSending, approvalPending, trialStarting, activeConversationId, activeExecution], () => {
  if (props.embedded && componentAlive) emit('runtime-state', {
    busy: isSending.value || trialStarting.value,
    pending: approvalPending.value,
    conversationId: activeConversationId.value,
    status: activeExecution.value.label,
  });
}, { immediate: true });
onMounted(async () => {
  if (experience) {
    const id = 'experience-session';
    const agents = experience.catalog.agents || [];
    aiApps.value = agents.map(a => ({ label:a.name, value:a.id }));
    activeConversationId.value = id;
    activeConversation.value = { id, title: experience.session.mode === 'public' ? '公开演示对话' : '限时体验对话', appId:experience.agentId };
    conversations.value = [activeConversation.value];
    watch(() => experience.agentId, id => { if (activeConversation.value) activeConversation.value.appId = id; });
    hasMoreMessages.value = false;
    watch([() => experience.messages, () => experience.busy, () => experience.failed], ([rows, busy, failed]) => {
      messages.value = rows.map((row, index) => ({ id:`experience-${index}`, conversationId:id,
        isSend:row.role === 'user', content:row.content,
        status:row.role === 'assistant' && index === rows.length - 1 ? (busy ? 'running' : failed ? 'failed' : 'completed') : 'completed',
        fileNames:'', contentFileUrls:'', attachments:[], fileIds:[], aIChatHistorysBindLogs:[] }));
      nextTick(() => scrollToBottom());
    }, { deep:true, immediate:true });
    watch(() => experience.busy, sending => {
      const run = ensureChatRun(id);
      run.sending = sending; if (sending) run.startedAt = Date.now(); run.canStopSend = true;
      run.terminal = sending ? null : { status: experience.failed ? 'failed' : 'completed' };
    }, { immediate:true });
    return;
  }
  if (!props.embedded) {
    window.addEventListener("eic-quick-chat", consumeQuickChatDraft);
    window.addEventListener("eic-open-chat", consumeOpenChat);
  }
  try {
    const response = await http.get('/api/AIChatHistorys/Capabilities');
    if (response?.code === 200) {
      for (const key of ['attachments', 'onlineSearch', 'skillExecution', 'mcpExecution']) {
        chatCapabilities[key] = response.data?.[key] === true;
      }
      for (const key of ['attachmentFormats', 'attachmentMaxFiles', 'attachmentMaxBytes', 'attachmentMaxChars']) {
        chatCapabilities[key] = response.data?.[key];
      }
    }
  } catch { /* capability 未确认时保持禁用 */ }
  if (!componentAlive) return;
  if (props.embedded) {
    await loadTrialAgent();
  } else {
    await loadAIApps();
    if (!componentAlive) return;
    await loadConversations();
    if (!componentAlive) return;
    await consumeOpenChat();
    await consumeQuickChatDraft();
  }

});

// 组件卸载时移除事件监听器
onUnmounted(() => {
  componentAlive = false;
  invalidateStatistics();
  ++conversationLoadSeq;
  invalidateHistory();
  window.removeEventListener("eic-quick-chat", consumeQuickChatDraft);
  window.removeEventListener("eic-open-chat", consumeOpenChat);
  // 停止 SignalR 连接
  if (connectionTimeout) {
    clearTimeout(connectionTimeout);
    connectionTimeout = null;
  }
  connectionServer?.close?.();

});

// 删除对话
const deleteConversation = async (conversationId, event) => {
  event.stopPropagation(); // 阻止事件冒泡，避免触发选择对话

  try {
    await confirm({
      title: "确认删除",
      content: "确定要删除这个对话吗？此操作不可恢复。",
      okText: "确认",
      cancelText: "取消",
      onOk: async function () {
        const response = await deleteAIChats(conversationId);
        if (!componentAlive) return;
        if (response.code === 200 && response.isSuccess !== false && response.data !== false) {
          forgetConversation(conversationId);
          await loadConversations();
          message.success("删除成功");
        }
      },
    });
  } catch (error) {
    if (error?.message !== "取消") {
      console.error("删除对话失败:", error);
      message.error("删除对话失败: " + (error.message || error));
    }
  }
};
</script>

<style scoped src="../../css/chatWorkspace.css"></style>

<style scoped>
.conversation-navigation { display:flex; flex:0 1 auto; flex-wrap:wrap; align-items:flex-start; justify-content:space-between; gap:6px 12px; max-height:24dvh; overflow:auto; padding:4px 16px; border-bottom:1px solid var(--eic-line); font-size:var(--eic-font-meta); }
.conversation-navigation details { min-width:0; flex:1; }
.conversation-navigation summary { cursor:pointer; }
.conversation-turns { display:flex; flex-direction:column; max-height:200px; overflow:auto; gap:6px; padding-top:8px; }
.conversation-turns button { text-align:left; overflow-wrap:anywhere; background:transparent; color:inherit; border:0; padding:6px; cursor:pointer; }
.conversation-turns button:hover, .conversation-turns button:focus-visible { background:var(--eic-bg-secondary, #f1f5f9); }
.conversation-usage { display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; padding:0 4px 10px; color:var(--eic-text-secondary); font-size:var(--eic-font-meta); min-width:0; }
.conversation-usage strong { color:var(--eic-text); font-weight:600; font-variant-numeric:tabular-nums; }
.conversation-usage .n-button { margin-left:auto; width:28px; min-width:28px; }
.sidebar-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.agent-switcher {
  min-width: 0;
}
</style>
