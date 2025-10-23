<template>
  <div id="oauth2" class="system">
    <el-form
      ref="form"
      :model="config"
      label-width="240px"
    >
      <div class="page-header mb-6">
        <h2 class="text-xl font-bold">
          OAuth2 应用集成配置
        </h2>
        <p class="text-gray-500 mt-2">
          配置 OAuth2 单点登录相关参数
        </p>
      </div>

      <el-tabs class="oauth2-tabs">
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
              OAuth2 回调域名配置
            </div>
            <div class="text-gray-600 mb-3">
              <p>回调域名：此信息将在创建 OAuth2 授权应用时使用</p>
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
              <el-button v-if="!openEdit" type="primary" class="config-btn w-full" icon="setting" @click="openConfig">
                配置链接应用信息
              </el-button>
            </div>
            <div class="bg-gray-50 dark:bg-slate-800 p-5 border dark:border-slate-700 rounded-lg">
              <div class="flex items-center mb-4">
                <span class="info-label">OAuth2 服务器地址:</span>
                <el-input v-if="openEdit" v-model="config.server_url" class="info-value flex-1" placeholder="例如: https://oauth2.example.com" />
                <span v-else class="info-value">{{ config.server_url || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">授权页面地址:</span>
                <el-input v-if="openEdit" v-model="config.authorize_url" class="info-value flex-1" placeholder="例如: /oauth/authorize" />
                <span v-else class="info-value">{{ config.authorize_url }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">获取 Token URL:</span>
                <el-input v-if="openEdit" v-model="config.token_url" class="info-value flex-1" placeholder="例如: /oauth2/token" />
                <span v-else class="info-value">{{ config.token_url || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">Scope:</span>
                <el-input v-if="openEdit" v-model="config.scope" class="info-value flex-1" placeholder="例如: openid profile email" />
                <span v-else class="info-value">{{ config.scope || 'openid profile email' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">令牌端点认证方式:</span>
                <el-select v-if="openEdit" v-model="config.token_auth_method" class="info-value flex-1" placeholder="client_secret_post">
                  <el-option label="client_secret_post" value="client_secret_post" />
                  <el-option label="client_secret_basic" value="client_secret_basic" />
                </el-select>
                <span v-else class="info-value">{{ config.token_auth_method || 'client_secret_post' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">获取用户信息 URL:</span>
                <el-input v-if="openEdit" v-model="config.userinfo_url" class="info-value flex-1" placeholder="例如: /oauth2/userinfo" />
                <span v-else class="info-value">{{ config.userinfo_url || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">OIDC 发现配置 URL (.well-known):</span>
                <el-input v-if="openEdit" v-model="config.discovery_url" class="info-value flex-1" placeholder="例如: /.well-known/openid-configuration" />
                <span v-else class="info-value">{{ config.discovery_url || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">退出登录回调 URL:</span>
                <el-input v-if="openEdit" v-model="config.logout_url" class="info-value flex-1" placeholder="例如: /oauth2/logout" />
                <span v-else class="info-value">{{ config.logout_url || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">Client ID:</span>
                <el-input v-if="openEdit" v-model="config.app_id" class="info-value flex-1" />
                <span v-else class="info-value">{{ config.app_id || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">Client Secret:</span>
                <el-input v-if="openEdit" v-model="config.app_secret" class="info-value flex-1" type="text" />
                <span v-else class="info-value">{{ config.app_secret ? config.app_secret : '未配置' }}</span>
              </div>
              <el-divider />
              <div class="section-title mb-4">
                用户信息映射配置
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">用户名字段:</span>
                <el-input v-if="openEdit" v-model="config.user_name_field" class="info-value flex-1" placeholder="例如: data.name" />
                <span v-else class="info-value">{{ config.user_name_field || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">邮箱字段:</span>
                <el-input v-if="openEdit" v-model="config.user_email_field" class="info-value flex-1" placeholder="例如: data.email" />
                <span v-else class="info-value">{{ config.user_email_field || '未配置' }}</span>
              </div>
              <div class="flex items-center mb-4">
                <span class="info-label">用户唯一标识字段:</span>
                <el-input v-if="openEdit" v-model="config.user_id_field" class="info-value flex-1" placeholder="例如: data.sub 或 data.id" />
                <span v-else class="info-value">{{ config.user_id_field || '未配置' }}</span>
              </div>
              <div class="float-right">
                <el-button type="primary" plain icon="connection" @click="testConnection">
                  测试连接
                </el-button>
                <el-button v-if="openEdit" type="primary" icon="goods-filled" @click="update">
                  保存
                </el-button>
              </div>
              <div class="clear-both" />
            </div>
          </div>

          <el-divider />

          <div class="card-section">
            <div class="section-title text-amber-500">
              <el-icon><warning-filled /></el-icon>
              <span>温馨提示</span>
            </div>
            <div class="tips-content">
              <p class="tip-item">
                1. 请确保您的 OAuth2 服务器已正确配置，并支持授权码模式
              </p>
              <p class="tip-item">
                2. Client ID 和 Client Secret 是应用在 OAuth2 服务器中的唯一标识
              </p>
              <p class="tip-item">
                3. 用户信息映射字段应与 OAuth2 服务器返回的用户信息字段一致
              </p>
              <p class="tip-item">
                4. 请确保在 OAuth2 服务器上正确配置回调域名，否则会导致认证失败
              </p>
            </div>
          </div>
        </div>
      </el-tabs>
    </el-form>
  </div>
</template>

<script setup>
import { ElMessage } from 'element-plus'
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { getSystemOAuth2, setSystemOAuth2 } from "@/api/gaia/system";
import { WarningFilled } from '@element-plus/icons-vue'

defineOptions({
  name: 'IntegratedOAuth2',
})

const bc = ref()
const host = ref("")
const openEdit = ref(false)
const config = ref({
  id: 0,
  classify: 0,
  status: false,
  server_url: "",
  token_url: "",
  userinfo_url: "",
  logout_url: "",
  app_id: "",
  app_app: "",
  authorize_url: "",
  app_secret: "",
  user_name_field: "",
  user_email_field: "",
  user_id_field: "",
    scope: "openid profile email",
    token_auth_method: "client_secret_post",
    redirect_uri: "",
    discovery_url: "",
  test: false,
})

// 验证配置是否有效
const isConfigValid = computed(() => {
  return !!(
    config.value.server_url &&
    config.value.token_url &&
    config.value.userinfo_url &&
    config.value.app_id &&
    config.value.app_secret &&
    config.value.user_id_field
  );
})

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

// 打开编辑
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
  const base = config.value.server_url || ''
  const authPath = config.value.authorize_url || ''
  let authorizeUrl = authPath.startsWith('http://') || authPath.startsWith('https://') ? authPath : `${base}${authPath}`
  let redirectUriRaw = config.value.redirect_uri || `${location.protocol}//${location.host}/admin/api/base/auth2/callback`
  let redirectUri = encodeURIComponent(redirectUriRaw)
  let authorizeLike = authorizeUrl.includes('?') ? '&' : '?'
  let scope = encodeURIComponent(config.value.scope || 'openid profile email')
  window.open(`${authorizeUrl}${authorizeLike}&client_id=${encodeURIComponent(config.value.app_id)}&response_type=code&scope=${scope}&redirect_uri=${redirectUri}`)
}

const initForm = async() => {
  const res = await getSystemOAuth2()
  if (res.code === 0) {
    host.value = res.data.host
    config.value = res.data.config
  }
}

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

  const res = await setSystemOAuth2(config.value)
  if (res.code === 0) {
    ElMessage({
      type: 'success',
      message: '设置成功',
    })
    await initForm()
    openEdit.value = false
  }
}

// start
onMounted(() => {
  initForm()
  bc.value = new BroadcastChannel('oAuth2');
  bc.value.onmessage = async function (event) {
    // 尝试激活标签
    window.focus();
    config.value.test = true;
    config.value.code = event.data.code;
    if (config.value.status && !isConfigValid.value) {
      config.value.status = false;
      ElMessage({
        type: 'warning',
        message: '请先填写应用信息配置'
      });
      return;
    }

    const res = await setSystemOAuth2(config.value)
    if (res.code === 0) {
      ElMessage({
        type: 'success',
        message: '链接成功',
      })
    }
  };
})
// clone
onBeforeUnmount(() => {
  console.log('clone')
  bc.value.close()
})
</script>

<style lang="scss" scoped>
.system {
  @apply bg-white p-9 rounded dark:bg-slate-900;
}

.page-header {
  margin-bottom: 20px;
}

.card {
  @apply bg-white dark:bg-slate-900 rounded-lg overflow-hidden;
  box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.03);
}

.card-header {
  padding: 16px 20px;
}

.card-section {
  padding: 20px;
}

.section-title {
  @apply text-lg font-medium mb-4 flex items-center;

  .el-icon {
    margin-right: 8px;
  }
}

.info-label {
  width: 180px;
  @apply text-gray-600 dark:text-gray-400;
}

.info-value {
  @apply font-medium dark:text-gray-200;
}

.tips-content {
  @apply text-gray-600 dark:text-gray-400;
}

.tip-item {
  margin-bottom: 8px;
  line-height: 1.6;
}

.config-btn {
  margin-bottom: 16px;
}
</style>
