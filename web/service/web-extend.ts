import Toast from '@/app/components/base/toast'

// Admin server 使用独立的 JWT 认证，需要从 admin_token 获取
const getAdminToken = () => {
  // 优先使用 admin_token，如果没有则尝试使用 console_token
  return localStorage.getItem('admin_token') || localStorage.getItem('console_token')
}

type batchProcessing = {
  id: string
}

// 二开部分Begin - 工作流批处理上传excel
export const processExcelUploadApi = async (
  image: File, installed_id: string, app_id: string, keyNameMapping?: Record<string, string>): Promise<batchProcessing | null> => {
  const formData = new FormData()
  formData.append('file', image)
  formData.append('installed_id', installed_id)
  formData.append('app_id', app_id)
  if (keyNameMapping)
    formData.append('key_name_mapping', JSON.stringify(keyNameMapping))

  const token = getAdminToken()
  if (!token)
    return null
  try {
    const response = await fetch(`/admin/gaia/workflow/batch/processing`, {
      method: 'POST',
      body: formData,
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    })

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}))
      Toast.notify({
        type: 'error',
        message: errorData.msg || errorData.message || '批量处理上传失败',
        duration: 6000,
      })
      return null
    }

    const s = await response.json() as { code?: number, data?: batchProcessing, msg?: string }

    // 检查返回的错误码
    if (s?.code && s.code !== 0) {
      Toast.notify({
        type: 'error',
        message: s.msg || '批量处理上传失败',
        duration: 6000,
      })
      return null
    }

    return s?.data || null
  }
  catch (error: any) {
    console.error('工作流批处理上传excel失败，请重新下载或检查现有模板:', error)

    // 提取错误消息
    let errorMessage = '工作流批处理上传excel失败，请重新下载或检查现有模板'
    if (error?.message) {
      errorMessage = error.message
    }
    else if (typeof error === 'string') {
      errorMessage = error
    }

    // 显示错误通知
    Toast.notify({
      type: 'error',
      message: errorMessage,
      duration: 6000,
    })

    return null
  }
}

// 获取最近30天的批量工作流列表
export const fetchBatchWorkflowListApi = async (
  installedId?: string,
  page: number = 1,
  limit: number = 10,
): Promise<{
  items: Array<{
    id: string
    error: string
    error_count: number
    file_name: string
    status: string
    total_rows: number
    processed_rows: number
    created_at: string
    updated_at: string
  }>
  total: number
  page: number
  limit: number
  total_pages: number
  has_more: boolean
} | null> => {
  const token = getAdminToken()
  if (!token)
    return null

  try {
    const params = new URLSearchParams({
      page: page.toString(),
      limit: limit.toString(),
    })

    if (installedId)
      params.append('installed_id', installedId)

    const response = await fetch(`/admin/gaia/workflow/batch/list?${params.toString()}`, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    })

    if (!response.ok) {
      console.error('获取批量工作流列表失败:', response.statusText)
      return null
    }

    const responseData = await response.json() as {
      code?: number
      data?: {
        items: Array<{
          id: string
          file_name: string
          status: string
          error: string
          error_count: number
          total_rows: number
          processed_rows: number
          created_at: string
          updated_at: string
        }>
        total: number
        page: number
        limit: number
        total_pages: number
        has_more: boolean
      }
      msg?: string
    }

    if (responseData?.code && responseData.code !== 0) {
      console.error('获取批量工作流列表失败:', responseData.msg)
      return null
    }

    return responseData?.data || null
  }
  catch (error: any) {
    console.error('获取批量工作流列表失败:', error)
    return null
  }
}

// 获取批量处理进度
export const fetchProgressApi = async (batchId: string) => {
  const token = getAdminToken()
  if (!token)
    return null
  try {
    const response = await fetch(`/admin/gaia/workflow/batch/${batchId}/progress`, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    })

    if (!response.ok) {
      console.error('获取批量处理进度失败:', response.statusText)
      return null
    }

    const s = await response.json() as { code?: number, data?: any, msg?: string }

    // 检查返回的错误码
    if (s?.code && s.code !== 0) {
      console.error('获取批量处理进度失败:', s.msg)
      // 对于进度查询错误，我们不显示Toast，因为这是轮询操作
      return null
    }

    return s?.data
  }
  catch (error) {
    console.error('获取批量处理进度失败:', error)
    // 对于进度查询错误，我们不显示Toast，因为这是轮询操作
    return null
  }
}

// 停止批量处理
export const stopBatchApi = async (batchId: string) => {
  const token = getAdminToken()
  if (!token)
    return false
  try {
    const response = await fetch(`/admin/gaia/workflow/batch/${batchId}/stop`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok)
      return false

    const s = await response.json() as { code: number, msg: string }
    return s?.code === 0
  }
  catch (error) {
    console.error('停止批量处理失败:', error)
    return false
  }
}

// 恢复批量处理
export const resumeBatchApi = async (batchId: string) => {
  const token = getAdminToken()
  if (!token)
    return false
  try {
    const response = await fetch(`/admin/gaia/workflow/batch/${batchId}/resume`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok)
      return false

    const s = await response.json() as { code: number, msg: string }
    return s?.code === 0
  }
  catch (error) {
    console.error('恢复批量处理失败:', error)
    return false
  }
}

// 重试批量处理（完全重新开始所有任务）
export const retryBatchApi = async (batchId: string) => {
  const token = getAdminToken()
  if (!token)
    return false
  try {
    const response = await fetch(`/admin/gaia/workflow/batch/${batchId}/retry`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok)
      return false

    const s = await response.json() as { code: number, msg: string }
    return s?.code === 0
  }
  catch (error) {
    console.error('重试批量处理失败:', error)
    return false
  }
}

// 仅重试失败的任务
export const retryFailedTasksApi = async (batchId: string) => {
  const token = getAdminToken()
  if (!token)
    return false
  try {
    const response = await fetch(`/admin/gaia/workflow/batch/${batchId}/retry-failed`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok)
      return false

    const s = await response.json() as { code: number, msg: string }
    return s?.code === 0
  }
  catch (error) {
    console.error('重试失败任务失败:', error)
    return false
  }
}

// 下载批量处理结果
export const downloadBatchApi = async (batchId: string): Promise<Blob | null> => {
  const token = getAdminToken()
  if (!token)
    return null
  try {
    const response = await fetch(`/admin/gaia/workflow/batch/${batchId}/download`, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    })

    // 检查是否返回JSON错误响应
    const contentType = response.headers.get('content-type')
    console.log('contentType', contentType)
    if (contentType && contentType.includes('application/json')) {
      // 显示错误消息
      Toast.notify({
        type: 'error',
        message: '您的登录已失效，请重新登陆后再试',
        duration: 6000,
      })

      // 清除本地存储的token
      localStorage.removeItem('setup_status')
      localStorage.removeItem('console_token')
      localStorage.removeItem('refresh_token')

      // 清除对话记录
      if (localStorage?.getItem('conversationIdInfo'))
        localStorage.removeItem('conversationIdInfo')

      window.location.href = '/signin'
      console.log('signin')
      return null
    }

    if (!response.ok)
      throw new Error(`HTTP error! status: ${response.status}`)

    return await response.blob()
  }
  catch (error) {
    console.error('下载批量处理结果失败:', error)
    return null
  }
}
