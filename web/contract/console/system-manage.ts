// Extend: 系统管理 — 代码执行控制（sandbox-full 授权名单）契约
import { type } from '@orpc/contract'
import { base } from '../base'

export type CodeExecutionControlItem = {
  id: string
  email: string
  created_by: string | null
  created_at: string
}

export type CodeExecutionControlListResponse = {
  items: CodeExecutionControlItem[]
}

export type CodeExecutionControlAddResponse = {
  result: string
  item: CodeExecutionControlItem
  cache_synced: boolean
}

export type CodeExecutionControlRemoveResponse = {
  result: string
  cache_synced: boolean
}

export const codeExecutionControlListContract = base
  .route({
    path: '/system-manage-extend/code-execution-control',
    method: 'GET',
  })
  .output(type<CodeExecutionControlListResponse>())

export const codeExecutionControlAddContract = base
  .route({
    path: '/system-manage-extend/code-execution-control',
    method: 'POST',
    successStatus: 201,
  })
  .input(
    type<{
      body: {
        email: string
      }
    }>(),
  )
  .output(type<CodeExecutionControlAddResponse>())

export const codeExecutionControlRemoveContract = base
  .route({
    path: '/system-manage-extend/code-execution-control/{id}',
    method: 'DELETE',
  })
  .input(
    type<{
      params: {
        id: string
      }
    }>(),
  )
  .output(type<CodeExecutionControlRemoveResponse>())
