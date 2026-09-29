import type { FC, ReactNode } from 'react'
import type { Theme } from '../embedded-chatbot/theme/theme'
import type { ChatConfig, ChatItem, OnFeedback, OnRegenerate, OnSend } from '../types'
import type { HumanInputFormSubmitData } from './answer/human-input-content/type'
import type { AnswerActionPosition } from './answer/operation'
import type { InputForm } from './type'
import type { SpeechToTextTarget } from '@/app/components/base/voice-input/types'
import type { HumanInputNodeType } from '@/app/components/workflow/nodes/human-input/types'
import type { Node } from '@/app/components/workflow/types'
import type { AppData, ToolIcon } from '@/models/share'
import { Button } from '@langgenius/dify-ui/button'
import { cn } from '@langgenius/dify-ui/cn'
import { queryOptions, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, memo } from 'react'
import { useTranslation } from 'react-i18next'
import { useShallow } from 'zustand/react/shallow'
import { useStore as useAppStore } from '@/app/components/app/store'
import { useChatWithHistoryContext } from '@/app/components/base/chat/chat-with-history/context'
import {
  deleteMessageContext,
  hasConsoleContextSession,
  messageContextList,
} from '@/service/message-context-extend'
import Answer from './answer'
import ChatInputArea from './chat-input-area'
import ChatLogModals from './chat-log-modals'
import { ChatContextProvider } from './context-provider'
import Question from './question'
import s from './style.module.css'
import TryToAsk from './try-to-ask'
import { useChatLayout } from './use-chat-layout'

const MessageContextMarker = ({
  conversationId,
  messageId,
  isResponding,
}: {
  conversationId: string
  messageId: string
  isResponding?: boolean
}) => {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const queryKey = ['message-context-extend', conversationId]
  const { data: contextList = [] } = useQuery(
    queryOptions({
      queryKey,
      queryFn: () => messageContextList({ conversation_id: conversationId }),
      enabled: !isResponding,
      retry: false,
      // Override the app's five-minute freshness window: finishing an answer can
      // create a new context boundary in the same conversation.
      staleTime: 0,
      gcTime: 0,
    }),
  )
  const { mutate, isPending } = useMutation({
    mutationFn: () =>
      deleteMessageContext({
        conversation_id: conversationId,
        message_id: messageId,
      }),
    onSuccess: async (result) => {
      if (result !== 'ok') return
      // Cancel earlier reads before updating this conversation, even if the user
      // has switched to another conversation while the deletion was in flight.
      await queryClient.cancelQueries({ queryKey, exact: true })
      queryClient.setQueryData<string[]>(queryKey, (previous) =>
        previous?.filter((id) => id !== messageId),
      )
      await queryClient.invalidateQueries({ queryKey, exact: true })
    },
  })

  if (!contextList.includes(messageId)) return null

  return (
    <button
      type="button"
      disabled={isPending || isResponding}
      onClick={() => mutate()}
      className={s.contextTag}
      aria-label={t(($) => $['configuration.restoreContext'], { ns: 'extend' })}
    >
      <span className={s.isCenter}>
        {t(($) => $['configuration.clearContext'], { ns: 'extend' })}
      </span>
      <span className={s.recover}>
        {t(($) => $['configuration.restoreContext'], { ns: 'extend' })}
      </span>
    </button>
  )
}

export type ChatProps = {
  answerActionPosition?: AnswerActionPosition
  isTryApp?: boolean
  readonly?: boolean
  appData?: AppData
  chatList: ChatItem[]
  config?: ChatConfig
  isResponding?: boolean
  noStopResponding?: boolean
  onStopResponding?: () => void
  noChatInput?: boolean
  showRegenerate?: boolean
  onSend?: OnSend
  inputs?: Record<string, unknown>
  inputsForm?: InputForm[]
  onRegenerate?: OnRegenerate
  chatContainerClassName?: string
  chatContainerInnerClassName?: string
  chatFooterClassName?: string
  chatFooterInnerClassName?: string
  suggestedQuestions?: string[]
  showPromptLog?: boolean
  questionIcon?: ReactNode
  answerIcon?: ReactNode
  allToolIcons?: Record<string, ToolIcon>
  onAnnotationEdited?: (question: string, answer: string, index: number) => void
  onAnnotationAdded?: (
    annotationId: string,
    authorName: string,
    question: string,
    answer: string,
    index: number,
  ) => void
  onAnnotationRemoved?: (index: number) => void
  chatNode?: ReactNode
  disableFeedback?: boolean
  onFeedback?: OnFeedback
  chatAnswerContainerInner?: string
  hideProcessDetail?: boolean
  hideLogModal?: boolean
  theme?: Theme
  switchSibling?: (siblingMessageId: string) => void
  showFeatureBar?: boolean
  showFileUpload?: boolean
  featureBarReadonly?: boolean
  onFeatureBarClick?: (state: boolean) => void
  noSpacing?: boolean
  inputDisabled?: boolean
  inputPlaceholder?: string
  inputPlaceholderBotName?: string
  sendButtonLabel?: string
  sendButtonLoading?: boolean
  footerNotice?: ReactNode
  footerNoticeTooltip?: ReactNode
  sidebarCollapseState?: boolean
  hideAvatar?: boolean
  sendOnEnter?: boolean
  speechToTextTarget?: SpeechToTextTarget
  onBeforeSpeechToText?: () => Promise<unknown>
  renderAgentContent?: (props: {
    item: ChatItem
    responding?: boolean
    content?: string
  }) => ReactNode
  onHumanInputFormSubmit?: (formToken: string, formData: HumanInputFormSubmitData) => Promise<void>
  getHumanInputNodeData?: (nodeID: string) => Node<HumanInputNodeType> | undefined
}

const Chat: FC<ChatProps> = ({
  answerActionPosition,
  isTryApp,
  readonly = false,
  appData,
  config,
  onSend,
  inputs,
  inputsForm,
  onRegenerate,
  chatList,
  isResponding,
  noStopResponding,
  onStopResponding,
  noChatInput,
  showRegenerate,
  chatContainerClassName,
  chatContainerInnerClassName,
  chatFooterClassName,
  chatFooterInnerClassName,
  suggestedQuestions,
  showPromptLog,
  questionIcon,
  answerIcon,
  onAnnotationAdded,
  onAnnotationEdited,
  onAnnotationRemoved,
  chatNode,
  disableFeedback,
  onFeedback,
  chatAnswerContainerInner,
  hideProcessDetail,
  hideLogModal,
  theme,
  switchSibling,
  showFeatureBar,
  showFileUpload,
  featureBarReadonly,
  onFeatureBarClick,
  noSpacing,
  inputDisabled,
  inputPlaceholder,
  inputPlaceholderBotName,
  sendButtonLabel,
  sendButtonLoading,
  footerNotice,
  footerNoticeTooltip,
  sidebarCollapseState,
  hideAvatar,
  sendOnEnter,
  speechToTextTarget,
  onBeforeSpeechToText,
  renderAgentContent,
  onHumanInputFormSubmit,
  getHumanInputNodeData,
}) => {
  const { t } = useTranslation()
  const { currentConversationId } = useChatWithHistoryContext()
  const canLoadMessageContext = !!currentConversationId && hasConsoleContextSession()
  const {
    currentLogItem,
    setCurrentLogItem,
    showPromptLogModal,
    setShowPromptLogModal,
    showAgentLogModal,
    setShowAgentLogModal,
  } = useAppStore(
    useShallow((state) => ({
      currentLogItem: state.currentLogItem,
      setCurrentLogItem: state.setCurrentLogItem,
      showPromptLogModal: state.showPromptLogModal,
      setShowPromptLogModal: state.setShowPromptLogModal,
      showAgentLogModal: state.showAgentLogModal,
      setShowAgentLogModal: state.setShowAgentLogModal,
    })),
  )
  const { width, chatContainerRef, chatContainerInnerRef, chatFooterRef, chatFooterInnerRef } =
    useChatLayout({
      chatList,
      sidebarCollapseState,
    })

  const hasTryToAsk =
    config?.suggested_questions_after_answer?.enabled && !!suggestedQuestions?.length && onSend

  return (
    <ChatContextProvider
      readonly={readonly}
      config={config}
      chatList={chatList}
      isResponding={isResponding}
      showPromptLog={showPromptLog}
      questionIcon={questionIcon}
      answerIcon={answerIcon}
      onSend={onSend}
      onRegenerate={onRegenerate}
      showRegenerate={showRegenerate}
      onAnnotationAdded={onAnnotationAdded}
      onAnnotationEdited={onAnnotationEdited}
      onAnnotationRemoved={onAnnotationRemoved}
      disableFeedback={disableFeedback}
      onFeedback={onFeedback}
      getHumanInputNodeData={getHumanInputNodeData}
    >
      <div data-testid="chat-root" className={cn('relative h-full', isTryApp && 'flex flex-col')}>
        <div
          data-testid="chat-container"
          ref={chatContainerRef}
          className={cn(
            'relative h-full overflow-x-hidden overflow-y-auto',
            isTryApp && 'h-0 grow',
            chatContainerClassName,
          )}
        >
          {chatNode}
          <div
            ref={chatContainerInnerRef}
            className={cn(
              'w-full',
              !noSpacing && 'px-8',
              chatContainerInnerClassName,
              isTryApp && 'px-0',
            )}
          >
            {chatList.map((item, index) => {
              if (item.isAnswer) {
                const isLast = item.id === chatList.at(-1)?.id
                return (
                  <Fragment key={item.id}>
                    <Answer
                      answerActionPosition={answerActionPosition}
                      appData={appData}
                      item={item}
                      question={chatList[index - 1]?.content ?? ''}
                      index={index}
                      config={config}
                      answerIcon={answerIcon}
                      responding={isLast && isResponding}
                      showPromptLog={showPromptLog}
                      chatAnswerContainerInner={chatAnswerContainerInner}
                      hideProcessDetail={hideProcessDetail}
                      noChatInput={noChatInput}
                      switchSibling={switchSibling}
                      hideAvatar={hideAvatar}
                      renderAgentContent={renderAgentContent}
                      onHumanInputFormSubmit={onHumanInputFormSubmit}
                    />
                    {canLoadMessageContext && (
                      <MessageContextMarker
                        key={currentConversationId}
                        conversationId={currentConversationId}
                        messageId={item.id}
                        isResponding={isResponding}
                      />
                    )}
                  </Fragment>
                )
              }
              return (
                <Question
                  key={item.id}
                  item={item}
                  questionIcon={questionIcon}
                  theme={theme}
                  enableEdit={config?.questionEditEnable}
                  switchSibling={switchSibling}
                  hideAvatar={hideAvatar}
                />
              )
            })}
          </div>
        </div>
        <div
          data-testid="chat-footer"
          className={cn(
            'pointer-events-none absolute bottom-0 z-10 flex justify-center bg-chat-input-mask',
            (hasTryToAsk || !noChatInput || !noStopResponding) && chatFooterClassName,
          )}
          ref={chatFooterRef}
        >
          <div
            ref={chatFooterInnerRef}
            className={cn(
              'pointer-events-none relative',
              chatFooterInnerClassName,
              isTryApp && 'px-0',
            )}
          >
            {!noStopResponding && isResponding && (
              <div data-testid="stop-responding-container" className="mb-2 flex justify-center">
                <Button
                  className="pointer-events-auto bg-components-panel-bg text-components-button-secondary-text inset-ring-components-panel-border"
                  onClick={onStopResponding}
                >
                  <div className="i-custom-vender-solid-mediaAndDevices-stop-circle h-3.5 w-3.5" />
                  <span className="text-xs font-normal">
                    {t(($) => $['operation.stopResponding'], { ns: 'appDebug' })}
                  </span>
                </Button>
              </div>
            )}
            {hasTryToAsk && <TryToAsk suggestedQuestions={suggestedQuestions} onSend={onSend} />}
            {!noChatInput && (
              <ChatInputArea
                botName={inputPlaceholderBotName || appData?.site?.title || 'Bot'}
                customPlaceholder={inputPlaceholder ?? appData?.site?.input_placeholder}
                disabled={inputDisabled}
                showFeatureBar={showFeatureBar}
                showFileUpload={showFileUpload}
                featureBarReadonly={featureBarReadonly}
                featureBarDisabled={isResponding}
                onFeatureBarClick={onFeatureBarClick}
                visionConfig={config?.file_upload}
                speechToTextConfig={config?.speech_to_text}
                speechToTextTarget={speechToTextTarget}
                onBeforeSpeechToText={onBeforeSpeechToText}
                onSend={onSend}
                inputs={inputs}
                inputsForm={inputsForm}
                theme={theme}
                isResponding={isResponding}
                readonly={readonly}
                sendButtonLabel={sendButtonLabel}
                sendButtonLoading={sendButtonLoading}
                footerNotice={footerNotice}
                footerNoticeTooltip={footerNoticeTooltip}
                sendOnEnter={sendOnEnter}
              />
            )}
          </div>
        </div>
        <ChatLogModals
          width={width}
          currentLogItem={currentLogItem}
          showPromptLogModal={showPromptLogModal}
          showAgentLogModal={showAgentLogModal}
          hideLogModal={hideLogModal}
          setCurrentLogItem={setCurrentLogItem}
          setShowPromptLogModal={setShowPromptLogModal}
          setShowAgentLogModal={setShowAgentLogModal}
        />
      </div>
    </ChatContextProvider>
  )
}

export default memo(Chat)
