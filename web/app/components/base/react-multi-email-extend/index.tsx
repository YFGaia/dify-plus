import { cn } from '@langgenius/dify-ui/cn'
import React, { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { isEmail, ReactMultiEmail } from 'react-multi-email'
import s from './index.module.css'
import 'react-multi-email/dist/style.css'

type CustomEmailInputProps = {
  emails: string[]
  onChange: (emails: string[]) => void
  className?: string
  placeholder?: string
}

const CustomEmailInput: React.FC<CustomEmailInputProps> = ({ emails, onChange, className, placeholder }) => {
  const { t } = useTranslation()
  const [inputValue, setInputValue] = useState<string>('')
  const defaultDomain = process.env.NEXT_PUBLIC_DEFAULT_DOMAIN

  const setBlur = () => {
    if (inputValue && !inputValue.includes('@') && defaultDomain) {
      const newEmail = `${inputValue}@${defaultDomain}`
      if (isEmail(newEmail)) {
        setInputValue('')
        onChange([...emails, newEmail])
        // eslint-disable-next-line no-implied-eval
        setTimeout('document.getElementsByClassName(\'bg-transparent\')[0].value = \'\'', 100)
      }
    }
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' && inputValue && !inputValue.includes('@') && defaultDomain) {
      const newEmail = `${inputValue}@${defaultDomain}`
      if (isEmail(newEmail)) {
        setInputValue('')
        onChange([...emails, newEmail])
        // eslint-disable-next-line no-implied-eval
        setTimeout('document.getElementsByClassName(\'bg-transparent\')[0].value = \'\'', 100)
      }
    }
  }

  return (
    <ReactMultiEmail
      className={cn(
        'w-full border-none px-3 pt-2 outline-none',
        'appearance-none overflow-y-auto rounded-lg text-sm text-gray-900',
        s.emailsInput,
        className,
      )}
      autoFocus
      emails={emails}
      allowDuplicate={false}
      inputClassName="bg-transparent"
      onChange={onChange}
      autoComplete="on"
      onBlur={setBlur}
      onChangeInput={setInputValue}
      initialInputValue={inputValue}
      getLabel={(email: string, index: number, removeEmail: (index: number) => void) => (
        <div data-tag key={index} className="bg-components-button-secondary-bg!">
          <div data-tag-item>{email}</div>
          <button
            type="button"
            data-tag-handle
            aria-label={`${t('operation.remove', { ns: 'common' })} ${email}`}
            className="border-none bg-transparent p-0 text-inherit"
            onClick={() => removeEmail(index)}
          >
            ×
          </button>
        </div>
      )}
      onKeyDown={handleKeyDown}
      placeholder={placeholder}
    />
  )
}

export default CustomEmailInput
