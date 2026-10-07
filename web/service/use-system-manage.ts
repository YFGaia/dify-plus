// Extend: 系统管理 — 代码执行控制（sandbox-full 授权名单）mutation hooks
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { consoleQuery } from './console'

export const useAddCodeExecutionControlEmail = () => {
  const queryClient = useQueryClient()

  return useMutation(
    consoleQuery.systemManage.codeExecutionControlAdd.mutationOptions({
      onSuccess: () => {
        queryClient.invalidateQueries({
          queryKey: consoleQuery.systemManage.codeExecutionControlList.key(),
        })
      },
    }),
  )
}

export const useRemoveCodeExecutionControlEmail = () => {
  const queryClient = useQueryClient()

  return useMutation(
    consoleQuery.systemManage.codeExecutionControlRemove.mutationOptions({
      onSuccess: () => {
        queryClient.invalidateQueries({
          queryKey: consoleQuery.systemManage.codeExecutionControlList.key(),
        })
      },
    }),
  )
}
