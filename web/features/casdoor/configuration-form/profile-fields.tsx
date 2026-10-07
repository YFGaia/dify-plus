import type { CasdoorConfiguration } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { Field, FieldDescription } from '@langgenius/dify-ui/field'
import { Fieldset, FieldsetLegend } from '@langgenius/dify-ui/fieldset'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@langgenius/dify-ui/select'
import { useTranslation } from '#i18n'

export function ProfileFields({
  configuration,
  onChange,
}: {
  configuration: CasdoorConfiguration
  onChange: (configuration: CasdoorConfiguration) => void
}) {
  const { t } = useTranslation('extend')
  const modes = [
    { value: 'off', label: t(($) => $['systemManage.casdoor.off']) },
    { value: 'fill_empty', label: t(($) => $['systemManage.casdoor.fillEmpty']) },
    { value: 'managed', label: t(($) => $['systemManage.casdoor.managed']) },
  ] as const
  const avatar = configuration.avatar_sync ? (configuration.avatar_mode ?? 'fill_empty') : 'off'
  return (
    <Fieldset className="space-y-3">
      <FieldsetLegend>{t(($) => $['systemManage.casdoor.profile'])}</FieldsetLegend>
      <Field name="name_sync">
        <Select<NonNullable<CasdoorConfiguration['name_sync']>>
          items={modes}
          value={configuration.name_sync ?? 'fill_empty'}
          onValueChange={(mode) => {
            if (mode) onChange({ ...configuration, name_sync: mode })
          }}
        >
          <SelectLabel>{t(($) => $['systemManage.casdoor.nameMode'])}</SelectLabel>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {modes.map((mode) => (
              <SelectItem key={mode.value} value={mode.value}>
                {mode.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
      <Field name="avatar_sync">
        <Select<NonNullable<CasdoorConfiguration['name_sync']>>
          items={modes}
          value={avatar}
          onValueChange={(mode) => {
            if (mode)
              onChange({
                ...configuration,
                avatar_sync: mode !== 'off',
                avatar_mode: mode === 'off' ? (configuration.avatar_mode ?? 'fill_empty') : mode,
              })
          }}
        >
          <SelectLabel>{t(($) => $['systemManage.casdoor.avatarMode'])}</SelectLabel>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {modes.map((mode) => (
              <SelectItem key={mode.value} value={mode.value}>
                {mode.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <FieldDescription>{t(($) => $['systemManage.casdoor.profileHelp'])}</FieldDescription>
      </Field>
      <p className="text-sm text-text-secondary">
        {t(($) => $['systemManage.casdoor.optionalUnavailable'])}
      </p>
    </Fieldset>
  )
}
