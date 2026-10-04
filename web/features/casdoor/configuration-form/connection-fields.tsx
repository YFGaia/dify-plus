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
}: {
  configuration: CasdoorConfiguration
  onChange: (configuration: CasdoorConfiguration) => void
  secret: string
  onSecretChange: (secret: string) => void
  errors: DraftErrors
}) {
  const { t } = useTranslation('extend')
  const fields = [
    ['browser_frontend_url', t(($) => $['systemManage.casdoor.browserUrl'])],
    ['backend_api_url', t(($) => $['systemManage.casdoor.apiUrl'])],
    ['expected_issuer', t(($) => $['systemManage.casdoor.issuer'])],
    ['organization', t(($) => $['systemManage.casdoor.organization'])],
    ['application', t(($) => $['systemManage.casdoor.application'])],
    ['client_id', t(($) => $['systemManage.casdoor.clientId'])],
    ['button_text', t(($) => $['systemManage.casdoor.buttonText'])],
  ] as const
  return (
    <Fieldset className="space-y-3">
      <FieldsetLegend>{t(($) => $['systemManage.casdoor.connection'])}</FieldsetLegend>
      <p className="text-sm text-text-secondary">
        {t(($) => $['systemManage.casdoor.applicationHelp'])}
      </p>
      {fields.map(([name, label]) => (
        <Field key={name} name={name} invalid={Boolean(errors[name])}>
          <FieldLabel>{label}</FieldLabel>
          <Input
            required
            autoComplete="off"
            value={configuration[name] ?? ''}
            onValueChange={(value) => onChange({ ...configuration, [name]: value })}
          />
          {errors[name] && (
            <FieldError match>
              {errors[name] === 'invalidUrl'
                ? t(($) => $['systemManage.casdoor.invalidUrl'])
                : t(($) => $['systemManage.casdoor.invalidField'])}
            </FieldError>
          )}
        </Field>
      ))}
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
    </Fieldset>
  )
}
