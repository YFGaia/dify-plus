import type { FC } from 'react'
import type { ModelParameterRule } from '../declarations'
import { cn } from '@langgenius/dify-ui/cn'
import { Field, FieldItem, FieldLabel } from '@langgenius/dify-ui/field'
import { Fieldset, FieldsetLegend } from '@langgenius/dify-ui/fieldset'
import { Radio, RadioGroup } from '@langgenius/dify-ui/radio-group' // 上游 base/radio 已删除，改用 dify-ui radio 原语
import {
  Select,
  SelectContent,
  SelectItem,
  SelectItemIndicator,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from '@langgenius/dify-ui/select'
import { Slider } from '@langgenius/dify-ui/slider'
import { Switch } from '@langgenius/dify-ui/switch'
import { useState } from 'react'
import { Infotip } from '@/app/components/base/infotip'
import TagInput from '@/app/components/base/tag-input'
import { useLanguage } from '../hooks'
import { isNullOrUndefined } from '../utils'

export type ParameterValue = number | string | string[] | boolean | undefined

type ParameterItemProps = {
  parameterRule: ModelParameterRule
  value?: ParameterValue
  onChange?: (value: ParameterValue) => void
  className?: string
  onSwitch?: (checked: boolean, assignValue: ParameterValue) => void
  isInWorkflow?: boolean
}
const ParameterItem: FC<ParameterItemProps> = ({
  parameterRule,
  value,
  onChange,
  className,
  onSwitch,
  isInWorkflow,
}) => {
  const language = useLanguage()
  const [localValue, setLocalValue] = useState(value)

  const getDefaultValue = () => {
    let defaultValue: ParameterValue

    if (parameterRule.type === 'int' || parameterRule.type === 'float')
      defaultValue = isNullOrUndefined(parameterRule.default)
        ? parameterRule.min || 0
        : parameterRule.default
    else if (parameterRule.type === 'string')
      defaultValue = parameterRule.options?.length
        ? parameterRule.default || ''
        : parameterRule.default || ''
    else if (parameterRule.type === 'boolean')
      defaultValue = !isNullOrUndefined(parameterRule.default) ? parameterRule.default : false
    else if (parameterRule.type === 'tag')
      defaultValue = !isNullOrUndefined(parameterRule.default) ? parameterRule.default : []

    return defaultValue
  }

  const renderValue = value ?? localValue ?? getDefaultValue()

  const handleInputChange = (newValue: ParameterValue) => {
    setLocalValue(newValue)

    if (
      onChange &&
      (parameterRule.name === 'stop' || !isNullOrUndefined(value) || parameterRule.required)
    )
      onChange(newValue)
  }

  const handleSlideChange = (num: number) => {
    if (!isNullOrUndefined(parameterRule.max) && num > parameterRule.max!) {
      handleInputChange(parameterRule.max)
      return
    }

    if (!isNullOrUndefined(parameterRule.min) && num < parameterRule.min!) {
      handleInputChange(parameterRule.min)
      return
    }

    handleInputChange(num)
  }

  const handleRadioChange = (v: boolean) => {
    handleInputChange(v)
  }

  const handleStringInputChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
  ) => {
    handleInputChange(e.target.value)
  }

  const handleTagChange = (newSequences: string[]) => {
    handleInputChange(newSequences)
  }

  const handleSwitch = (checked: boolean) => {
    if (onSwitch) {
      const assignValue: ParameterValue = localValue || getDefaultValue()

      onSwitch(checked, assignValue)
    }
  }

  const renderInput = () => {
    const numberInputWithSlide =
      (parameterRule.type === 'int' || parameterRule.type === 'float') &&
      !isNullOrUndefined(parameterRule.min) &&
      !isNullOrUndefined(parameterRule.max)

    if (parameterRule.type === 'int' || parameterRule.type === 'float') {
      let step = 100
      if (parameterRule.max) {
        if (parameterRule.max < 10) step = 0.1
        else if (parameterRule.max < 100) step = 1
        else if (parameterRule.max < 1000) step = 10
        else if (parameterRule.max < 10000) step = 100
      }

      return (
        numberInputWithSlide && (
          <Slider
            className="w-[120px]"
            value={renderValue as number}
            min={parameterRule.min}
            max={parameterRule.max}
            step={step}
            onValueChange={handleSlideChange}
          />
        )
      )
    }

    if (parameterRule.type === 'boolean') {
      const booleanValue = typeof renderValue === 'boolean' ? renderValue : undefined
      const translatedLabel = parameterRule.label[language] || parameterRule.label.en_US

      return (
        <Field name={parameterRule.name} className="contents">
          <Fieldset
            render={
              <RadioGroup<boolean>
                className="flex w-[200px] items-center gap-3"
                value={booleanValue}
                onValueChange={handleRadioChange}
              />
            }
          >
            <FieldsetLegend className="sr-only">{translatedLabel}</FieldsetLegend>
            <FieldItem>
              <FieldLabel className="flex w-[94px] items-center gap-1.5 system-sm-regular text-text-secondary">
                <Radio value={true} />
                True
              </FieldLabel>
            </FieldItem>
            <FieldItem>
              <FieldLabel className="flex w-[94px] items-center gap-1.5 system-sm-regular text-text-secondary">
                <Radio value={false} />
                False
              </FieldLabel>
            </FieldItem>
          </Fieldset>
        </Field>
      )
    }

    if (parameterRule.type === 'string' && !parameterRule.options?.length) {
      return (
        <input
          className={cn(
            isInWorkflow ? 'w-[200px]' : 'w-full',
            'text-gra-900 ml-4 flex h-8 appearance-none items-center rounded-lg bg-gray-100 px-3 text-[13px] outline-none',
          )}
          value={renderValue as string}
          onChange={handleStringInputChange}
        />
      )
    }

    if (parameterRule.type === 'text') {
      return (
        <textarea
          className="ml-4 h-20 w-full rounded-lg bg-gray-100 px-1 text-[12px] text-gray-900 outline-none"
          value={renderValue as string}
          onChange={handleStringInputChange}
        />
      )
    }

    if (parameterRule.type === 'string' && !!parameterRule?.options?.length) {
      return (
        <Select
          value={renderValue as string}
          onValueChange={(v) => handleInputChange(v ?? undefined)}
        >
          <SelectTrigger className={cn(isInWorkflow ? 'w-[200px]' : 'w-full', 'ml-4 h-8')}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {parameterRule.options!.map((option) => (
              <SelectItem key={option} value={option}>
                <SelectItemText>{option}</SelectItemText>
                <SelectItemIndicator />
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )
    }

    if (parameterRule.type === 'tag') {
      return (
        <div className={cn(isInWorkflow ? 'w-[200px]' : 'w-full', 'ml-4')}>
          <TagInput
            items={renderValue as string[]}
            onChange={handleTagChange}
            customizedConfirmKey="Tab"
            isInWorkflow={isInWorkflow}
          />
        </div>
      )
    }

    return null
  }

  return (
    <div className={`relative flex items-center justify-between ${className}`}>
      <div>
        <div
          className={cn(isInWorkflow ? 'w-[140px]' : 'w-full', 'ml-4 flex shrink-0 items-center')}
        >
          <div
            className="mr-0.5 truncate text-[13px] font-medium text-gray-700"
            title={parameterRule.label[language] || parameterRule.label.en_US}
          >
            {parameterRule.label[language] || parameterRule.label.en_US}
          </div>
          {parameterRule.help && (
            <>
              <Infotip
                aria-label={parameterRule.help[language] || parameterRule.help.en_US}
                className="mr-1 shrink-0"
                popupClassName="w-[200px] whitespace-pre-wrap"
              >
                {parameterRule.help[language] || parameterRule.help.en_US}
              </Infotip>
              <span className="absolute right-16 bottom-[-3px] text-xs text-orange-600">
                {renderValue}
              </span>
            </>
          )}
          {!parameterRule.required && parameterRule.name !== 'stop' && (
            <Switch checked={!isNullOrUndefined(value)} onCheckedChange={handleSwitch} size="md" />
          )}
        </div>
        {parameterRule.type === 'tag' && (
          <div className={cn(!isInWorkflow && 'w-[200px]', 'text-xs font-normal text-gray-400')}>
            {parameterRule?.tagPlaceholder?.[language]}
          </div>
        )}
      </div>
      {renderInput()}
    </div>
  )
}

export default ParameterItem
