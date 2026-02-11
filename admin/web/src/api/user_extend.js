import service from '@/utils/request'

// @Summary 用户OA登录
// @Produce  application/json
// @Param data body {authorize_code:"string"}
// @Router /base/login [post]
export const oaLogin = (data) => {
  return service({
    url: '/base/oaLogin',
    method: 'post',
    data: data
  })
}

// 获取 Gaia 登录方式（钉钉/OAuth2 是否启用及授权地址）
export const getGaiaLoginOptions = (params) => {
  return service({
    url: '/base/gaiaLoginOptions',
    method: 'get',
    params
  })
}

// Gaia OAuth2 登录：传 code 或 access_token（Extend: 兼容 casdoor implicit/hybrid 仅回传 access_token）
export const gaiaOAuth2Login = (data) => {
  return service({
    url: '/base/gaiaOAuth2Login',
    method: 'post',
    data
  })
}

// 钉钉 code 登录
export const dingtalkLogin = (data) => {
  return service({
    url: '/base/dingtalkLogin',
    method: 'post',
    data
  })
}
