'use client'
import type { FC } from 'react'
import {
  RiLoader2Line,
  RiPlayLargeLine,
} from '@remixicon/react'
import * as React from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/app/components/base/button'
import useBreakpoints, { MediaType } from '@/hooks/use-breakpoints'
import { cn } from '@/utils/classnames'
import CSVDownload from './csv-download'
import CSVReader from './csv-reader'

export type IRunBatchProps = {
  vars: { name: string }[]
  onSend: (data: string[][]) => void
  onBatchSend?: (originalFile: File, data: string[][], fileName?: string) => void // Extend: Batch import
  isAllFinished: boolean
  isInstalledApp?: boolean // Extend: Batch import
  installedAppInfo?: any // Extend: Batch import
}

const RunBatch: FC<IRunBatchProps> = ({
  vars,
  onSend,
  onBatchSend, // Extend: Batch import
  isAllFinished,
}) => {
  const { t } = useTranslation()
  const media = useBreakpoints()
  const isPC = media === MediaType.pc

  const [csvData, setCsvData] = React.useState<string[][]>([])
  const [isParsed, setIsParsed] = React.useState(false)
  // Extend: Start Batch import
  const [isUploading, setIsUploading] = React.useState(false)
  const [fileName, setFileName] = React.useState<string>('')
  const [originalFile, setOriginalFile] = React.useState<File | null>(null)
  const [isRecentlyClicked, setIsRecentlyClicked] = React.useState(false)

  const handleParsed = (data: string[][], originalFile?: File) => {
    console.log('handleParsed 被调用, originalFile:', originalFile ? originalFile.name : 'undefined')
    setCsvData(data)
    setIsParsed(true)
    if (originalFile) {
      setFileName(originalFile.name)
      setOriginalFile(originalFile)
      console.log('originalFile 已设置:', originalFile.name)
    }
    else {
      console.warn('⚠️ originalFile 未传递!')
    }
  }

  const handleSend = async () => {
    console.log('=== 批量运行调试信息 ===')
    console.log('csvData:', csvData ? csvData.length : 'null')
    console.log('originalFile:', originalFile ? originalFile.name : 'null')
    console.log('onBatchSend:', onBatchSend ? '已定义' : '未定义')
    console.log('isRecentlyClicked:', isRecentlyClicked)
    
    if (!csvData || csvData.length === 0 || !originalFile || isRecentlyClicked) {
      console.log('提前返回，原因：', {
        noCsvData: !csvData || csvData.length === 0,
        noOriginalFile: !originalFile,
        isRecentlyClicked,
      })
      return
    }

    // 设置防重复点击状态
    setIsRecentlyClicked(true)

    // 3秒后允许再次点击
    setTimeout(() => {
      setIsRecentlyClicked(false)
    }, 3000)

    const dataRows = csvData.slice(1).filter(row => !row.every(cell => cell === ''))
    const rowCount = dataRows.length
    
    console.log('有效数据行数:', rowCount)
    console.log('判断条件: rowCount > 10 && onBatchSend =', rowCount > 10, '&&', !!onBatchSend, '=', rowCount > 10 && !!onBatchSend)

    // 如果超过10行，使用批量处理
    if (rowCount > 10 && onBatchSend) {
      console.log('✅ 使用admin后台批量处理')
      setIsUploading(true)
      try {
        await onBatchSend(originalFile, csvData, fileName)
      }
      catch (error) {
        console.error('批量处理失败:', error)
      }
      finally {
        setIsUploading(false)
      }
    }
    else {
      console.log('❌ 使用旧的前端处理逻辑')
      onSend(csvData)
    }
  }

  const Icon = isAllFinished && !isUploading ? RiPlayLargeLine : RiLoader2Line
  const isDisabled = !isParsed || (!isAllFinished && !isUploading) || isRecentlyClicked

  // Extend: Start Batch import
  return (
    <div className="pt-4">
      <CSVReader onParsed={handleParsed} />
      <CSVDownload vars={vars} />
      <div className="flex justify-end">
        <Button
          variant="primary"
          className={cn('mt-4 pl-3 pr-4', !isPC && 'grow')}
          onClick={handleSend}
          disabled={isDisabled}
        >
          <Icon className={cn(!isAllFinished && 'animate-spin', 'mr-1 h-4 w-4 shrink-0')} aria-hidden="true" />
          <span className="text-[13px] uppercase">{t('generation.run', { ns: 'share' })}</span>
        </Button>
      </div>
    </div>
  )
}
export default React.memo(RunBatch)
