import service from '@/utils/request'

/** 获取全局 Token 配置（脱敏） */
export const getAppVersionToken = () => {
  return service({ url: '/gaia/app-version/token', method: 'get' })
}

/** 设置全局 Token */
export const setAppVersionToken = (data) => {
  return service({ url: '/gaia/app-version/token', method: 'put', data })
}

/** 输入登录密码验证后查看明文 Token */
export const revealAppVersionToken = (data) => {
  return service({ url: '/gaia/app-version/token/reveal', method: 'post', data })
}

/** 版本列表 */
export const getAppVersionReleases = () => {
  return service({ url: '/gaia/app-version/releases', method: 'get' })
}

/** 新增版本 */
export const createAppVersionRelease = (data) => {
  return service({ url: '/gaia/app-version/releases', method: 'post', data })
}

/** 版本详情 */
export const getAppVersionRelease = (id) => {
  return service({ url: `/gaia/app-version/releases/${id}`, method: 'get' })
}

/** 更新版本信息 */
export const updateAppVersionRelease = (id, data) => {
  return service({ url: `/gaia/app-version/releases/${id}`, method: 'put', data })
}

/** 上传安装包到指定版本（自动识别平台/架构），formData: file */
export const uploadAppVersionPackage = (releaseId, formData) => {
  return service({
    url: `/gaia/app-version/releases/${releaseId}/upload`,
    method: 'post',
    data: formData,
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}

/** 删除指定版本下某 platform/arch 的包 */
export const deleteAppVersionDownload = (releaseId, params) => {
  return service({
    url: `/gaia/app-version/releases/${releaseId}/download`,
    method: 'delete',
    params
  })
}
