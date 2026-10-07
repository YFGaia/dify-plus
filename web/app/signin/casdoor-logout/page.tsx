import type { Metadata } from '@/next'
import { getRouteMetadata } from '@/app/route-metadata'
import { CasdoorLogoutResult } from '@/features/casdoor/logout/result'

export async function generateMetadata(): Promise<Metadata> {
  return {
    ...(await getRouteMetadata('extend', ($) => $['casdoorLogout.title'])),
    referrer: 'no-referrer',
  }
}

export default function CasdoorLogoutPage() {
  return <CasdoorLogoutResult />
}
