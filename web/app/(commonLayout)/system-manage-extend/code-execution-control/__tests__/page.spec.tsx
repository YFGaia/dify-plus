import type { ReactNode } from 'react'
import { toast } from '@langgenius/dify-ui/toast'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CodeExecutionControlPage from '../page'

const { addEmail, listEmails, removeEmail } = vi.hoisted(() => ({
  addEmail: vi.fn(),
  listEmails: vi.fn(),
  removeEmail: vi.fn(),
}))

vi.mock('@/service/console', () => ({
  consoleQuery: {
    systemManage: {
      codeExecutionControlList: {
        queryOptions: () => ({
          queryKey: ['console', 'systemManage', 'codeExecutionControlList'],
          queryFn: listEmails,
        }),
        key: () => ['console', 'systemManage', 'codeExecutionControlList'],
      },
      codeExecutionControlAdd: {
        mutationOptions: (options: Record<string, unknown>) => ({
          mutationFn: addEmail,
          ...options,
        }),
      },
      codeExecutionControlRemove: {
        mutationOptions: (options: Record<string, unknown>) => ({
          mutationFn: removeEmail,
          ...options,
        }),
      },
    },
  },
}))

vi.mock('@langgenius/dify-ui/toast', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
  },
}))

const mockItem = {
  id: 'item-1',
  email: 'alice@example.com',
  created_by: 'account-1',
  created_at: '2026-07-01T10:00:00Z',
}

const renderPage = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return render(<CodeExecutionControlPage />, { wrapper })
}

describe('CodeExecutionControlPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    listEmails.mockResolvedValue({ items: [mockItem] })
    addEmail.mockResolvedValue({ result: 'success', item: mockItem, cache_synced: true })
    removeEmail.mockResolvedValue({ result: 'success', cache_synced: true })
  })

  describe('Rendering', () => {
    it('should render authorized emails returned by the list query', async () => {
      renderPage()

      expect(await screen.findByText('alice@example.com')).toBeInTheDocument()
      expect(screen.getByText(/codeExecutionControl\.description/)).toBeInTheDocument()
    })

    it('should show empty state when the list is empty', async () => {
      listEmails.mockResolvedValue({ items: [] })

      renderPage()

      expect(await screen.findByText(/codeExecutionControl\.empty/)).toBeInTheDocument()
    })
  })

  describe('Add email', () => {
    it('should add a valid email and refresh the list', async () => {
      renderPage()
      await screen.findByText('alice@example.com')

      fireEvent.change(screen.getByPlaceholderText(/emailPlaceholder/), {
        target: { value: 'bob@example.com' },
      })
      fireEvent.click(screen.getByRole('button', { name: /codeExecutionControl\.add$/ }))

      await waitFor(() => {
        expect(addEmail).toHaveBeenCalledWith(
          { body: { email: 'bob@example.com' } },
          expect.anything(),
        )
      })
      await waitFor(() => {
        expect(listEmails).toHaveBeenCalledTimes(2)
      })
      expect(toast.success).toHaveBeenCalled()
    })

    it('should block submit and show validation error when email format is invalid', async () => {
      renderPage()
      await screen.findByText('alice@example.com')

      fireEvent.change(screen.getByPlaceholderText(/emailPlaceholder/), {
        target: { value: 'not-an-email' },
      })
      fireEvent.click(screen.getByRole('button', { name: /codeExecutionControl\.add$/ }))

      expect(addEmail).not.toHaveBeenCalled()
      expect(screen.getByText(/codeExecutionControl\.invalidEmail/)).toBeInTheDocument()
    })

    it('should show error toast when backend rejects a duplicate email', async () => {
      addEmail.mockRejectedValue(new Error('email already exists'))

      renderPage()
      await screen.findByText('alice@example.com')

      fireEvent.change(screen.getByPlaceholderText(/emailPlaceholder/), {
        target: { value: 'alice@example.com' },
      })
      fireEvent.click(screen.getByRole('button', { name: /codeExecutionControl\.add$/ }))

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith('email already exists')
      })
    })

    it('should show warning toast when cache sync fails after add', async () => {
      addEmail.mockResolvedValue({ result: 'success', item: mockItem, cache_synced: false })

      renderPage()
      await screen.findByText('alice@example.com')

      fireEvent.change(screen.getByPlaceholderText(/emailPlaceholder/), {
        target: { value: 'bob@example.com' },
      })
      fireEvent.click(screen.getByRole('button', { name: /codeExecutionControl\.add$/ }))

      await waitFor(() => {
        expect(toast.warning).toHaveBeenCalled()
      })
      expect(toast.success).not.toHaveBeenCalled()
    })
  })

  describe('Delete email', () => {
    it('should delete an email after confirming the dialog', async () => {
      renderPage()
      await screen.findByText('alice@example.com')

      fireEvent.click(screen.getByRole('button', { name: /common\.delete/ }))

      const dialog = await screen.findByRole('alertdialog')
      expect(dialog).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: /common\.confirm/ }))

      await waitFor(() => {
        expect(removeEmail).toHaveBeenCalledWith({ params: { id: 'item-1' } }, expect.anything())
      })
      await waitFor(() => {
        expect(listEmails).toHaveBeenCalledTimes(2)
      })
      expect(toast.success).toHaveBeenCalled()
    })

    it('should not delete when cancelling the confirm dialog', async () => {
      renderPage()
      await screen.findByText('alice@example.com')

      fireEvent.click(screen.getByRole('button', { name: /common\.delete/ }))
      await screen.findByRole('alertdialog')

      fireEvent.click(screen.getByRole('button', { name: /common\.cancel/ }))

      await waitFor(() => {
        expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
      })
      expect(removeEmail).not.toHaveBeenCalled()
    })
  })
})
