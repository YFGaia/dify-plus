'use client'
import type { DragEvent, FC, ReactNode } from 'react'
import React, { useRef, useState } from 'react'
import Papa from 'papaparse'
import jschardet from 'jschardet'
import { useTranslation } from 'react-i18next'
import { Csv as CSVIcon } from '@/app/components/base/icons/src/public/files'
import { cn } from '@/utils/classnames'

export type Props = {
  onParsed: (data: string[][], originalFile?: File) => void // Extend: Batch import
}

// 二开部分 - Begin 自定义CSVReader
type CCProps = {
  onUploadAccepted: (results: any, file: File) => void // Extend: Batch import
  onDragOver: (event: DragEvent) => void
  onDragLeave: (event: DragEvent) => void
  children: (props: any) => React.ReactElement
}

const CustomCSVReader: React.FC<CCProps> = ({
  onUploadAccepted, onDragOver, onDragLeave, children,
}) => {
  const [zoneHover, setZoneHover] = useState(false)
  const [acceptedFile, setAcceptedFile] = useState<File | null>(null)

  const readFile = (file: File) => {
    const reader = new FileReader()

    reader.onload = (event) => {
      const result = event.target?.result as string

      // 检测文本编码
      const encodingResult = jschardet.detect(result)
      let encoding = encodingResult.encoding || 'utf-8'
      // 处理可能的误判，将 ISO-8859-2 视为 GBK
      if (encoding === 'ISO-8859-2')
        encoding = 'gbk'
      else if (encodingResult.encoding == null) {
        // 判断是否windows
        const language = (navigator as any).language || (navigator as any).userLanguage
        const isWindows = (navigator.platform && navigator.platform.includes('Win')) || navigator.userAgent.includes('Win')
        const isChineseLanguage = /^zh/i.test(language) || (navigator.languages && navigator.languages.some(lang => /^zh/i.test(lang)))
        if (isWindows && isChineseLanguage)
          encoding = 'gbk'
      }

      // 处理可能的误判
      if (encoding === 'ISO-8859-2' || encoding === 'TIS-620' || !encoding.indexOf('windows'))
        encoding = 'gbk'

      // 重新用检测到的编码读取文件内容
      const correctReader = new FileReader()

      correctReader.onload = (e) => {
        const text = e.target?.result as string

        // 使用 PapaParse 解析 CSV 文件
        Papa.parse(text, {
          complete: (results: any) => {
            onUploadAccepted(results, file)
          },
        })
      }

      correctReader.readAsText(file, encoding)
    }

    reader.readAsBinaryString(file)
  }

  const handleDrop = (event: DragEvent) => {
    event.preventDefault()
    setZoneHover(false)

    const files = event.dataTransfer.files
    if (files.length > 0) {
      const file = files[0]
      setAcceptedFile(file)
      readFile(file)
    }
  }

  const inputRef: any = useRef<ReactNode>(null)

  const handleClick = () => {
    inputRef.current.click()
  }

  const getRootProps = () => ({
    onClick: handleClick,
    onDrop: handleDrop,
    onDragOver: (event: DragEvent) => {
      event.preventDefault()
      setZoneHover(true)
    },
    onDragLeave: (event: DragEvent) => {
      event.preventDefault()
      setZoneHover(false)
    },
  })

  const renderChildren = () => {
    return children({ getRootProps, acceptedFile })
  }

  return (
    <>
      <input
        accept="text/csv, .csv, application/vnd.ms-excel"
        ref={inputRef}
        type="file"
        style={{ display: 'none' }}
        required={false}
        multiple={false}
        onChange={async (event) => {
          if (event.target.files && event.target.files.length > 0) {
            const file = event.target.files[0]
            setAcceptedFile(file)
            readFile(file)
          }
        }}
      />
      {renderChildren()}
    </>
  )
}
// 二开部分 - End 自定义CSVReader

const CSVReader: FC<Props> = ({
  onParsed,
}) => {
  const { t } = useTranslation()
  const [zoneHover, setZoneHover] = useState(false)
  return (
    <CustomCSVReader
      onUploadAccepted={(results: any, file: File) => {
        onParsed(results.data, file)
        setZoneHover(false)
      }}
      onDragOver={(event: DragEvent) => {
        event.preventDefault()
        setZoneHover(true)
      }}
      onDragLeave={(event: DragEvent) => {
        event.preventDefault()
        setZoneHover(false)
      }}
    >
      {({
        getRootProps,
        acceptedFile,
      }: any) => (
        <>
          <div
            {...getRootProps()}
            className={cn(
              'system-sm-regular flex h-20 items-center rounded-xl border border-dashed border-components-dropzone-border bg-components-dropzone-bg',
              acceptedFile && 'border-solid border-components-panel-border bg-components-panel-on-panel-item-bg px-6 hover:border-components-panel-bg-blur hover:bg-components-panel-on-panel-item-bg-hover',
              zoneHover && 'border border-components-dropzone-border-accent bg-components-dropzone-bg-accent',
            )}
          >
            {
              acceptedFile
                ? (
                  <div className="flex w-full items-center space-x-2">
                    <CSVIcon className="shrink-0" />
                    <div className="flex w-0 grow">
                      <span className="max-w-[calc(100%_-_30px)] truncate text-text-secondary">{acceptedFile.name.replace(/.csv$/, '')}</span>
                      <span className="shrink-0 text-text-tertiary">.csv</span>
                    </div>
                  </div>
                )
                : (
                  <div className="flex w-full items-center justify-center space-x-2">
                    <CSVIcon className="shrink-0" />
                    <div className="text-text-tertiary">
                      {t('generation.csvUploadTitle', {ns: 'share'})}
                      <span className="cursor-pointer text-text-accent">{t('generation.browse', {ns: 'share'})}</span>
                    </div>
                  </div>
                )
            }
          </div>
        </>
      )}
    </CustomCSVReader>
  )
}

export default React.memo(CSVReader)
