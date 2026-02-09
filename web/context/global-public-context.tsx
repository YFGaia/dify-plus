'use client'
import type { FC, PropsWithChildren } from 'react'
import type { SystemFeatures } from '@/types/feature'
import { useQuery } from '@tanstack/react-query'
import { create } from 'zustand'
import Loading from '@/app/components/base/loading'
import { consoleClient, setLoginConfigToken } from '@/service/client'
import { defaultSystemFeatures } from '@/types/feature'
import { fetchSetupStatusWithCache } from '@/utils/setup-status'

type GlobalPublicStore = {
  systemFeatures: SystemFeatures
  setSystemFeatures: (systemFeatures: SystemFeatures) => void
}

export const useGlobalPublicStore = create<GlobalPublicStore>(set => ({
  systemFeatures: defaultSystemFeatures,
  setSystemFeatures: (systemFeatures: SystemFeatures) => set(() => ({ systemFeatures })),
}))

const systemFeaturesQueryKey = ['systemFeatures'] as const
const setupStatusQueryKey = ['setupStatus'] as const

// extend: CVE-2025-63387未授权访问 — 先请求 bootstrap 拿 JWT（cookie + body），跨域时用 Header 带 token 请求 login_config
async function fetchSystemFeatures() {
  const bootstrapRes = await consoleClient.loginConfigBootstrap()
  if (bootstrapRes?.token)
    setLoginConfigToken(bootstrapRes.token)
  const data = await consoleClient.loginConfig()
  const { setSystemFeatures } = useGlobalPublicStore.getState()
  setSystemFeatures({ ...defaultSystemFeatures, ...data })
  return data
}

export function useSystemFeaturesQuery() {
  return useQuery({
    queryKey: systemFeaturesQueryKey,
    queryFn: fetchSystemFeatures,
  })
}

export function useIsSystemFeaturesPending() {
  const { isPending } = useSystemFeaturesQuery()
  return isPending
}

export function useSetupStatusQuery() {
  return useQuery({
    queryKey: setupStatusQueryKey,
    queryFn: fetchSetupStatusWithCache,
    staleTime: Infinity,
  })
}

const GlobalPublicStoreProvider: FC<PropsWithChildren> = ({
  children,
}) => {
  // Fetch systemFeatures and setupStatus in parallel to reduce waterfall.
  // setupStatus is prefetched here and cached in localStorage for AppInitializer.
  const { isPending } = useSystemFeaturesQuery()

  // Prefetch setupStatus for AppInitializer (result not needed here)
  useSetupStatusQuery()

  if (isPending)
    return <div className="flex h-screen w-screen items-center justify-center"><Loading /></div>
  return <>{children}</>
}
export default GlobalPublicStoreProvider
