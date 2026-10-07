import type { CasdoorConfiguration } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import type { DraftErrors } from './configuration-draft'
import { Field, FieldDescription, FieldError, FieldLabel } from '@langgenius/dify-ui/field'
import { Fieldset, FieldsetLegend } from '@langgenius/dify-ui/fieldset'
import { Input } from '@langgenius/dify-ui/input'
import { useTranslation } from '#i18n'

export function ConnectionFields({
  configuration,
  onChange,
  secret,
  onSecretChange,
  errors,
  advancedOnly = false,
}: {
  configuration: CasdoorConfiguration
  onChange: (configuration: CasdoorConfiguration) => void
  secret: string
  onSecretChange: (secret: string) => void
  errors: DraftErrors
  advancedOnly?: boolean
}) {
  const { t } = useTranslation('extend')
  const fields = [
    ['browser_frontend_url', t(($) => $['systemManage.casdoor.browserUrl'])],
    ['organization', t(($) => $['systemManage.casdoor.organization'])],
    ['application', t(($) => $['systemManage.casdoor.application'])],
    ['client_id', t(($) => $['systemManage.casdoor.clientId'])],
  ] as const
  const optionalFields = [
    ['backend_api_url', t(($) => $['systemManage.casdoor.apiUrl'])],
    ['expected_issuer', t(($) => $['systemManage.casdoor.issuer'])],
    ['button_text', t(($) => $['systemManage.casdoor.buttonText'])],
  ] as const
  const visibleFields = advancedOnly ? optionalFields : fields
  return (
    <Fieldset className="space-y-3">
      {advancedOnly && (
        <FieldsetLegend>{t(($) => $['systemManage.casdoor.advancedConnection'])}</FieldsetLegend>
      )}
      <div className="space-y-4">
        {visibleFields.map(([name, label]) => (
          <Field key={name} name={name} invalid={Boolean(errors[name])}>
            <FieldLabel>{label}</FieldLabel>
            <Input
              required={!advancedOnly}
              placeholder={
                advancedOnly && name !== 'button_text'
                  ? configuration.browser_frontend_url
                  : undefined
              }
              autoComplete="off"
              value={configuration[name] ?? ''}
              onValueChange={(value) => {
                if (name === 'browser_frontend_url') {
                  const previous = configuration.browser_frontend_url.replace(/\/$/, '')
                  const next = value.replace(/\/$/, '')
                  onChange({
                    ...configuration,
                    browser_frontend_url: value,
                    backend_api_url:
                      !configuration.backend_api_url || configuration.backend_api_url === previous
                        ? next
                        : configuration.backend_api_url,
                    expected_issuer:
                      !configuration.expected_issuer || configuration.expected_issuer === previous
                        ? next
                        : configuration.expected_issuer,
                  })
                } else onChange({ ...configuration, [name]: value })
              }}
            />
            {name === 'browser_frontend_url' && (
              <FieldDescription>
                {t(($) => $['systemManage.casdoor.endpointHelp'])}
              </FieldDescription>
            )}
            {advancedOnly && name !== 'button_text' && (
              <FieldDescription>{t(($) => $['systemManage.casdoor.autoValue'])}</FieldDescription>
            )}
            {errors[name] && (
              <FieldError match>
                {errors[name] === 'invalidUrl'
                  ? t(($) => $['systemManage.casdoor.invalidUrl'])
                  : t(($) => $['systemManage.casdoor.invalidField'])}
              </FieldError>
            )}
          </Field>
        ))}
        {!advancedOnly && (
          <Field name="replacement-secret">
            <FieldLabel>{t(($) => $['systemManage.casdoor.secret'])}</FieldLabel>
            <Input
              type="password"
              autoComplete="new-password"
              value={secret}
              onValueChange={onSecretChange}
            />
            <FieldDescription>{t(($) => $['systemManage.casdoor.secretHelp'])}</FieldDescription>
          </Field>
        )}
      </div>
    </Fieldset>
  )
}
