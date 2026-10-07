import { zCasdoorWorkspacesResponse } from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { Button } from '@langgenius/dify-ui/button'
import { Field, FieldDescription, FieldError } from '@langgenius/dify-ui/field'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@langgenius/dify-ui/select'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from '#i18n'
import { consoleQuery } from '@/service/console'
import { ManagementError } from './management-error'

export function WorkspaceSelector({
  name,
  label,
  value,
  onChange,
  invalid = false,
  showHistory = false,
}: {
  name: string
  label: string
  value: string
  onChange: (value: string) => void
  invalid?: boolean
  showHistory?: boolean
}) {
  const { t } = useTranslation('extend')
  const [page, setPage] = useState(1)
  const workspaces = useQuery(
    consoleQuery.systemManageExtend.integration.casdoor.workspaces.get.queryOptions({
      input: { query: { page, limit: 100 } },
      context: { silent: true },
      retry: false,
      select: (data) => {
        const parsed = zCasdoorWorkspacesResponse.safeParse(data)
        if (!parsed.success) throw new Error('Casdoor workspace response unavailable.')
        return parsed.data
      },
    }),
  )
  const selected = workspaces.data?.workspaces.find((workspace) => workspace.workspace_id === value)
  const items = (workspaces.data?.workspaces ?? []).map((workspace) => ({
    value: workspace.workspace_id,
    label: showHistory ? `${workspace.name} · ${workspace.workspace_id}` : workspace.name,
  }))
  if (value && !selected) items.push({ value, label: value })
  const earliest = workspaces.data?.earliest_created_workspace
  return (
    <div className="space-y-2">
      <Field name={name} invalid={invalid}>
        <Select<string>
          name={name}
          value={value || null}
          onValueChange={(next) => onChange(next ?? '')}
          items={items}
        >
          <SelectLabel>{label}</SelectLabel>
          <SelectTrigger>
            <SelectValue<string>
              placeholder={t(($) => $['systemManage.casdoor.workspaceSelect'])}
            />
          </SelectTrigger>
          <SelectContent>
            {value && !selected && <SelectItem value={value}>{value}</SelectItem>}
            {(workspaces.data?.workspaces ?? []).map((workspace) => (
              <SelectItem
                key={workspace.workspace_id}
                value={workspace.workspace_id}
                disabled={!workspace.available}
              >
                <div className="space-y-0.5">
                  <p>
                    {workspace.name}
                    {showHistory && ` · ${workspace.workspace_id}`}
                  </p>
                  {showHistory && (
                    <p>
                      {t(($) => $['systemManage.casdoor.createdAt'], {
                        time: workspace.created_at,
                      })}
                    </p>
                  )}
                  {!workspace.available && (
                    <p>{t(($) => $['systemManage.casdoor.workspaceUnavailable'])}</p>
                  )}
                </div>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {invalid && (
          <FieldError match>{t(($) => $['systemManage.casdoor.invalidField'])}</FieldError>
        )}
        {value && !selected && (
          <FieldDescription>
            {t(($) => $['systemManage.casdoor.workspaceOffPage'])} {value}
          </FieldDescription>
        )}
        {selected && !selected.available && (
          <FieldDescription>
            {t(($) => $['systemManage.casdoor.workspaceUnavailable'])} {value}
          </FieldDescription>
        )}
      </Field>
      {workspaces.isPending && (
        <div role="status" className="h-10 animate-pulse rounded-lg bg-background-section">
          {t(($) => $['systemManage.casdoor.workspaceLoading'])}
        </div>
      )}
      {workspaces.isError && (
        <>
          <p>{t(($) => $['systemManage.casdoor.workspaceFailed'])}</p>
          <ManagementError error={workspaces.error} />
          <Button
            type="button"
            onClick={() => {
              void workspaces.refetch()
            }}
          >
            {t(($) => $['systemManage.casdoor.retry'])}
          </Button>
        </>
      )}
      {workspaces.data && (
        <>
          {workspaces.data.workspaces.length === 0 && (
            <p>{t(($) => $['systemManage.casdoor.workspaceEmpty'])}</p>
          )}
          {(showHistory || page > 1 || workspaces.data.has_more) && (
            <>
              <p>
                {t(($) => $['systemManage.casdoor.workspacePage'], {
                  page: workspaces.data.page,
                  total: workspaces.data.total,
                })}
              </p>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  disabled={page === 1 || workspaces.isFetching}
                  onClick={() => setPage((current) => current - 1)}
                >
                  {t(($) => $['systemManage.casdoor.previousPage'])}
                </Button>
                <Button
                  type="button"
                  disabled={!workspaces.data.has_more || workspaces.isFetching}
                  onClick={() => setPage((current) => current + 1)}
                >
                  {t(($) => $['systemManage.casdoor.nextPage'])}
                </Button>
              </div>
            </>
          )}
          {showHistory && (
            <div className="space-y-1 text-sm text-text-secondary">
              <p>
                {earliest
                  ? t(($) => $['systemManage.casdoor.earliest'], {
                      id: earliest.workspace_id,
                      time: earliest.created_at,
                    })
                  : t(($) => $['systemManage.casdoor.earliestMissing'])}
              </p>
              {earliest && !earliest.available && (
                <p>{t(($) => $['systemManage.casdoor.workspaceUnavailable'])}</p>
              )}
              {workspaces.data.earliest_created_ambiguous && (
                <p>{t(($) => $['systemManage.casdoor.earliestAmbiguous'])}</p>
              )}
            </div>
          )}
        </>
      )}
      {showHistory && (
        <p className="text-sm text-text-secondary">
          {t(($) => $['systemManage.casdoor.workspaceHistory'])}
        </p>
      )}
    </div>
  )
}
