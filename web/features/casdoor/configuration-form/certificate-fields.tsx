import type { PublicCertificatePolicy } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import type { DraftErrors } from './configuration-draft'
import { Button } from '@langgenius/dify-ui/button'
import { Field, FieldError, FieldLabel } from '@langgenius/dify-ui/field'
import { Fieldset, FieldsetLegend } from '@langgenius/dify-ui/fieldset'
import { Input } from '@langgenius/dify-ui/input'
import { Textarea } from '@langgenius/dify-ui/textarea'
import { useTranslation } from '#i18n'

export function CertificateFields({
  certificates,
  onChange,
  errors,
  rowIds,
}: {
  certificates: PublicCertificatePolicy[]
  rowIds: string[]
  onChange: (certificates: PublicCertificatePolicy[], rowIds: string[]) => void
  errors: DraftErrors
}) {
  const { t } = useTranslation('extend')
  const update = (index: number, key: keyof PublicCertificatePolicy, value: string) =>
    onChange(
      certificates.map((certificate, current) =>
        current === index
          ? { ...certificate, [key]: key === 'kid' && value === '' ? null : value }
          : certificate,
      ),
      rowIds,
    )
  return (
    <Fieldset className="space-y-3">
      <FieldsetLegend>{t(($) => $['systemManage.casdoor.certificates'])}</FieldsetLegend>
      <p className="text-sm text-text-secondary">
        {t(($) => $['systemManage.casdoor.certificateHelp'])}
      </p>
      {certificates.map((certificate, index) => (
        <Fieldset
          key={rowIds[index]}
          className="space-y-2 rounded-lg border border-divider-regular p-3"
        >
          <FieldsetLegend>
            {t(($) => $['systemManage.casdoor.certificate'], { number: index + 1 })}
          </FieldsetLegend>
          <Field
            name={`certificates.${index}.pem`}
            invalid={Boolean(errors[`certificates.${index}.pem`])}
          >
            <FieldLabel>{t(($) => $['systemManage.casdoor.pem'])}</FieldLabel>
            <Textarea
              required
              value={certificate.pem}
              onValueChange={(value) => update(index, 'pem', value)}
            />
            {errors[`certificates.${index}.pem`] && (
              <FieldError match>
                {t(($) => $['systemManage.casdoor.invalidCertificate'])}
              </FieldError>
            )}
          </Field>
          {(
            [
              ['kid', t(($) => $['systemManage.casdoor.kid'])],
              ['not_before', t(($) => $['systemManage.casdoor.notBefore'])],
              ['accept_until', t(($) => $['systemManage.casdoor.acceptUntil'])],
            ] as const
          ).map(([key, label]) => (
            <Field
              key={key}
              name={`certificates.${index}.${key}`}
              invalid={Boolean(errors[`certificates.${index}.${key}`])}
            >
              <FieldLabel>{label}</FieldLabel>
              <Input
                required={key !== 'kid'}
                value={certificate[key] ?? ''}
                onValueChange={(value) => update(index, key, value)}
              />
              {errors[`certificates.${index}.${key}`] && (
                <FieldError match>
                  {t(($) => $['systemManage.casdoor.invalidCertificate'])}
                </FieldError>
              )}
            </Field>
          ))}
          <Button
            type="button"
            onClick={() =>
              onChange(
                certificates.filter((_, current) => current !== index),
                rowIds.filter((_, current) => current !== index),
              )
            }
          >
            {t(($) => $['systemManage.casdoor.removeCertificate'], { number: index + 1 })}
          </Button>
        </Fieldset>
      ))}
      {errors.certificates && (
        <p role="alert">{t(($) => $['systemManage.casdoor.invalidCertificate'])}</p>
      )}
      <Button
        type="button"
        disabled={certificates.length >= 2}
        onClick={() =>
          onChange(
            [...certificates, { pem: '', kid: null, not_before: '', accept_until: '' }],
            [...rowIds, crypto.randomUUID()],
          )
        }
      >
        {t(($) => $['systemManage.casdoor.addCertificate'])}
      </Button>
    </Fieldset>
  )
}
