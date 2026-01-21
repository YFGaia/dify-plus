'use client'

import { useTranslation } from 'react-i18next'
import Link from 'next/link'
import { useSelectedLayoutSegment } from 'next/navigation'
import { cn } from '@/utils/classnames'
import {
  RiImage2Fill,
  RiImage2Line,
} from '@remixicon/react'
type ExploreNavProps = {
  className?: string
}

const DrawNav = ({
  className,
}: ExploreNavProps) => {
  const { t } = useTranslation()
  const selectedSegment = useSelectedLayoutSegment()
  const actived = selectedSegment === 'draw-extend'

  return (
    <Link href="https://gaia-x.yafex.cn/draw" className={cn(
      className, 'group',
      actived && 'bg-white shadow-md',
      actived ? 'text-primary-600' : 'text-gray-500 hover:bg-gray-200',
    )}>
      {
        actived
          ? <RiImage2Fill className='mr-2 h-4 w-4' />
          : <RiImage2Line className='mr-2 h-4 w-4' />
      }
      {t("aiDraw.title", { ns: "extend" } )}
    </Link>
  )
}

export default DrawNav
