import type { WorkspaceRoleMapping } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import type { DraftErrors } from './configuration-draft'
import { Button } from '@langgenius/dify-ui/button'
import { Field, FieldError, FieldLabel } from '@langgenius/dify-ui/field'
import { Fieldset, FieldsetLegend } from '@langgenius/dify-ui/fieldset'
import { Input } from '@langgenius/dify-ui/input'
import { useTranslation } from '#i18n'
import { targetRoles } from './configuration-draft'
import { WorkspaceSelector } from './workspace-selector'

export function WorkspaceMappings({
  mappings,
  organization,
  onChange,
  errors,
  rowIds,
}: {
  mappings: WorkspaceRoleMapping[]
  organization: string
  rowIds: string[]
  onChange: (mappings: WorkspaceRoleMapping[], rowIds: string[]) => void
  errors: DraftErrors
}) {
  const { t } = useTranslation('extend')
  const labels = {
    admin: t(($) => $['systemManage.casdoor.admin']),
    editor: t(($) => $['systemManage.casdoor.editor']),
    normal: t(($) => $['systemManage.casdoor.normal']),
  }
  const update = (index: number, next: WorkspaceRoleMapping) =>
    onChange(
      mappings.map((mapping, current) => (index === current ? next : mapping)),
      rowIds,
    )
  const updateRole = (
    index: number,
    mapping: WorkspaceRoleMapping,
    role: (typeof targetRoles)[number],
    value: string,
  ) => {
    update(index, {
      ...mapping,
      [role]: value === '' ? null : { organization, name: value },
    })
  }
  return (
    <Fieldset className="space-y-3">
      <FieldsetLegend>{t(($) => $['systemManage.casdoor.mappings'])}</FieldsetLegend>
      <p className="text-sm text-text-secondary">
        {t(($) => $['systemManage.casdoor.mappingHelp'])}
      </p>
      {mappings.map((mapping, index) => (
        <Fieldset
          key={rowIds[index]}
          className="space-y-3 rounded-lg border border-divider-regular p-3"
        >
          <FieldsetLegend>
            {t(($) => $['systemManage.casdoor.mapping'], { number: index + 1 })}
          </FieldsetLegend>
          <WorkspaceSelector
            name={`workspace_mappings.${index}.workspace_id`}
            label={t(($) => $['systemManage.casdoor.mappingWorkspace'])}
            value={mapping.workspace_id}
            onChange={(workspace_id) => update(index, { ...mapping, workspace_id })}
            invalid={Boolean(errors[`workspace_mappings.${index}.workspace_id`])}
          />
          <div className="grid gap-3 md:grid-cols-3">
            {targetRoles.map((role) => {
              const prefix = `workspace_mappings.${index}.${role}`
              const invalid = Object.keys(errors).some(
                (path) => path === prefix || path.startsWith(`${prefix}.`),
              )
              return (
                <Fieldset key={role} className="space-y-2">
                  <FieldsetLegend>{labels[role]}</FieldsetLegend>
                  <Field name={`${prefix}.name`} invalid={invalid}>
                    <FieldLabel>{t(($) => $['systemManage.casdoor.roleName'])}</FieldLabel>
                    <Input
                      autoComplete="off"
                      value={mapping[role]?.name ?? ''}
                      onValueChange={(value) => updateRole(index, mapping, role, value)}
                    />
                    {invalid && (
                      <FieldError match>
                        {t(($) => $['systemManage.casdoor.invalidMapping'])}
                      </FieldError>
                    )}
                  </Field>
                </Fieldset>
              )
            })}
          </div>
          <Button
            type="button"
            onClick={() =>
              onChange(
                mappings.filter((_, current) => current !== index),
                rowIds.filter((_, current) => current !== index),
              )
            }
          >
            {t(($) => $['systemManage.casdoor.removeMapping'], { number: index + 1 })}
          </Button>
        </Fieldset>
      ))}
      {errors.workspace_mappings && (
        <p role="alert">{t(($) => $['systemManage.casdoor.invalidMapping'])}</p>
      )}
      <Button
        type="button"
        disabled={mappings.length >= 100}
        onClick={() =>
          onChange(
            [...mappings, { workspace_id: '', admin: null, editor: null, normal: null }],
            [...rowIds, crypto.randomUUID()],
          )
        }
      >
        {t(($) => $['systemManage.casdoor.addMapping'])}
      </Button>
    </Fieldset>
  )
}
