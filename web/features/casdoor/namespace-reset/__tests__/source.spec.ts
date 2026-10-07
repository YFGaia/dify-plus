import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vite-plus/test'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { captureManagerSource } from '../../manager-source'

const account = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

describe('manager source generation from the actual profile cache', () => {
  it.each(['clear', 'remove', 'error', 'same-account-update', 'account-aba'] as const)(
    'rejects a retained operation after %s even when its account returns',
    (change) => {
      const client = new QueryClient()
      seedAccountProfileQuery(client, { id: account })
      const source = captureManagerSource(client)
      expect(source.key).not.toBeNull()
      expect(() => source.check()).not.toThrow()
      const key = userProfileQueryOptions().queryKey
      if (change === 'clear') client.clear()
      if (change === 'remove') client.removeQueries({ queryKey: key })
      if (change === 'error')
        client.getQueryCache().find({ queryKey: key })!.setState({ status: 'error' })
      if (change === 'account-aba')
        seedAccountProfileQuery(client, { id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' })
      seedAccountProfileQuery(client, { id: account })
      expect(() => source.check()).toThrow('casdoor_manager_source_changed')
      client.clear()
    },
  )
  it('rejects A → B → A when every profile update has the exact same timestamp', () => {
    const clock = vi.spyOn(Date, 'now').mockReturnValue(1000)
    const client = new QueryClient()
    try {
      seedAccountProfileQuery(client, { id: account })
      const source = captureManagerSource(client)
      seedAccountProfileQuery(client, { id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' })
      seedAccountProfileQuery(client, { id: account })
      expect(client.getQueryState(userProfileQueryOptions().queryKey)?.dataUpdatedAt).toBe(1000)
      expect(() => source.check()).toThrow('casdoor_manager_source_changed')
    } finally {
      clock.mockRestore()
      client.clear()
    }
  })
  it('has no usable source when the profile is absent or errored', () => {
    const client = new QueryClient()
    expect(captureManagerSource(client).key).toBeNull()
    seedAccountProfileQuery(client, { id: account })
    client
      .getQueryCache()
      .find({ queryKey: userProfileQueryOptions().queryKey })!
      .setState({ status: 'error' })
    expect(captureManagerSource(client).key).toBeNull()
    client.clear()
  })
})
