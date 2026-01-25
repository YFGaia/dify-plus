<template>
  <div class="system">
    <el-form
      ref="form"
      :model="config"
      label-width="240px"
    >
      <div class="page-header mb-6">
        <h2 class="text-xl font-bold">
          钉钉应用集成配置
        </h2>
        <p class="text-gray-500 mt-2">
          配置钉钉扫码登录相关参数
        </p>
      </div>

      <el-tabs class="dingtalk-tabs">
        <div class="card">
          <div class="card-header flex items-center justify-between">
            <span class="text-lg font-medium">启用状态</span>
            <div class="flex items-center">
              <el-switch
                v-model="config.status"
                active-text="已启用"
                :disabled="!isConfigValid"
                @change="handleStatusChange"
              />
            </div>
          </div>

          <el-divider />

          <div class="card-section">
            <div class="section-title">
              钉钉回调域名配置
            </div>
            <div class="text-gray-600 mb-3">
              <p>回调域名：此信息将在创建钉钉扫码登录应用时使用，可至<span class="text-blue-500 cursor-pointer" @click="goToSecuritySettings">开发配置-安全设置</span>进行修改</p>
            </div>

            <div class="flex items-center">
              <el-input v-model="host" disabled readonly class="flex-1" />
              <el-button type="primary" class="ml-2" icon="copy-document" @click="copyHost">
                复制
              </el-button>
            </div>
          </div>

          <el-divider />

          <div class="card-section">
            <div class="section-title">
              应用信息配置
            </div>
            <div class="mb-4">
              <el-button v-if="!openEdit" type="primary" class="config-btn" icon="setting" @click="openConfig">
                配置链接应用信息
              </el-button>
            </div>
            <div class="bg-gray-50 dark:bg-slate-800 p-5 border dark:border-slate-700 rounded-lg">
              <div class="flex items-center mb-4">
                <span class="info-label">CorpID:</span>
                <el-input v-if="openEdit" v-model="config.corp_id" class="info-value flex-1" />
                <span v-else class="info-value">{{ config.corp_id || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">AppID:</span>
                <el-input v-if="openEdit" v-model="config.app_id" class="info-value flex-1" />
                <span v-else class="info-value">{{ config.app_id }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">AgentID:</span>
                <el-input v-if="openEdit" v-model="config.agent_id" class="info-value flex-1" />
                <span v-else class="info-value">{{ config.agent_id || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">AppKey:</span>
                <el-input v-if="openEdit" v-model="config.app_key" class="info-value flex-1" />
                <span v-else class="info-value">{{ config.app_key || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">AppSecret:</span>
                <el-input v-if="openEdit" v-model="config.app_secret" class="info-value flex-1" />
                <span v-else class="info-value">{{ config.app_secret || '未配置' }}</span>
              </div>
              <div class="float-right">
                <el-button type="primary" plain icon="connection" @click="testConnection">
                  测试连接
                </el-button>
              </div>
              <div class="clear-both" />
            </div>
          </div>

          <el-divider />

          <div class="card-section">
            <div class="section-title">
              第三方邮箱配置
            </div>
            <div class="bg-gray-50 dark:bg-slate-800 p-5 border dark:border-slate-700 rounded-lg">
              <!-- 基础配置 -->
              <div class="flex items-center mb-4">
                <span class="info-label">邮箱详情的URL:</span>
                <el-input
                  v-if="openEdit"
                  v-model="emailApiConfig.url"
                  class="info-value flex-1"
                  placeholder="请输入钉钉通过用户名获取邮箱地址的链接地址"
                />
                <span v-else class="info-value">{{ emailApiConfig.url || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">HTTP方法:</span>
                <el-select
                  v-if="openEdit"
                  v-model="emailApiConfig.method"
                  class="info-value flex-1"
                  placeholder="选择HTTP方法"
                >
                  <el-option label="GET" value="GET" />
                  <el-option label="POST" value="POST" />
                  <el-option label="PUT" value="PUT" />
                  <el-option label="DELETE" value="DELETE" />
                </el-select>
                <span v-else class="info-value">{{ emailApiConfig.method || 'GET' }}</span>
              </div>

              <!-- 标签页配置 -->
              <div v-if="openEdit" class="mt-4">
                <el-tabs v-model="activeTab" type="border-card">
                  <!-- Headers标签 -->
                  <el-tab-pane :label="`Headers (${getHeadersCount()})`" name="headers">
                    <div class="headers-editor">
                      <div v-for="(header, index) in emailApiHeaders" :key="index" class="flex items-center mb-2 gap-2">
                        <el-input
                          v-model="header.key"
                          placeholder="Header名称 (如: Authorization)"
                          class="flex-1"
                        />
                        <el-input
                          v-model="header.value"
                          placeholder="Header值"
                          class="flex-1"
                        />
                        <el-button
                          type="danger"
                          icon="delete"
                          circle
                          size="small"
                          @click="removeHeader(index)"
                        />
                      </div>
                      <el-button
                        type="primary"
                        plain
                        icon="plus"
                        size="small"
                        @click="addHeader"
                      >
                        添加Header
                      </el-button>
                    </div>
                  </el-tab-pane>

                  <!-- Body标签 (仅POST/PUT/DELETE显示) -->
                  <el-tab-pane
                    v-if="emailApiConfig.method !== 'GET'"
                    label="Body"
                    name="body"
                  >
                    <div class="body-editor">
                      <div class="mb-3">
                        <el-radio-group v-model="emailApiConfig.body_type">
                          <el-radio label="form-data">form-data</el-radio>
                          <el-radio label="x-www-form-urlencoded">x-www-form-urlencoded</el-radio>
                          <el-radio label="raw">raw (JSON)</el-radio>
                        </el-radio-group>
                      </div>

                      <!-- form-data -->
                      <div v-if="emailApiConfig.body_type === 'form-data'" class="body-content">
                        <div v-for="(item, index) in bodyFormData" :key="index" class="flex items-center mb-2 gap-2">
                          <el-input
                            v-model="item.key"
                            placeholder="字段名"
                            class="flex-1"
                            :disabled="item.isSystemField"
                          />
                          <el-input
                            v-model="item.value"
                            placeholder="字段值（系统自动填充）"
                            class="flex-1"
                            :disabled="item.isSystemField"
                          />
                          <el-button
                            v-if="!item.isSystemField"
                            type="danger"
                            icon="delete"
                            circle
                            size="small"
                            @click="removeFormDataItem(index)"
                          />
                          <el-tag v-if="item.isSystemField" type="info" size="small">系统字段</el-tag>
                        </div>
                        <el-button type="primary" plain icon="plus" size="small" @click="addFormDataItem">
                          添加字段
                        </el-button>
                      </div>

                      <!-- x-www-form-urlencoded -->
                      <div v-if="emailApiConfig.body_type === 'x-www-form-urlencoded'" class="body-content">
                        <div v-for="(item, index) in bodyUrlEncoded" :key="index" class="flex items-center mb-2 gap-2">
                          <el-input
                            v-model="item.key"
                            placeholder="字段名"
                            class="flex-1"
                            :disabled="item.isSystemField"
                          />
                          <el-input
                            v-model="item.value"
                            placeholder="字段值（系统自动填充）"
                            class="flex-1"
                            :disabled="item.isSystemField"
                          />
                          <el-button
                            v-if="!item.isSystemField"
                            type="danger"
                            icon="delete"
                            circle
                            size="small"
                            @click="removeUrlEncodedItem(index)"
                          />
                          <el-tag v-if="item.isSystemField" type="info" size="small">系统字段</el-tag>
                        </div>
                        <el-button type="primary" plain icon="plus" size="small" @click="addUrlEncodedItem">
                          添加字段
                        </el-button>
                      </div>

                      <!-- raw JSON -->
                      <div v-if="emailApiConfig.body_type === 'raw'" class="body-content">
                        <el-input
                          v-model="bodyRaw"
                          type="textarea"
                          :rows="8"
                          placeholder='请输入JSON格式，例如: {"userId": "xxx", "other": "value"}'
                        />
                      </div>
                    </div>
                  </el-tab-pane>

                  <!-- Authorization标签 -->
                  <el-tab-pane label="Authorization" name="authorization">
                    <div class="auth-editor">
                      <div class="mb-3">
                        <el-radio-group v-model="emailApiConfig.authorization.type">
                          <el-radio label="none">None</el-radio>
                          <el-radio label="bearer">Bearer Token</el-radio>
                          <el-radio label="basic">Basic Auth</el-radio>
                        </el-radio-group>
                      </div>

                      <!-- Bearer Token -->
                      <div v-if="emailApiConfig.authorization.type === 'bearer'" class="auth-content">
                        <el-input
                          v-model="emailApiConfig.authorization.token"
                          placeholder="请输入Bearer Token"
                          type="password"
                          show-password
                        />
                      </div>

                      <!-- Basic Auth -->
                      <div v-if="emailApiConfig.authorization.type === 'basic'" class="auth-content">
                        <el-input
                          v-model="emailApiConfig.authorization.username"
                          placeholder="Username"
                          class="mb-2"
                        />
                        <el-input
                          v-model="emailApiConfig.authorization.password"
                          placeholder="Password"
                          type="password"
                          show-password
                        />
                      </div>
                    </div>
                  </el-tab-pane>
                </el-tabs>
              </div>

              <!-- 只读模式显示 -->
              <div v-else class="mt-4">
                <div v-if="Object.keys(emailApiConfig.headers).length > 0" class="mb-3">
                  <div class="text-sm font-medium mb-2">Headers:</div>
                  <div v-for="(value, key) in emailApiConfig.headers" :key="key" class="flex items-center mb-1">
                    <span class="info-label text-sm">{{ key }}:</span>
                    <span class="info-value text-sm ml-2">{{ value }}</span>
                  </div>
                </div>
                <div v-if="emailApiConfig.authorization.type !== 'none'" class="mb-3">
                  <div class="text-sm font-medium mb-2">Authorization:</div>
                  <span class="info-value text-sm">{{ emailApiConfig.authorization.type === 'bearer' ? 'Bearer Token' : 'Basic Auth' }}</span>
                </div>
              </div>

              <!-- 邮箱请求字段和提取 -->
              <div class="flex items-center mb-4 mt-4">
                <span class="info-label">邮箱请求字段:</span>
                <el-input
                  v-if="openEdit"
                  v-model="emailApiConfig.request_param_field"
                  class="info-value flex-1"
                  placeholder="例如: userId"
                />
                <span v-else class="info-value">{{ emailApiConfig.request_param_field || 'userId' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">邮箱信息提取:</span>
                <el-input
                  v-if="openEdit"
                  v-model="emailApiConfig.response_email_field"
                  class="info-value flex-1"
                  placeholder="例如: data[0].userName"
                >
                  <template #append>
                    <el-tooltip content="支持点号路径(data.email)和数组索引(data[0].userName)" placement="top">
                      <el-icon><QuestionFilled /></el-icon>
                    </el-tooltip>
                  </template>
                </el-input>
                <span v-else class="info-value">{{ emailApiConfig.response_email_field || 'data[0].userName' }}</span>
              </div>
              <div v-if="openEdit" class="flex justify-end">
                <el-button type="primary" icon="goods-filled" @click="update">
                  保存
                </el-button>
              </div>
              <div class="clear-both" />
            </div>
          </div>

          <el-divider />

          <div class="card-section">
            <div class="section-title text-amber-500">
              <el-icon><i class="el-icon-warning" /></el-icon>
              <span>温馨提示</span>
            </div>
            <div class="tips-content">
              <p class="tip-item">
                1. 扫码登录应用创建入口：
                <el-link type="primary" href="https://open-dev.dingtalk.com/fe/app" target="_blank">
                  https://open-dev.dingtalk.com/fe/app
                </el-link>
              </p>
              <p class="tip-item">
                2. AppId和AppSecret是扫码登录应用的唯一标识，创建完成后可见
              </p>
              <p class="tip-item">
                查看路径: 钉钉开放平台>应用开发>移动接入应用>扫码登录应用授权应用的列表。
              </p>
            </div>
          </div>
        </div>
      </el-tabs>
    </el-form>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { QuestionFilled } from '@element-plus/icons-vue'
import { getSystemDingTalk, setSystemDingTalk } from "@/api/gaia/system";

defineOptions({
  name: 'IntegratedDingTalk',
})

const host = ref("")
const openEdit = ref(false)
const config = ref({
  id: 0,
  classify: 1,
  status: false,
  corp_id: "",
  agent_id: "",
  app_id: "",
  app_key: "",
  app_secret: "",
  test: false,
  config: ""
})

// 第三方邮箱API配置
const emailApiConfig = ref({
  enabled: false,
  url: '',
  method: 'GET',
  request_param_field: 'userId',
  body_type: 'raw',  // form-data | x-www-form-urlencoded | raw
  headers: {},
  authorization: {
    type: 'none',    // none | bearer | basic
    token: '',
    username: '',
    password: ''
  },
  response_email_field: 'data[0].userName'
})

// 标签页管理
const activeTab = ref('headers')

// Headers编辑器数组
const emailApiHeaders = ref([{ key: '', value: '' }])

// Body编辑器数据（根据body_type不同使用不同格式）
const bodyFormData = ref([{ key: '', value: '' }])  // form-data
const bodyUrlEncoded = ref([{ key: '', value: '' }])  // x-www-form-urlencoded
const bodyRaw = ref('')  // raw JSON

// 验证配置是否有效
const isConfigValid = computed(() => {
  return !!(config.value.corp_id && config.value.agent_id && config.value.app_key && config.value.app_secret);
})

// 验证邮箱API配置是否有效
const isEmailApiConfigValid = computed(() => {
  if (!emailApiConfig.value.enabled) {
    return true; // 未启用时认为有效
  }
  return !!(
    emailApiConfig.value.url &&
    emailApiConfig.value.method &&
    emailApiConfig.value.request_param_field &&
    emailApiConfig.value.response_email_field
  );
})

// Headers管理
const getHeadersCount = () => {
  return emailApiHeaders.value.filter(h => h.key && h.value).length
}

const addHeader = () => {
  emailApiHeaders.value.push({ key: '', value: '' })
}

const removeHeader = (index) => {
  emailApiHeaders.value.splice(index, 1)
  if (emailApiHeaders.value.length === 0) {
    emailApiHeaders.value.push({ key: '', value: '' })
  }
}

// Headers数组转对象
const headersArrayToObject = () => {
  const headers = {}
  emailApiHeaders.value.forEach(header => {
    if (header.key && header.value) {
      headers[header.key] = header.value
    }
  })
  return headers
}

// Headers对象转数组
const headersObjectToArray = (headersObj) => {
  const arr = []
  for (const [key, value] of Object.entries(headersObj || {})) {
    arr.push({ key, value })
  }
  if (arr.length === 0) {
    arr.push({ key: '', value: '' })
  }
  return arr
}

// 确保系统字段存在（主请求字段）
const ensureSystemField = (bodyArray, fieldName) => {
  // 查找是否已存在系统字段
  const systemFieldIndex = bodyArray.value.findIndex(item => item.isSystemField)

  if (systemFieldIndex >= 0) {
    // 更新现有系统字段的key
    bodyArray.value[systemFieldIndex].key = fieldName
  } else {
    // 添加新的系统字段到第一个位置
    bodyArray.value.unshift({
      key: fieldName,
      value: '',
      isSystemField: true
    })
  }
}

// 移除系统字段标记（如果需要）
const removeSystemField = (bodyArray) => {
  const systemFieldIndex = bodyArray.value.findIndex(item => item.isSystemField)
  if (systemFieldIndex >= 0) {
    bodyArray.value.splice(systemFieldIndex, 1)
  }
}

// Body form-data管理
const addFormDataItem = () => {
  bodyFormData.value.push({ key: '', value: '', isSystemField: false })
}

const removeFormDataItem = (index) => {
  // 不允许删除系统字段
  if (bodyFormData.value[index].isSystemField) {
    return
  }
  bodyFormData.value.splice(index, 1)
  // 确保至少有一个非系统字段或系统字段存在
  const hasSystemField = bodyFormData.value.some(item => item.isSystemField)
  const hasOtherField = bodyFormData.value.some(item => !item.isSystemField)
  if (!hasSystemField && !hasOtherField) {
    ensureSystemField(bodyFormData, emailApiConfig.value.request_param_field)
  }
}

// Body x-www-form-urlencoded管理
const addUrlEncodedItem = () => {
  bodyUrlEncoded.value.push({ key: '', value: '', isSystemField: false })
}

const removeUrlEncodedItem = (index) => {
  // 不允许删除系统字段
  if (bodyUrlEncoded.value[index].isSystemField) {
    return
  }
  bodyUrlEncoded.value.splice(index, 1)
  // 确保至少有一个非系统字段或系统字段存在
  const hasSystemField = bodyUrlEncoded.value.some(item => item.isSystemField)
  const hasOtherField = bodyUrlEncoded.value.some(item => !item.isSystemField)
  if (!hasSystemField && !hasOtherField) {
    ensureSystemField(bodyUrlEncoded, emailApiConfig.value.request_param_field)
  }
}

// 处理状态变更
const handleStatusChange = (val) => {
  if (val && !isConfigValid.value) {
    config.value.status = false;
    ElMessage({
      type: 'warning',
      message: '请先填写应用信息配置'
    });
    return;
  }
  update();
}

// 掩码显示文本
const openConfig = () => {
  openEdit.value = true
}

// 复制回调URL
const copyHost = () => {
  navigator.clipboard.writeText(host.value);
  ElMessage({
    type: 'success',
    message: '复制成功'
  });
}

// 测试连接
const testConnection = async () => {
  if (!isConfigValid.value) {
    ElMessage({
      type: 'warning',
      message: '请先填写完整的应用信息配置'
    });
    return;
  }

  config.value.test = true;
  const res = await setSystemDingTalk(config.value)
  if (res.code === 0) {
    ElMessage({
      type: 'success',
      message: '链接成功',
    })
  }
}

// 监听URL变化，自动启用/禁用邮箱API配置
watch(() => emailApiConfig.value.url, (newUrl) => {
  emailApiConfig.value.enabled = !!newUrl && newUrl.trim() !== ''
})

// 监听request_param_field变化，更新Body中的系统字段
watch(() => emailApiConfig.value.request_param_field, (newField) => {
  if (!newField) return

  // 如果当前是form-data或x-www-form-urlencoded，更新系统字段
  if (emailApiConfig.value.method !== 'GET') {
    if (emailApiConfig.value.body_type === 'form-data') {
      ensureSystemField(bodyFormData, newField)
    } else if (emailApiConfig.value.body_type === 'x-www-form-urlencoded') {
      ensureSystemField(bodyUrlEncoded, newField)
    }
  }
})

// 监听body_type变化，管理系统字段
watch(() => emailApiConfig.value.body_type, (newType, oldType) => {
  if (emailApiConfig.value.method === 'GET') return

  const fieldName = emailApiConfig.value.request_param_field

  // 从旧类型移除系统字段
  if (oldType === 'form-data') {
    removeSystemField(bodyFormData)
  } else if (oldType === 'x-www-form-urlencoded') {
    removeSystemField(bodyUrlEncoded)
  }

  // 在新类型中添加系统字段
  if (newType === 'form-data') {
    ensureSystemField(bodyFormData, fieldName)
  } else if (newType === 'x-www-form-urlencoded') {
    ensureSystemField(bodyUrlEncoded, fieldName)
  }
})

// 监听method变化，管理系统字段
watch(() => emailApiConfig.value.method, (newMethod, oldMethod) => {
  const fieldName = emailApiConfig.value.request_param_field

  // 如果从非GET变为GET，移除系统字段
  if (oldMethod !== 'GET' && newMethod === 'GET') {
    if (emailApiConfig.value.body_type === 'form-data') {
      removeSystemField(bodyFormData)
    } else if (emailApiConfig.value.body_type === 'x-www-form-urlencoded') {
      removeSystemField(bodyUrlEncoded)
    }
  }
  // 如果从GET变为非GET，添加系统字段
  else if (oldMethod === 'GET' && newMethod !== 'GET') {
    if (emailApiConfig.value.body_type === 'form-data') {
      ensureSystemField(bodyFormData, fieldName)
    } else if (emailApiConfig.value.body_type === 'x-www-form-urlencoded') {
      ensureSystemField(bodyUrlEncoded, fieldName)
    }
  }
})

const initForm = async() => {
  const res = await getSystemDingTalk()
  if (res.code === 0) {
    host.value = res.data.host
    config.value = res.data.config

    // 解析config字段中的email_api配置
    if (config.value.config) {
      try {
        const configData = typeof config.value.config === 'string'
          ? JSON.parse(config.value.config)
          : config.value.config

        if (configData.email_api) {
          const url = configData.email_api.url || ''
          emailApiConfig.value = {
            enabled: !!url && url.trim() !== '', // 根据URL自动设置enabled
            url: url,
            method: configData.email_api.method || 'GET',
            request_param_field: configData.email_api.request_param_field || 'userId',
            body_type: configData.email_api.body_type || 'raw',
            headers: configData.email_api.headers || {},
            authorization: configData.email_api.authorization || {
              type: 'none',
              token: '',
              username: '',
              password: ''
            },
            response_email_field: configData.email_api.response_email_field || 'data[0].userName'
          }

          // 转换headers为数组格式供编辑
          emailApiHeaders.value = headersObjectToArray(configData.email_api.headers)

          // 转换body数据
          if (configData.email_api.body_data) {
            if (emailApiConfig.value.body_type === 'form-data') {
              const formData = configData.email_api.body_data.form_data || []
              // 转换为带isSystemField标记的格式
              bodyFormData.value = formData.map(item => ({
                key: item.key || '',
                value: item.value || '',
                isSystemField: false
              }))
              // 确保系统字段存在
              if (emailApiConfig.value.method !== 'GET') {
                ensureSystemField(bodyFormData, emailApiConfig.value.request_param_field)
              }
            } else if (emailApiConfig.value.body_type === 'x-www-form-urlencoded') {
              const urlencoded = configData.email_api.body_data.urlencoded || []
              // 转换为带isSystemField标记的格式
              bodyUrlEncoded.value = urlencoded.map(item => ({
                key: item.key || '',
                value: item.value || '',
                isSystemField: false
              }))
              // 确保系统字段存在
              if (emailApiConfig.value.method !== 'GET') {
                ensureSystemField(bodyUrlEncoded, emailApiConfig.value.request_param_field)
              }
            } else {
              bodyRaw.value = configData.email_api.body_data.raw || ''
            }
          } else {
            // 初始化body数据
            bodyFormData.value = []
            bodyUrlEncoded.value = []
            bodyRaw.value = ''
            // 如果方法不是GET，添加系统字段
            if (emailApiConfig.value.method !== 'GET') {
              if (emailApiConfig.value.body_type === 'form-data') {
                ensureSystemField(bodyFormData, emailApiConfig.value.request_param_field)
              } else if (emailApiConfig.value.body_type === 'x-www-form-urlencoded') {
                ensureSystemField(bodyUrlEncoded, emailApiConfig.value.request_param_field)
              }
            }
          }
        }
      } catch (e) {
        console.error('解析邮箱API配置失败:', e)
      }
    }
  }
}
initForm()

const update = async() => {
  config.value.test = false;

  if (config.value.status && !isConfigValid.value) {
    config.value.status = false;
    ElMessage({
      type: 'warning',
      message: '请先填写应用信息配置'
    });
    return;
  }

  // 验证邮箱API配置
  if (!isEmailApiConfigValid.value) {
    ElMessage({
      type: 'warning',
      message: '请填写完整的第三方邮箱API配置'
    });
    return;
  }

  // 将emailApiConfig合并到config字段中
  // 转换headers数组为对象
  const headers = headersArrayToObject()

  // 准备body数据
  const bodyData = {}
  if (emailApiConfig.value.method !== 'GET') {
    if (emailApiConfig.value.body_type === 'form-data') {
      // 保存所有字段（包括系统字段），但移除isSystemField标记
      bodyData.form_data = bodyFormData.value
        .filter(item => item.key) // 只过滤掉空的key
        .map(item => ({
          key: item.key,
          value: item.value || '' // 系统字段的value为空，由后端填充
        }))
    } else if (emailApiConfig.value.body_type === 'x-www-form-urlencoded') {
      // 保存所有字段（包括系统字段），但移除isSystemField标记
      bodyData.urlencoded = bodyUrlEncoded.value
        .filter(item => item.key) // 只过滤掉空的key
        .map(item => ({
          key: item.key,
          value: item.value || '' // 系统字段的value为空，由后端填充
        }))
    } else {
      bodyData.raw = bodyRaw.value
    }
  }

  const configData = {
    email_api: {
      ...emailApiConfig.value,
      headers,
      body_data: bodyData
    }
  }
  config.value.config = JSON.stringify(configData)

  const res = await setSystemDingTalk(config.value)
  if (res.code === 0) {
    ElMessage({
      type: 'success',
      message: '设置成功',
    })
    await initForm()
    openEdit.value = false
  }
}

// 跳转到钉钉安全设置页面
const goToSecuritySettings = () => {
  if (!config.value.app_id || config.value.app_id === '') {
    ElMessage({
      type: 'warning',
      message: '配置AppID后可以正常跳转',
    });
    return;
  }

  const url = `https://open-dev.dingtalk.com/fe/ai?hash=#/app/${config.value.app_id}/security#/app/${config.value.app_id}/security`;
  window.open(url, '_blank');
}
</script>

<style lang="scss" scoped>
.system {
  @apply bg-white p-9 rounded dark:bg-slate-900;
}

.card {
  @apply bg-white dark:bg-slate-900 rounded-lg shadow-sm border border-gray-100 dark:border-slate-700 p-5;
}

.card-header {
  @apply mb-4;
}

.card-section {
  @apply py-3;
}

.section-title {
  @apply text-lg font-medium mb-4 flex items-center;
}

.info-label {
  @apply text-gray-600 dark:text-gray-400 w-28;
}

.info-value {
  @apply font-mono text-gray-800 dark:text-gray-200;
}

.tips-content {
  @apply bg-amber-50 dark:bg-amber-900/20 p-4 rounded-lg;
}

.tip-item {
  @apply mb-2 text-gray-700 dark:text-gray-300;
}

.config-btn {
  @apply w-full justify-center;
}

.action-footer {
  @apply flex items-center gap-3;
}

:deep(.el-tabs__nav) {
  @apply mb-5;
}

:deep(.el-divider) {
  @apply my-5;
}
</style>
