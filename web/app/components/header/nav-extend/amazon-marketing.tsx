'use client'

import Link from 'next/link'
import { useSelectedLayoutSegment } from 'next/navigation'
import { cn } from '@/utils/classnames'
import {
  RiAmazonFill,
  RiAmazonLine,
} from '@remixicon/react'
type ExploreNavProps = {
  className?: string
}

const AmazonMarketingNav = ({
  className,
}: ExploreNavProps) => {
  const selectedSegment = useSelectedLayoutSegment()
  const activated = selectedSegment === 'amazon-marketing-extend'
  return (
    <Link href="/amazon-marketing-extend" className={cn(
      'group text-sm font-medium',
      activated && 'font-semibold bg-components-main-nav-nav-button-bg-active hover:bg-components-main-nav-nav-button-bg-active-hover shadow-md',
      activated ? 'text-components-main-nav-nav-button-text-active' : 'text-components-main-nav-nav-button-text hover:bg-components-main-nav-nav-button-bg-hover',
      className,
    )}>
      {
        activated
          ? <RiAmazonFill className='mr-2 h-4 w-4' />
          : <RiAmazonLine className='h-4 w-4' />
      }
      <span className="text-ellipsis">{'广告运营'}</span>
    </Link>
  )
}

export default AmazonMarketingNav
