import service from '@/utils/request'

// 获取提供商配置列表
export const getProviderListApi = () => {
  return service({
    url: '/gaia/model-provider/list',
    method: 'get'
  })
}

// 更新提供商配置
export const updateProviderConfigApi = (data) => {
  return service({
    url: '/gaia/model-provider/update',
    method: 'post',
    data
  })
}

// 获取可用模型
export const getAvailableModelsApi = (providerName) => {
  return service({
    url: '/gaia/model-provider/available-models',
    method: 'get',
    params: {
      provider_name: providerName
    }
  })
}

// 测试提供商凭证
export const testProviderCredentialsApi = (providerName) => {
  return service({
    url: '/gaia/model-provider/test-credentials',
    method: 'get',
    params: {
      provider_name: providerName
    }
  })
}

// 获取开启的模型列表（OpenAI格式）
export const getEnabledModelsApi = () => {
  return service({
    url: '/gaia/models',
    method: 'get'
  })
}

// 获取代理日志
export const getProxyLogsApi = (params) => {
  return service({
    url: '/gaia/model-provider/logs',
    method: 'get',
    params
  })
}
