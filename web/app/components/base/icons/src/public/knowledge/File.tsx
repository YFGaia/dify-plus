// GENERATE BY script
// DON NOT EDIT IT MANUALLY

<<<<<<<< HEAD:web/app/components/base/icons/src/public/tracing/ArizeIcon.tsx
import * as React from 'react'
import data from './ArizeIcon.json'
import IconBase from '@/app/components/base/icons/IconBase'
========
>>>>>>>> upstream/main:web/app/components/base/icons/src/public/knowledge/File.tsx
import type { IconData } from '@/app/components/base/icons/IconBase'
import * as React from 'react'
import IconBase from '@/app/components/base/icons/IconBase'
import data from './File.json'

const Icon = (
  {
    ref,
    ...props
  }: React.SVGProps<SVGSVGElement> & {
    ref?: React.RefObject<React.RefObject<HTMLOrSVGElement>>
  },
) => <IconBase {...props} ref={ref} data={data as IconData} />

<<<<<<<< HEAD:web/app/components/base/icons/src/public/tracing/ArizeIcon.tsx
Icon.displayName = 'ArizeIcon'
========
Icon.displayName = 'File'
>>>>>>>> upstream/main:web/app/components/base/icons/src/public/knowledge/File.tsx

export default Icon
