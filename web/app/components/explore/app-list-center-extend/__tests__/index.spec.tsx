import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithNuqs } from '@/test/nuqs-testing'
import Apps from '..'

const { push } = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('@/next/navigation', () => ({ useRouter: () => ({ push }) }))
vi.mock('@/context/permission-state', async () => {
  const { createPermissionStateModuleMock } = await import('@/test/console/state-fixture')
  return createPermissionStateModuleMock(() => ({ workspacePermissionKeys: [] }))
})

const row = (id: string, name: string, category: string, description = '') => ({
  app_id: id,
  installed_id: `installed-${id}`,
  category,
  description,
  app: { id: `installed-${id}`, name, mode: 'chat', icon_type: 'emoji', icon: '😀' },
})
const response = {
  categories: ['Writing', 'Research'],
  recommended_apps: [
    row('writer', 'Writer app', 'Writing', 'Draft REPORTS'),
    row('writer', 'Writer app', 'Research', 'Draft REPORTS'),
    row('reader', 'Reader app', 'Research', 'Find sources'),
    row('other', 'Other app', '未分类'),
  ],
}

const renderApps = (
  options: { searchParams?: string; data?: unknown; status?: number; tagsStatus?: number } = {},
) => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input)
      if (url.includes('/installed/apps')) {
        return new Response(JSON.stringify(options.data ?? response), {
          status: options.status ?? 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/tags')) {
        return new Response(
          JSON.stringify([
            { id: 'tag-writing', name: 'Writing', type: 'app' },
            { id: 'tag-research', name: 'Research', type: 'app' },
          ]),
          { status: options.tagsStatus ?? 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      throw new Error(`Unexpected request: ${url}`)
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithNuqs(
    <QueryClientProvider client={client}>
      <Apps />
    </QueryClientProvider>,
    { searchParams: options.searchParams },
  )
}

describe('installed app center', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(() => vi.unstubAllGlobals())

  it('deduplicates multi-tag apps and opens the canonical installed route', async () => {
    const user = userEvent.setup()
    renderApps({ searchParams: '?category=Writing' })
    await screen.findByText('Writer app')
    expect(screen.getAllByText('Writer app')).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'extend.appCard.newConversation' }))
    expect(push).toHaveBeenCalledWith('/installed/installed-writer')
  })

  it('reads category from the URL and updates it through the category control', async () => {
    const user = userEvent.setup()
    const { onUrlUpdate } = renderApps({ searchParams: '?category=Writing' })
    await screen.findByText('Writer app')
    expect(screen.queryByText('Reader app')).not.toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: 'Research' }))
    await screen.findByText('Reader app')
    expect(onUrlUpdate).toHaveBeenLastCalledWith(
      expect.objectContaining({
        searchParams: expect.any(URLSearchParams),
      }),
    )
    expect(onUrlUpdate.mock.lastCall?.[0].searchParams.get('category')).toBe('Research')
  })

  it('maps selected tag IDs to category names before deduplicating multiple selections', async () => {
    const user = userEvent.setup()
    renderApps()
    await screen.findByText('Writer app')
    await user.click(screen.getByRole('combobox', { name: 'common.tag.placeholder' }))
    await user.click(await screen.findByRole('option', { name: 'Writing' }))
    expect(screen.queryByText('Reader app')).not.toBeInTheDocument()
    expect(screen.queryByText('Other app')).not.toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'Research' }))
    expect(screen.getByText('Reader app')).toBeInTheDocument()
    expect(screen.getAllByText('Writer app')).toHaveLength(1)
    expect(screen.queryByText('Other app')).not.toBeInTheDocument()
  })

  it('combines category and case-insensitive name or description search', async () => {
    const user = userEvent.setup()
    renderApps({ searchParams: '?category=Research' })
    await screen.findByText('Reader app')
    const search = screen.getByRole('searchbox', { name: 'common.operation.search' })
    await user.type(search, 'rEpOrTs')
    expect(screen.getByText('Writer app')).toBeInTheDocument()
    expect(screen.queryByText('Reader app')).not.toBeInTheDocument()
    await user.clear(search)
    await user.type(search, 'READER')
    expect(screen.getByText('Reader app')).toBeInTheDocument()
    expect(screen.queryByText('Writer app')).not.toBeInTheDocument()
  })

  it.each([
    { status: 500 },
    { data: { categories: [], recommended_apps: [{ app_id: 'broken' }] } },
    { tagsStatus: 500 },
  ])('shows an error for failed or malformed API data: %j', async (options) => {
    renderApps(options)
    expect(await screen.findByRole('alert')).toHaveTextContent('common.errorBoundary.title')
    expect(screen.queryByText('Writer app')).not.toBeInTheDocument()
  })

  it('accepts a valid empty response without treating it as an error', async () => {
    renderApps({ data: { categories: [], recommended_apps: [] } })
    await waitFor(() => expect(screen.getByRole('searchbox')).toBeInTheDocument())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'extend.appCard.newConversation' }),
    ).not.toBeInTheDocument()
  })
})
