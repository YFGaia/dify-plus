'use client'
import type { FC } from 'react'
import type {
  MoreLikeThisConfig,
  PromptConfig,
  SavedMessage,
  TextToSpeechConfig,
} from '@/models/debug'
import type { InstalledApp } from '@/models/explore'
import type { SiteInfo } from '@/models/share'
import type { VisionFile, VisionSettings } from '@/types/app'
import {
  RiBookmark3Line,
  RiErrorWarningFill,
} from '@remixicon/react'
import { useBoolean } from 'ahooks'
import { useSearchParams } from 'next/navigation'
import * as React from 'react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import SavedItems from '@/app/components/app/text-generate/saved-items'
import AppIcon from '@/app/components/base/app-icon'
import Badge from '@/app/components/base/badge'
import Loading from '@/app/components/base/loading'
import DifyLogo from '@/app/components/base/logo/dify-logo'
import Toast from '@/app/components/base/toast'
import Res from '@/app/components/share/text-generation/result'
import RunOnce from '@/app/components/share/text-generation/run-once'
import { appDefaultIconBackground, BATCH_CONCURRENCY, DEFAULT_VALUE_MAX_LEN } from '@/config'
import { useGlobalPublicStore } from '@/context/global-public-context'
import { useWebAppStore } from '@/context/web-app-context'
import { useAppFavicon } from '@/hooks/use-app-favicon'
import useBreakpoints, { MediaType } from '@/hooks/use-breakpoints'
import useDocumentTitle from '@/hooks/use-document-title'
import { changeLanguage } from '@/i18n-config/client'
import { AccessMode } from '@/models/access-control'
import { fetchSavedMessage as doFetchSavedMessage, removeMessage, saveMessage } from '@/service/share'
import { Resolution, TransferMethod } from '@/types/app'
import { cn } from '@/utils/classnames'
import { userInputsFormToPromptVariables } from '@/utils/model-config'
import TabHeader from '../../base/tab-header'
import MenuDropdown from './menu-dropdown'
import RunBatch from './run-batch'
import ResDownload from './run-batch/res-download'
import BatchProgress from './run-batch/batch-progress' // Extend: Batch import
import Pagination from '@/app/components/base/pagination' // Extend: Batch import
// Extend: Start Batch import
import { downloadBatchApi, fetchBatchWorkflowListApi, processExcelUploadApi } from '@/service/web-extend'
// Extend: Stop Batch import

const GROUP_SIZE = BATCH_CONCURRENCY // to avoid RPM(Request per minute) limit. The group task finished then the next group.
enum TaskStatus {
  pending = 'pending',
  running = 'running',
  completed = 'completed',
  failed = 'failed',
}

type TaskParam = {
  inputs: Record<string, any>
}

type Task = {
  id: number
  status: TaskStatus
  params: TaskParam
}

export type IMainProps = {
  isInstalledApp?: boolean
  installedAppInfo?: InstalledApp
  isWorkflow?: boolean
}

const TextGeneration: FC<IMainProps> = ({
  isInstalledApp = false,
  installedAppInfo,
  isWorkflow = false,
}) => {
  const { notify } = Toast

  const { t } = useTranslation()
  const media = useBreakpoints()
  const isPC = media === MediaType.pc

  const searchParams = useSearchParams()
  const mode = searchParams.get('mode') || 'create'
  const [currentTab, setCurrentTab] = useState<string>(['create', 'batch'].includes(mode) ? mode : 'create')

  // Notice this situation isCallBatchAPI but not in batch tab
  const [isCallBatchAPI, setIsCallBatchAPI] = useState(false)
  const isInBatchTab = currentTab === 'batch'
  const [inputs, doSetInputs] = useState<Record<string, any>>({})
  const inputsRef = useRef(inputs)
  const setInputs = useCallback((newInputs: Record<string, any>) => {
    doSetInputs(newInputs)
    inputsRef.current = newInputs
  }, [])
  const systemFeatures = useGlobalPublicStore(s => s.systemFeatures)
  const [appId, setAppId] = useState<string>('')
  const [siteInfo, setSiteInfo] = useState<SiteInfo | null>(null)
  const [customConfig, setCustomConfig] = useState<Record<string, any> | null>(null)
  const [promptConfig, setPromptConfig] = useState<PromptConfig | null>(null)
  const [moreLikeThisConfig, setMoreLikeThisConfig] = useState<MoreLikeThisConfig | null>(null)
  const [textToSpeechConfig, setTextToSpeechConfig] = useState<TextToSpeechConfig | null>(null)

  // save message
  const [savedMessages, setSavedMessages] = useState<SavedMessage[]>([])
  const fetchSavedMessage = useCallback(async () => {
    const res: any = await doFetchSavedMessage(isInstalledApp, appId)
    setSavedMessages(res.data)
  }, [isInstalledApp, appId])
  const handleSaveMessage = async (messageId: string) => {
    await saveMessage(messageId, isInstalledApp, appId)
    notify({ type: 'success', message: t('api.saved', { ns: 'common' }) })
    fetchSavedMessage()
  }
  const handleRemoveSavedMessage = async (messageId: string) => {
    await removeMessage(messageId, isInstalledApp, appId)
    notify({ type: 'success', message: t('api.remove', { ns: 'common' }) })
    fetchSavedMessage()
  }

  // send message task
  const [controlSend, setControlSend] = useState(0)
  const [controlStopResponding, setControlStopResponding] = useState(0)
  const [visionConfig, setVisionConfig] = useState<VisionSettings>({
    enabled: false,
    number_limits: 2,
    detail: Resolution.low,
    transfer_methods: [TransferMethod.local_file],
  })
  const [completionFiles, setCompletionFiles] = useState<VisionFile[]>([])
  const [runControl, setRunControl] = useState<{ onStop: () => Promise<void> | void, isStopping: boolean } | null>(null)

  useEffect(() => {
    if (isCallBatchAPI)
      setRunControl(null)
  }, [isCallBatchAPI])

  const handleSend = () => {
    setIsCallBatchAPI(false)
    setControlSend(Date.now())

    // eslint-disable-next-line ts/no-use-before-define
    setAllTaskList([]) // clear batch task running status

    // eslint-disable-next-line ts/no-use-before-define
    showResultPanel()
  }

  const [controlRetry, setControlRetry] = useState(0)
  const handleRetryAllFailedTask = () => {
    setControlRetry(Date.now())
  }
  const [allTaskList, doSetAllTaskList] = useState<Task[]>([])
  const allTaskListRef = useRef<Task[]>([])
  const getLatestTaskList = () => allTaskListRef.current
  const setAllTaskList = (taskList: Task[]) => {
    doSetAllTaskList(taskList)
    allTaskListRef.current = taskList
  }

  // Extend: Start Batch import - 批量处理相关状态
  const [batchJobs, setBatchJobs] = useState<Array<{
    id: string
    fileName: string
    createdAt: string
    status: string
    totalRows: number
    processedRows: number
    error?: string
  }>>([])

  // 分页状态
  const [currentPage, setCurrentPage] = useState(1)
  const batchJobsLimit = 5 // 每页5个任务
  const [totalBatchJobs, setTotalBatchJobs] = useState(0)
  const [isLoadingBatchJobs, setIsLoadingBatchJobs] = useState(false)

  // 从后端获取批量工作流列表
  const loadBatchWorkflows = useCallback(async () => {
    if (!appId || currentTab !== 'batch')
      return

    setIsLoadingBatchJobs(true)
    try {
      const result = await fetchBatchWorkflowListApi(installedAppInfo?.id, currentPage, batchJobsLimit)
      if (result) {
        // 转换数据格式以兼容现有组件
        const convertedJobs = result.items.map(item => ({
          id: item.id,
          error: item.error,
          error_count: item.error_count,
          fileName: item.file_name,
          createdAt: item.created_at,
          status: item.status,
          totalRows: item.total_rows,
          processedRows: item.processed_rows,
        }))
        setBatchJobs(convertedJobs)
        setTotalBatchJobs(result.total)
      }
    }
    catch (error) {
      console.error('Failed to load batch workflows:', error)
    }
    finally {
      setIsLoadingBatchJobs(false)
    }
  }, [appId, currentTab, currentPage, installedAppInfo?.id, batchJobsLimit])

  // 加载批量工作流列表
  useEffect(() => {
    loadBatchWorkflows()
  }, [loadBatchWorkflows])

  // 自动刷新批量工作流列表（每3秒）
  useEffect(() => {
    if (currentTab !== 'batch' || batchJobs.length === 0)
      return

    // 检查是否有进行中的任务
    const hasActiveJobs = batchJobs.some(job =>
      job.status === 'pending' || job.status === 'processing',
    )

    if (!hasActiveJobs)
      return

    const refreshInterval = setInterval(() => {
      loadBatchWorkflows()
    }, 3000) // 每3秒刷新一次

    return () => clearInterval(refreshInterval)
  }, [currentTab, batchJobs, loadBatchWorkflows])

  // 计算分页数据 - 现在数据已经是从后端分页获取的，不需要再切片
  const paginatedBatchJobs = batchJobs
  // Extend: Stop Batch import

  const pendingTaskList = allTaskList.filter(task => task.status === TaskStatus.pending)
  const noPendingTask = pendingTaskList.length === 0
  const showTaskList = allTaskList.filter(task => task.status !== TaskStatus.pending)
  const currGroupNumRef = useRef(0)

  const setCurrGroupNum = (num: number) => {
    currGroupNumRef.current = num
  }
  const getCurrGroupNum = () => {
    return currGroupNumRef.current
  }
  const allSuccessTaskList = allTaskList.filter(task => task.status === TaskStatus.completed)
  const allFailedTaskList = allTaskList.filter(task => task.status === TaskStatus.failed)
  const allTasksFinished = allTaskList.every(task => task.status === TaskStatus.completed)
  const allTasksRun = allTaskList.every(task => [TaskStatus.completed, TaskStatus.failed].includes(task.status))
  const batchCompletionResRef = useRef<Record<string, string>>({})
  const setBatchCompletionRes = (res: Record<string, string>) => {
    batchCompletionResRef.current = res
  }
  const getBatchCompletionRes = () => batchCompletionResRef.current
  const exportRes = allTaskList.map((task) => {
    const batchCompletionResLatest = getBatchCompletionRes()
    const res: Record<string, string> = {}
    const { inputs } = task.params
    promptConfig?.prompt_variables.forEach((v) => {
      res[v.name] = inputs[v.key]
    })
    let result = batchCompletionResLatest[task.id]
    // task might return multiple fields, should marshal object to string
    if (typeof batchCompletionResLatest[task.id] === 'object')
      result = JSON.stringify(result)

    res[t('generation.completionResult', { ns: 'share' })] = result
    return res
  })
  const checkBatchInputs = (data: string[][]) => {
    if (!data || data.length === 0) {
      notify({ type: 'error', message: t('generation.errorMsg.empty', { ns: 'share' }) })
      return false
    }
    const headerData = data[0]
    let isMapVarName = true
    promptConfig?.prompt_variables.forEach((item, index) => {
      if (!isMapVarName)
        return

      if (item.name !== headerData[index])
        isMapVarName = false
    })

    if (!isMapVarName) {
      notify({ type: 'error', message: t('generation.errorMsg.fileStructNotMatch', { ns: 'share' }) })
      return false
    }

    let payloadData = data.slice(1)
    if (payloadData.length === 0) {
      notify({ type: 'error', message: t('generation.errorMsg.atLeastOne', { ns: 'share' }) })
      return false
    }

    // check middle empty line
    const allEmptyLineIndexes = payloadData.filter(item => item.every(i => i === '')).map(item => payloadData.indexOf(item))
    if (allEmptyLineIndexes.length > 0) {
      let hasMiddleEmptyLine = false
      let startIndex = allEmptyLineIndexes[0] - 1
      allEmptyLineIndexes.forEach((index) => {
        if (hasMiddleEmptyLine)
          return

        if (startIndex + 1 !== index) {
          hasMiddleEmptyLine = true
          return
        }
        startIndex++
      })

      if (hasMiddleEmptyLine) {
        notify({ type: 'error', message: t('generation.errorMsg.emptyLine', { ns: 'share', rowIndex: startIndex + 2 }) })
        return false
      }
    }

    // check row format
    payloadData = payloadData.filter(item => !item.every(i => i === ''))
    // after remove empty rows in the end, checked again
    if (payloadData.length === 0) {
      notify({ type: 'error', message: t('generation.errorMsg.atLeastOne', { ns: 'share' }) })
      return false
    }
    let errorRowIndex = 0
    let requiredVarName = ''
    let moreThanMaxLengthVarName = ''
    let maxLength = 0
    payloadData.forEach((item, index) => {
      if (errorRowIndex !== 0)
        return

      promptConfig?.prompt_variables.forEach((varItem, varIndex) => {
        if (errorRowIndex !== 0)
          return
        if (varItem.type === 'string') {
          const maxLen = varItem.max_length || DEFAULT_VALUE_MAX_LEN
          if (item[varIndex].length > maxLen) {
            moreThanMaxLengthVarName = varItem.name
            maxLength = maxLen
            errorRowIndex = index + 1
            return
          }
        }
        if (!varItem.required)
          return

        if (item[varIndex].trim() === '') {
          requiredVarName = varItem.name
          errorRowIndex = index + 1
        }
      })
    })

    if (errorRowIndex !== 0) {
      if (requiredVarName)
        notify({ type: 'error', message: t('generation.errorMsg.invalidLine', { ns: 'share', rowIndex: errorRowIndex + 1, varName: requiredVarName }) })

      if (moreThanMaxLengthVarName)
        notify({ type: 'error', message: t('generation.errorMsg.moreThanMaxLengthLine', { ns: 'share', rowIndex: errorRowIndex + 1, varName: moreThanMaxLengthVarName, maxLength }) })

      return false
    }
    return true
  }
  const handleRunBatch = (data: string[][]) => {
    if (!checkBatchInputs(data))
      return
    if (!allTasksFinished) {
      notify({ type: 'info', message: t('errorMessage.waitForBatchResponse', { ns: 'appDebug' }) })
      return
    }

    const payloadData = data.filter(item => !item.every(i => i === '')).slice(1)
    const varLen = promptConfig?.prompt_variables.length || 0
    setIsCallBatchAPI(true)
    const allTaskList: Task[] = payloadData.map((item, i) => {
      const inputs: Record<string, any> = {}
      if (varLen > 0) {
        item.slice(0, varLen).forEach((input, index) => {
          const varSchema = promptConfig?.prompt_variables[index]
          inputs[varSchema?.key as string] = input
          if (!input) {
            if (varSchema?.type === 'string' || varSchema?.type === 'paragraph')
              inputs[varSchema?.key as string] = ''
            else
              inputs[varSchema?.key as string] = undefined
          }
        })
      }
      return {
        id: i + 1,
        status: i < GROUP_SIZE ? TaskStatus.running : TaskStatus.pending,
        params: {
          inputs,
        },
      }
    })
    setAllTaskList(allTaskList)
    setCurrGroupNum(0)
    setControlSend(Date.now())
    // clear run once task status
    setControlStopResponding(Date.now())

    // eslint-disable-next-line ts/no-use-before-define
    showResultPanel()
  }

  // Extend: Start Batch import - 处理批量上传
  const handleBatchUpload = async (originalFile: File, data: string[][], originalFileName?: string) => {
    if (!checkBatchInputs(data))
      return

    try {
      // 创建key-name映射
      const keyNameMapping: Record<string, string> = {}
      promptConfig?.prompt_variables.forEach((variable) => {
        keyNameMapping[variable.name] = variable.key
      })

      // 直接使用原始文件
      const result = await processExcelUploadApi(originalFile, installedAppInfo?.id || '', appId, keyNameMapping)
      if (result === null) {
        // API调用失败，错误信息已经在processExcelUploadApi中显示
        return
      }
      // 上传成功后，重新加载批量任务列表
      await loadBatchWorkflows()
      // 显示结果面板
      // eslint-disable-next-line ts/no-use-before-define
      showResultPanel()
      notify({ type: 'success', message: t('batchWorkflow.batchUploadSuccess', { ns: 'extend' }) })
    }
    catch (error) {
      console.error('批量上传失败:', error)
      notify({ type: 'error', message: t('batchWorkflow.batchUploadFailed', { ns: 'extend' }) })
    }
  }
  // 下载批量处理结果
  const handleBatchDownload = async (batchId: string) => {
    try {
      const blob = await downloadBatchApi(batchId)
      if (blob) {
        const url = window.URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `batch_results_${batchId}.csv`
        document.body.appendChild(a)
        a.click()
        window.URL.revokeObjectURL(url)
        document.body.removeChild(a)
      }
    }
    catch (error) {
      console.error('下载失败:', error)
      notify({ type: 'error', message: t('batchWorkflow.downloadFailed', { ns: 'extend' }) })
    }
  }

  // 处理重试成功回调
  const handleRetrySuccess = () => {
    // 重试成功后，重新加载批量工作流列表
    loadBatchWorkflows()
    console.log('批量任务重试成功，已刷新列表')
  }
  // Extend: Stop Batch import

  const handleCompleted = (completionRes: string, taskId?: number, isSuccess?: boolean) => {
    const allTaskListLatest = getLatestTaskList()
    const batchCompletionResLatest = getBatchCompletionRes()
    const pendingTaskList = allTaskListLatest.filter(task => task.status === TaskStatus.pending)
    const runTasksCount = 1 + allTaskListLatest.filter(task => [TaskStatus.completed, TaskStatus.failed].includes(task.status)).length
    const needToAddNextGroupTask = (getCurrGroupNum() !== runTasksCount) && pendingTaskList.length > 0 && (runTasksCount % GROUP_SIZE === 0 || (allTaskListLatest.length - runTasksCount < GROUP_SIZE))
    // avoid add many task at the same time
    if (needToAddNextGroupTask)
      setCurrGroupNum(runTasksCount)

    const nextPendingTaskIds = needToAddNextGroupTask ? pendingTaskList.slice(0, GROUP_SIZE).map(item => item.id) : []
    const newAllTaskList = allTaskListLatest.map((item) => {
      if (item.id === taskId) {
        return {
          ...item,
          status: isSuccess ? TaskStatus.completed : TaskStatus.failed,
        }
      }
      if (needToAddNextGroupTask && nextPendingTaskIds.includes(item.id)) {
        return {
          ...item,
          status: TaskStatus.running,
        }
      }
      return item
    })
    setAllTaskList(newAllTaskList)
    if (taskId) {
      setBatchCompletionRes({
        ...batchCompletionResLatest,
        [`${taskId}`]: completionRes,
      })
    }
  }

  const appData = useWebAppStore(s => s.appInfo)
  const appParams = useWebAppStore(s => s.appParams)
  const accessMode = useWebAppStore(s => s.webAppAccessMode)
  useEffect(() => {
    (async () => {
      if (!appData || !appParams)
        return
      if (!isWorkflow)
        fetchSavedMessage()
      const { app_id: appId, site: siteInfo, custom_config } = appData
      setAppId(appId)
      setSiteInfo(siteInfo as SiteInfo)
      setCustomConfig(custom_config)
      await changeLanguage(siteInfo.default_language)

      const { user_input_form, more_like_this, file_upload, text_to_speech }: any = appParams
      setVisionConfig({
        // legacy of image upload compatible
        ...file_upload,
        transfer_methods: file_upload?.allowed_file_upload_methods || file_upload?.allowed_upload_methods,
        // legacy of image upload compatible
        image_file_size_limit: appParams?.system_parameters.image_file_size_limit,
        fileUploadConfig: appParams?.system_parameters,
      } as any)
      const prompt_variables = userInputsFormToPromptVariables(user_input_form)
      setPromptConfig({
        prompt_template: '', // placeholder for future
        prompt_variables,
      } as PromptConfig)
      setMoreLikeThisConfig(more_like_this)
      setTextToSpeechConfig(text_to_speech)
    })()
  }, [appData, appParams, fetchSavedMessage, isWorkflow])

  // Can Use metadata(https://beta.nextjs.org/docs/api-reference/metadata) to set title. But it only works in server side client.
  useDocumentTitle(siteInfo?.title || t('generation.title', { ns: 'share' }))

  useAppFavicon({
    enable: !isInstalledApp,
    icon_type: siteInfo?.icon_type,
    icon: siteInfo?.icon,
    icon_background: siteInfo?.icon_background,
    icon_url: siteInfo?.icon_url,
  })

  const [isShowResultPanel, { setTrue: doShowResultPanel, setFalse: hideResultPanel }] = useBoolean(false)
  const showResultPanel = () => {
    // fix: useClickAway hideResSidebar will close sidebar
    setTimeout(() => {
      doShowResultPanel()
    }, 0)
  }
  const [resultExisted, setResultExisted] = useState(false)

  const renderRes = (task?: Task) => (
    <Res
      key={task?.id}
      isWorkflow={isWorkflow}
      isCallBatchAPI={isCallBatchAPI}
      isPC={isPC}
      isMobile={!isPC}
      isInstalledApp={isInstalledApp}
      appId={appId}
      installedAppInfo={installedAppInfo}
      isError={task?.status === TaskStatus.failed}
      promptConfig={promptConfig}
      moreLikeThisEnabled={!!moreLikeThisConfig?.enabled}
      inputs={isCallBatchAPI ? (task as Task).params.inputs : inputs}
      controlSend={controlSend}
      controlRetry={task?.status === TaskStatus.failed ? controlRetry : 0}
      controlStopResponding={controlStopResponding}
      onShowRes={showResultPanel}
      handleSaveMessage={handleSaveMessage}
      taskId={task?.id}
      onCompleted={handleCompleted}
      visionConfig={visionConfig}
      completionFiles={completionFiles}
      isShowTextToSpeech={!!textToSpeechConfig?.enabled}
      siteInfo={siteInfo}
      onRunStart={() => setResultExisted(true)}
      onRunControlChange={!isCallBatchAPI ? setRunControl : undefined}
      hideInlineStopButton={!isCallBatchAPI}
    />
  )

  const renderBatchRes = () => {
    return (showTaskList.map(task => renderRes(task)))
  }

  const renderResWrap = (
    <div
      className={cn(
        'relative flex h-full flex-col',
        !isPC && 'h-[calc(100vh_-_36px)] rounded-t-2xl shadow-lg backdrop-blur-sm',
        !isPC
          ? isShowResultPanel
            ? 'bg-background-default-burn'
            : 'border-t-[0.5px] border-divider-regular bg-components-panel-bg'
          : 'bg-chatbot-bg',
      )}
    >
      {/* Extend: Start Batch import */}
      {(isCallBatchAPI || (isInBatchTab && batchJobs.length > 0)) && (
        <div className={cn(
          'flex shrink-0 items-center justify-between px-14 pb-2 pt-9',
          !isPC && 'px-4 pb-1 pt-3',
        )}
        >
          <div className="system-md-semibold-uppercase text-text-primary">
            {isCallBatchAPI ? t('generation.executions', { ns: 'share', num: allTaskList.length }) : t('batchWorkflow.batchJobs', { ns: 'extend', num: batchJobs.length })}
          </div>
          {allSuccessTaskList.length > 0 && (
            <ResDownload
              isMobile={!isPC}
              values={exportRes}
            />
          )}
        </div>
      )}
      <div className={cn(
        'flex h-0 grow flex-col overflow-y-auto',
        isPC && 'px-14 py-8',
        isPC && (isCallBatchAPI || (isInBatchTab && batchJobs.length > 0)) && 'pt-0',
        !isPC && 'p-0 pb-2',
      )}
      >
        {!isCallBatchAPI && !(isInBatchTab && batchJobs.length > 0) ? renderRes() : (
          <>
            {isCallBatchAPI && renderBatchRes()}
            {isInBatchTab && batchJobs.length > 0 && (
              <div className="space-y-4">
                {/* 数据保留提示 */}
                <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-3">
                  <div className="text-sm text-yellow-800">
                    <strong>{t('batchWorkflow.dataRetentionNotice', { ns: 'extend' })}:</strong> {t('batchWorkflow.dataRetentionDescription', { ns: 'extend' })}
                  </div>
                </div>

                {/* 批量任务列表 */}
                <div className="space-y-4">
                  {isLoadingBatchJobs ? (
                    <div className="flex justify-center py-8">
                      <div className="h-8 w-8 animate-spin rounded-full border-b-2 border-blue-600"></div>
                    </div>
                  ) : paginatedBatchJobs.length > 0 ? (
                    paginatedBatchJobs.map(job => (
                      <BatchProgress
                        key={job.id}
                        fileName={job.fileName}
                        batchId={job.id}
                        workflowId={appId}
                        jobData={job}
                        onDownload={() => handleBatchDownload(job.id)}
                        onRetrySuccess={handleRetrySuccess}
                      />
                    ))
                  ) : (
                    <div className="py-8 text-center text-gray-500">
                      {t('batchWorkflow.noBatchTasks', { ns: 'extend' })}
                    </div>
                  )}
                </div>

                {/* 分页控件 */}
                {totalBatchJobs > batchJobsLimit && (
                  <div className="mt-6 flex justify-center">
                    <Pagination
                      current={currentPage}
                      onChange={(page) => {
                        setCurrentPage(page)
                        // 页面变化时会自动触发useEffect重新加载数据
                      }}
                      total={totalBatchJobs}
                      limit={batchJobsLimit}
                      className="w-auto"
                    />
                  </div>
                )}
              </div>
            )}
          </>
        )}
        {!noPendingTask && isCallBatchAPI && (
          <div className="mt-4">
            <Loading type="area" />
          </div>
        )}
      </div>
      {/* Extend: Stop Batch import */}
      {isCallBatchAPI && allFailedTaskList.length > 0 && (
        <div className="absolute bottom-6 left-1/2 z-10 flex -translate-x-1/2 items-center gap-2 rounded-xl border border-components-panel-border bg-components-panel-bg-blur p-3 shadow-lg backdrop-blur-sm">
          <RiErrorWarningFill className="h-4 w-4 text-text-destructive" />
          <div className="system-sm-medium text-text-secondary">{t('generation.batchFailed.info', { ns: 'share', num: allFailedTaskList.length })}</div>
          <div className="h-3.5 w-px bg-divider-regular"></div>
          <div onClick={handleRetryAllFailedTask} className="system-sm-semibold-uppercase cursor-pointer text-text-accent">{t('generation.batchFailed.retry', { ns: 'share' })}</div>
        </div>
      )}
    </div>
  )

  if (!appId || !siteInfo || !promptConfig) {
    return (
      <div className="flex h-screen items-center">
        <Loading type="app" />
      </div>
    )
  }
  return (
    <div className={cn(
      'bg-background-default-burn',
      isPC && 'flex',
      !isPC && 'flex-col',
      isInstalledApp ? 'h-full rounded-2xl shadow-md' : 'h-screen',
    )}
    >
      {/* Left */}
      <div className={cn(
        'relative flex h-full shrink-0 flex-col',
        isPC ? 'w-[600px] max-w-[50%]' : resultExisted ? 'h-[calc(100%_-_64px)]' : '',
        isInstalledApp && 'rounded-l-2xl',
      )}
      >
        {/* header */}
        <div className={cn('shrink-0 space-y-4 border-b border-divider-subtle', isPC ? 'bg-components-panel-bg p-8 pb-0' : 'p-4 pb-0')}>
          <div className="flex items-center gap-3">
            <AppIcon
              size={isPC ? 'large' : 'small'}
              iconType={siteInfo.icon_type}
              icon={siteInfo.icon}
              background={siteInfo.icon_background || appDefaultIconBackground}
              imageUrl={siteInfo.icon_url}
            />
            <div className="system-md-semibold grow truncate text-text-secondary">{siteInfo.title}</div>
            <MenuDropdown hideLogout={isInstalledApp || accessMode === AccessMode.PUBLIC} data={siteInfo} />
          </div>
          {siteInfo.description && (
            <div className="system-xs-regular text-text-tertiary">{siteInfo.description}</div>
          )}
          <TabHeader
            items={[
              { id: 'create', name: t('generation.tabs.create', { ns: 'share' }) },
              { id: 'batch', name: t('generation.tabs.batch', { ns: 'share' }) },
              ...(!isWorkflow
                ? [{
                    id: 'saved',
                    name: t('generation.tabs.saved', { ns: 'share' }),
                    isRight: true,
                    icon: <RiBookmark3Line className="h-4 w-4" />,
                    extra: savedMessages.length > 0
                      ? (
                          <Badge className="ml-1">
                            {savedMessages.length}
                          </Badge>
                        )
                      : null,
                  }]
                : []),
            ]}
            value={currentTab}
            onChange={setCurrentTab}
          />
        </div>
        {/* form */}
        <div className={cn(
          'h-0 grow overflow-y-auto bg-components-panel-bg',
          isPC ? 'px-8' : 'px-4',
          !isPC && resultExisted && customConfig?.remove_webapp_brand && 'rounded-b-2xl border-b-[0.5px] border-divider-regular',
        )}
        >
          <div className={cn(currentTab === 'create' ? 'block' : 'hidden')}>
            <RunOnce
              siteInfo={siteInfo}
              inputs={inputs}
              inputsRef={inputsRef}
              onInputsChange={setInputs}
              promptConfig={promptConfig}
              onSend={handleSend}
              visionConfig={visionConfig}
              onVisionFilesChange={setCompletionFiles}
              runControl={runControl}
            />
          </div>
          <div className={cn(isInBatchTab ? 'block' : 'hidden')}>
            <RunBatch
              vars={promptConfig.prompt_variables}
              onSend={handleRunBatch}
              onBatchSend={handleBatchUpload} // Extend: Batch import
              isAllFinished={allTasksRun}
              isInstalledApp={isInstalledApp} // Extend: Batch import
              installedAppInfo={installedAppInfo} // Extend: Batch import
            />
          </div>
          {currentTab === 'saved' && (
            <SavedItems
              className={cn(isPC ? 'mt-6' : 'mt-4')}
              isShowTextToSpeech={textToSpeechConfig?.enabled}
              list={savedMessages}
              onRemove={handleRemoveSavedMessage}
              onStartCreateContent={() => setCurrentTab('create')}
            />
          )}
        </div>
        {/* powered by */}
        {!customConfig?.remove_webapp_brand && (
          <div className={cn(
            'flex shrink-0 items-center gap-1.5 bg-components-panel-bg py-3',
            isPC ? 'px-8' : 'px-4',
            !isPC && resultExisted && 'rounded-b-2xl border-b-[0.5px] border-divider-regular',
          )}
          >
            <div className="system-2xs-medium-uppercase text-text-tertiary">{t('chat.poweredBy', { ns: 'share' })}</div>
            {
              systemFeatures.branding.enabled && systemFeatures.branding.workspace_logo
                ? <img src={systemFeatures.branding.workspace_logo} alt="logo" className="block h-5 w-auto" />
                : customConfig?.replace_webapp_logo
                  ? <img src={`${customConfig?.replace_webapp_logo}`} alt="logo" className="block h-5 w-auto" />
                  : <DifyLogo size="small" />
            }
          </div>
        )}
      </div>
      {/* Result */}
      <div className={cn(
        isPC
          ? 'h-full w-0 grow'
          : isShowResultPanel
            ? 'fixed inset-0 z-50 bg-background-overlay backdrop-blur-sm'
            : resultExisted
              ? 'relative h-16 shrink-0 overflow-hidden bg-background-default-burn pt-2.5'
              : '',
      )}
      >
        {!isPC && (
          <div
            className={cn(
              isShowResultPanel
                ? 'flex items-center justify-center p-2 pt-6'
                : 'absolute left-0 top-0 z-10 flex w-full items-center justify-center px-2 pb-[57px] pt-[3px]',
            )}
            onClick={() => {
              if (isShowResultPanel)
                hideResultPanel()
              else
                showResultPanel()
            }}
          >
            <div className="h-1 w-8 cursor-grab rounded bg-divider-solid" />
          </div>
        )}
        {renderResWrap}
      </div>
    </div>
  )
}

export default TextGeneration
