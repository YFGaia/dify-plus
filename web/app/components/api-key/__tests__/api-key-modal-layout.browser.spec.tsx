import type { ApiKeyItem } from '@dify/contracts/api/console/apps/types.gen'
import { QueryClient, QueryClientProvider, skipToken } from '@tanstack/react-query'
import { createStore, Provider } from 'jotai'
import { page } from 'vite-plus/test/browser'
import { render } from 'vitest-browser-react'
import { ApiKeyModal } from '../api-key-modal'

const quotaKey = vi.hoisted(() => ({
  id: 'layout-key',
  type: 'app',
  token: 'app-layout-fixture-abcdefghijklmnopqrst',
  description: 'Support assistant',
  accumulated_quota: 42.5,
  day_used_quota: 2.5,
  day_limit_quota: 10,
  month_used_quota: 12.5,
  month_limit_quota: 100,
  created_at: 1704067200,
  last_used_at: 1704153600,
} satisfies ApiKeyItem))

vi.mock('@/service/console', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/service/console')>()
  const apiKeys = actual.consoleQuery.apps.byResourceId.apiKeys
  return {
    ...actual,
    consoleQuery: {
      datasets: actual.consoleQuery.datasets,
      enterprise: actual.consoleQuery.enterprise,
      apps: {
        byResourceId: {
          apiKeys: {
            post: apiKeys.post,
            put: apiKeys.put,
            byApiKeyId: apiKeys.byApiKeyId,
            get: {
              queryKey: apiKeys.get.queryKey,
              queryOptions: ({ input }: { input: unknown }) => ({
                queryKey: ['layout-api-keys', input],
                queryFn: input === skipToken ? skipToken : async () => ({ data: [quotaKey] }),
              }),
            },
          },
        },
      },
    },
  }
})

vi.mock('@/context/workspace-state', async () => {
  const { atom } = await import('jotai')
  return { currentWorkspaceAtom: atom({ id: 'layout-workspace' }) }
})

vi.mock('@/hooks/use-timestamp', () => ({
  default: () => ({ formatTime: () => 'Jan 1, 2024 00:00' }),
}))

vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  const { default: appApi } = await import('@/i18n/en-US/app-api.json')
  const { default: common } = await import('@/i18n/en-US/common.json')
  const { default: extend } = await import('@/i18n/en-US/extend.json')
  const translations = Object.fromEntries(
    Object.entries({ appApi, common, extend }).flatMap(([namespace, messages]) =>
      Object.entries(messages).map(([key, value]) => [`${namespace}.${key}`, value]),
    ),
  )
  return createReactI18nextMock(translations)
})

function tableScrollContainer(table: Element, dialog: Element): HTMLElement {
  let element = table.parentElement
  while (element && element !== dialog) {
    if (['auto', 'scroll'].includes(getComputedStyle(element).overflowX)) return element
    element = element.parentElement
  }
  throw new Error('API key table has no horizontal scroll container inside its dialog')
}

describe('App API key quota modal layout', () => {
  afterEach(async () => {
    await page.viewport(1280, 720)
  })

  it.for([
    { width: 1440, height: 900 },
    { width: 390, height: 844 },
  ])('contains quota table scrolling at $width × $height', async ({ width, height }, { task }) => {
    // Browser Mode is required for responsive CSS, native table scrolling and hit testing.
    await page.viewport(width, height)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await render(
      <QueryClientProvider client={queryClient}>
        <Provider store={createStore()}>
          <ApiKeyModal
            open
            canManage
            scope={{ type: 'app', appId: 'layout-app' }}
            onOpenChange={vi.fn()}
          />
        </Provider>
      </QueryClientProvider>,
    )

    const dialog = page.getByRole('dialog', { name: 'API Key', exact: true })
    const table = dialog.getByRole('table')
    await expect.element(table.getByRole('cell', { name: 'Support assistant' })).toBeVisible()
    await expect.element(table.getByRole('cell', { name: '42.5', exact: true })).toBeVisible()
    await expect.element(table.getByRole('cell', { name: '2.5 / 10', exact: true })).toBeVisible()
    await expect.element(table.getByRole('cell', { name: '12.5 / 100', exact: true })).toBeVisible()

    const dialogElement = dialog.element()
    const scrollContainer = tableScrollContainer(table.element(), dialogElement)
    await expect
      .poll(() => dialogElement.getBoundingClientRect().width)
      .toBeCloseTo(Math.min(width * 0.9, 1200), 0)
    const dialogRect = dialogElement.getBoundingClientRect()
    const scrollRect = scrollContainer.getBoundingClientRect()
    expect(dialogRect.left).toBeGreaterThanOrEqual(0)
    expect(dialogRect.right).toBeLessThanOrEqual(width)
    expect(dialogRect.top).toBeGreaterThanOrEqual(0)
    expect(dialogRect.bottom).toBeLessThanOrEqual(height)
    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(width)
    expect(document.body.scrollWidth).toBeLessThanOrEqual(width)
    expect(dialogElement.scrollWidth).toBeLessThanOrEqual(dialogElement.clientWidth)
    expect(scrollRect.left).toBeGreaterThanOrEqual(dialogRect.left)
    expect(scrollRect.right).toBeLessThanOrEqual(dialogRect.right)
    expect(scrollContainer.scrollWidth).toBeGreaterThan(scrollContainer.clientWidth)

    const deleteButton = table.getByRole('button', { name: 'Delete app...abcdefghijklmnopqrst' })
    expect(deleteButton.element().getBoundingClientRect().right).toBeGreaterThan(scrollRect.right)
    scrollContainer.scrollTo({ left: scrollContainer.scrollWidth })
    await expect.poll(() => scrollContainer.scrollLeft).toBeGreaterThan(0)
    const actionRect = deleteButton.element().getBoundingClientRect()
    expect(actionRect.left).toBeGreaterThanOrEqual(scrollRect.left)
    expect(actionRect.right).toBeLessThanOrEqual(scrollRect.right)
    expect(actionRect.top).toBeGreaterThanOrEqual(scrollRect.top)
    expect(actionRect.bottom).toBeLessThanOrEqual(scrollRect.bottom)
    expect(dialogElement.scrollLeft).toBe(0)
    expect(document.documentElement.scrollLeft).toBe(0)

    Object.assign(task.meta, {
      quotaModalGeometry: {
        viewport: { width, height },
        dialog: dialogRect.toJSON(),
        documentWidth: document.documentElement.scrollWidth,
        scrollContainer: {
          width: scrollContainer.clientWidth,
          scrollWidth: scrollContainer.scrollWidth,
          scrollLeft: scrollContainer.scrollLeft,
        },
        action: actionRect.toJSON(),
      },
    })
    // A real click proves the revealed action is not clipped or covered by the modal.
    await deleteButton.click()
    await expect.element(page.getByRole('alertdialog')).toBeVisible()
    queryClient.clear()
  })
})
