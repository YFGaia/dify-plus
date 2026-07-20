import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getQuotaList, setUserQuota } from '@/service/system-manage-extend'
import QuotaManagementPage from '../page'

vi.mock('react-i18next', async () => {
  // extend: 上游 1.16.0 i18n 切换 typed-selector，t 首参为选择器函数，用官方测试桩还原为 key 字符串
  const { withSelectorKey } = await import('@/test/i18n-mock')
  return {
    useTranslation: () => ({
      t: withSelectorKey((key: string, options?: Record<string, string>) => {
        if (options?.name) return `${key}:${options.name}`
        return key
      }),
    }),
  }
})

vi.mock('@/service/system-manage-extend', () => ({
  getQuotaList: vi.fn(),
  setUserQuota: vi.fn(),
}))

vi.mock('@/app/components/base/ui/toast', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

const getQuotaListMock = vi.mocked(getQuotaList)
const setUserQuotaMock = vi.mocked(setUserQuota)

const mockList = {
  list: [
    {
      account_id: 'u-1',
      ranking: 1,
      name: 'Alice',
      email: 'alice@example.com',
      avatar: null,
      used_quota: 10,
      total_quota: 100,
      balance: 90,
    },
  ],
  total: 1,
  page: 1,
  page_size: 10,
}

describe('QuotaManagementPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getQuotaListMock.mockResolvedValue(mockList)
    setUserQuotaMock.mockResolvedValue({ result: 'success' })
  })

  it('should fetch first page on initial render', async () => {
    render(<QuotaManagementPage />)

    await waitFor(() => {
      expect(getQuotaListMock).toHaveBeenCalledWith({ page: 1, page_size: 10, keyword: undefined })
    })

    expect(await screen.findByText('Alice')).toBeInTheDocument()
  })

  it('should block submit when quota input is invalid', async () => {
    render(<QuotaManagementPage />)

    await screen.findByText('Alice')

    fireEvent.click(screen.getByText('systemManage.quota.action.edit'))

    const input = screen.getByPlaceholderText('systemManage.quota.editDialog.inputPlaceholder')
    fireEvent.change(input, { target: { value: 'abc' } })
    fireEvent.click(screen.getByText('systemManage.common.confirm'))

    expect(setUserQuotaMock).not.toHaveBeenCalled()
    expect(
      await screen.findByText('systemManage.quota.editDialog.invalidInput'),
    ).toBeInTheDocument()
  })

  it('should refresh list after successful quota update', async () => {
    render(<QuotaManagementPage />)

    await screen.findByText('Alice')

    fireEvent.click(screen.getByText('systemManage.quota.action.edit'))

    const input = screen.getByPlaceholderText('systemManage.quota.editDialog.inputPlaceholder')
    fireEvent.change(input, { target: { value: '120' } })
    fireEvent.click(screen.getByText('systemManage.common.confirm'))

    await waitFor(() => {
      expect(setUserQuotaMock).toHaveBeenCalledWith({ account_id: 'u-1', quota: 120 })
    })

    await waitFor(() => {
      expect(getQuotaListMock).toHaveBeenCalledTimes(2)
    })
  })
})
