import type { Metadata } from '@/next'
import { getRouteMetadata } from '@/app/route-metadata'
import { CasdoorSigninResult } from '@/features/casdoor/signin-result'

export async function generateMetadata(): Promise<Metadata> {
  return {
    ...(await getRouteMetadata('extend', ($) => $['casdoorSigninResult.title'])),
    referrer: 'no-referrer',
  }
}

export default function CasdoorResultPage() {
  return <CasdoorSigninResult />
}
