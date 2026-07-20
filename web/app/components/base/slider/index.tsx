'use client'

import type { FC } from 'react'
import { Slider as UISlider } from '@langgenius/dify-ui/slider'

type SliderProps = {
  className?: string
  value: number
  min?: number
  max?: number
  step?: number
  onChange?: (value: number) => void
  disabled?: boolean
}

const Slider: FC<SliderProps> = ({ onChange, ...rest }) => {
  return <UISlider {...rest} onValueChange={onChange} />
}

export default Slider
