import { Button } from '@langgenius/dify-ui/button'
import { Field, FieldDescription, FieldLabel } from '@langgenius/dify-ui/field'
import { Input } from '@langgenius/dify-ui/input'
import { useState } from 'react'
import { useTranslation } from '#i18n'
import { env } from '@/env'
import { plannedCallback } from './configuration-draft'

export function CallbackReference() {
  const { t } = useTranslation('extend')
  const callback = plannedCallback(env.NEXT_PUBLIC_API_PREFIX)
  const [copy, setCopy] = useState<'copied' | 'failed' | null>(null)
  return (
    <Field name="planned-callback">
      <FieldLabel>{t(($) => $['systemManage.casdoor.callback'])}</FieldLabel>
      {callback ? (
        <>
          <div className="flex items-center gap-2">
            <Input value={callback} readOnly className="min-w-0 flex-1" />
            <Button
              type="button"
              onClick={() => {
                if (!navigator.clipboard) {
                  setCopy('failed')
                  return
                }
                navigator.clipboard
                  .writeText(callback)
                  .then(() => setCopy('copied'))
                  .catch(() => setCopy('failed'))
              }}
            >
              {t(($) => $['systemManage.casdoor.copyCallback'])}
            </Button>
          </div>
        </>
      ) : (
        <p>{t(($) => $['systemManage.casdoor.callbackUnavailable'])}</p>
      )}
      <FieldDescription>{t(($) => $['systemManage.casdoor.callbackHelp'])}</FieldDescription>
      <p role="status">
        {copy === 'copied'
          ? t(($) => $['systemManage.casdoor.copied'])
          : copy === 'failed'
            ? t(($) => $['systemManage.casdoor.copyFailed'])
            : ''}
      </p>
    </Field>
  )
}
