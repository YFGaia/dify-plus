import { waitFor } from '@testing-library/react'
import { renderWithSystemFeatures } from '@/__tests__/utils/mock-system-features'
import { AccessMode } from '@/models/access-control'
import WebSSOForm from '../page'

const navigationMocks = vi.hoisted(() => ({
  replace: vi.fn(),
  searchParams: new URLSearchParams(),
}))

const webAppState = {
  shareCode: 'share-app',
  webAppAccessMode: AccessMode.PUBLIC,
}

vi.mock('@/context/web-app-context', () => ({
  useWebAppStore: (selector: (state: typeof webAppState) => unknown) => selector(webAppState),
}))

vi.mock('@/next/navigation', () => ({
  useRouter: () => ({ replace: navigationMocks.replace }),
  useSearchParams: () => navigationMocks.searchParams,
}))

vi.mock('@/service/webapp-auth', () => ({
  webAppLogout: vi.fn(),
  // extend: fork 页面会先检查 Console 登录态（WebApp 复用 Console 登录）
  checkConsoleLoginStatus: vi.fn().mockResolvedValue(true),
}))

describe('WebSSOForm redirect security', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    navigationMocks.searchParams = new URLSearchParams({
      redirect_url: 'https://evil.example/chatbot/evil-app',
    })
  })

  it('should use the login fallback when the redirect target is external', async () => {
    renderWithSystemFeatures(<WebSSOForm />, {
      systemFeatures: { webapp_auth: { enabled: true } },
    })

    await waitFor(() => {
      expect(navigationMocks.replace).toHaveBeenCalledWith('/explore/apps-center-extend') // extend: fork 默认落点为应用中心
    })
  })
})
