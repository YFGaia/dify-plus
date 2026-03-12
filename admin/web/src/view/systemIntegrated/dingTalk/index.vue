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
              第三方用户名提取配置
            </div>
            <div class="bg-gray-50 dark:bg-slate-800 p-5 border dark:border-slate-700 rounded-lg">
              <!-- 基础配置 -->
              <div class="flex items-center mb-4">
                <span class="info-label">第三方的URL:</span>
                <el-input
                  v-if="openEdit"
                  v-model="emailApiConfig.url"
                  class="info-value flex-1"
                  placeholder="请输入钉钉id获取用户名的链接地址"
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

                  <!-- Params标签 -->
                  <el-tab-pane :label="`Params (${getParamsCount()})`" name="params">
                    <div class="params-editor">
                      <div class="text-xs text-gray-400 mb-3">
                        配置 URL 查询参数，系统将自动判断使用 ? 或 &amp; 拼接到请求地址后
                      </div>
                      <div
                        v-for="(param, index) in emailApiParams"
                        :key="index"
                        class="flex items-center mb-2 gap-2"
                      >
                        <el-input
                          v-model="param.key"
                          placeholder="参数名（如 userId）"
                          style="flex: 2"
                        />
                        <el-select v-model="param.value_type" style="width: 120px; flex-shrink: 0">
                          <el-option label="String（自定义）" value="string" />
                          <el-option label="钉钉 ID（自动）" value="ding_id" />
                        </el-select>
                        <template v-if="param.value_type === 'ding_id'">
                          <el-input
                            disabled
                            placeholder="登录时自动填入钉钉 ID"
                            style="flex: 2"
                          />
                        </template>
                        <template v-else>
                          <el-input
                            v-model="param.value"
                            placeholder="参数值"
                            style="flex: 2"
                          />
                        </template>
                        <el-button
                          type="danger"
                          icon="delete"
                          circle
                          size="small"
                          @click="removeParam(index)"
                        />
                      </div>
                      <el-button type="primary" plain icon="plus" size="small" @click="addParam">
                        添加参数
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
                          <el-radio label="form-data">
                            form-data
                          </el-radio>
                          <el-radio label="x-www-form-urlencoded">
                            x-www-form-urlencoded
                          </el-radio>
                          <el-radio label="raw">
                            raw (JSON)
                          </el-radio>
                        </el-radio-group>
                      </div>

                      <!-- form-data -->
                      <div v-if="emailApiConfig.body_type === 'form-data'" class="body-content">
                        <div v-for="(item, index) in bodyFormData" :key="index" class="flex items-center mb-2 gap-2">
                          <el-input
                            v-model="item.key"
                            placeholder="字段名"
                            class="flex-1"
                          />
                          <el-select v-model="item.value_type" style="width: 120px">
                            <el-option label="String" value="string" />
                            <el-option label="Int" value="int" />
                            <el-option label="Bool" value="bool" />
                            <el-option label="钉钉 ID" value="ding_id" />
                          </el-select>
                          <template v-if="item.value_type === 'ding_id'">
                            <el-input disabled placeholder="运行时自动填充" class="flex-1" />
                          </template>
                          <template v-else-if="item.value_type === 'bool'">
                            <el-switch v-model="item.value" :active-value="'true'" :inactive-value="'false'" class="flex-1" />
                          </template>
                          <template v-else-if="item.value_type === 'int'">
                            <el-input v-model="item.value" type="number" placeholder="整数值" class="flex-1" />
                          </template>
                          <template v-else>
                            <el-input v-model="item.value" placeholder="字段值" class="flex-1" />
                          </template>
                          <el-button
                            type="danger"
                            icon="delete"
                            circle
                            size="small"
                            @click="removeFormDataItem(index)"
                          />
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
                          />
                          <el-select v-model="item.value_type" style="width: 120px">
                            <el-option label="String" value="string" />
                            <el-option label="Int" value="int" />
                            <el-option label="Bool" value="bool" />
                            <el-option label="钉钉 ID" value="ding_id" />
                          </el-select>
                          <template v-if="item.value_type === 'ding_id'">
                            <el-input disabled placeholder="运行时自动填充" class="flex-1" />
                          </template>
                          <template v-else-if="item.value_type === 'bool'">
                            <el-switch v-model="item.value" :active-value="'true'" :inactive-value="'false'" class="flex-1" />
                          </template>
                          <template v-else-if="item.value_type === 'int'">
                            <el-input v-model="item.value" type="number" placeholder="整数值" class="flex-1" />
                          </template>
                          <template v-else>
                            <el-input v-model="item.value" placeholder="字段值" class="flex-1" />
                          </template>
                          <el-button
                            type="danger"
                            icon="delete"
                            circle
                            size="small"
                            @click="removeUrlEncodedItem(index)"
                          />
                        </div>
                        <el-button type="primary" plain icon="plus" size="small" @click="addUrlEncodedItem">
                          添加字段
                        </el-button>
                      </div>

                      <!-- raw JSON -->
                      <div v-if="emailApiConfig.body_type === 'raw'" class="body-content">
                        <div class="flex justify-end mb-2">
                          <el-button
                            type="primary"
                            plain
                            size="small"
                            @click="insertDingIdMarker"
                          >
                            插入钉钉 ID
                          </el-button>
                        </div>
                        <el-input
                          ref="rawBodyTextarea"
                          v-model="bodyRaw"
                          type="textarea"
                          :rows="8"
                          placeholder="请输入JSON格式，例如: {&quot;userId&quot;: &quot;$&lt;{[ding_id]}&gt;&quot;}"
                          @click="saveRawCursorPos"
                          @keyup="saveRawCursorPos"
                        />
                        <div class="text-xs text-gray-400 mt-1">
                          点击「插入钉钉 ID」将在光标位置插入 <code>$&lt;{[ding_id]}&gt;</code> 占位符，发送请求时自动替换为实际钉钉 ID
                        </div>
                      </div>
                    </div>
                  </el-tab-pane>

                  <!-- Authorization标签 -->
                  <el-tab-pane label="Authorization" name="authorization">
                    <div class="auth-editor">
                      <div class="mb-3">
                        <el-radio-group v-model="emailApiConfig.authorization.type">
                          <el-radio label="none">
                            None
                          </el-radio>
                          <el-radio label="bearer">
                            Bearer Token
                          </el-radio>
                          <el-radio label="basic">
                            Basic Auth
                          </el-radio>
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
                  <div class="text-sm font-medium mb-2">
                    Headers:
                  </div>
                  <div v-for="(value, key) in emailApiConfig.headers" :key="key" class="flex items-center mb-1">
                    <span class="info-label text-sm">{{ key }}:</span>
                    <span class="info-value text-sm ml-2">{{ value }}</span>
                  </div>
                </div>
                <div v-if="emailApiConfig.authorization.type !== 'none'" class="mb-3">
                  <div class="text-sm font-medium mb-2">
                    Authorization:
                  </div>
                  <span class="info-value text-sm">{{ emailApiConfig.authorization.type === 'bearer' ? 'Bearer Token' : 'Basic Auth' }}</span>
                </div>
              </div>

              <!-- 用户名路径 -->
              <div class="flex items-center mb-4 mt-4">
                <span class="info-label">用户名路径:</span>
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
              <div v-if="openEdit" class="flex justify-end gap-2">
                <el-button icon="promotion" @click="openTestDialog">
                  测试配置
                </el-button>
                <el-button type="primary" icon="goods-filled" @click="update">
                  保存
                </el-button>
              </div>
              <div class="clear-both" />
            </div>
          </div>


          <!-- Token 列表 -->
          <div class="card-section">
            <div class="flex items-center justify-between mb-3">
              <div class="section-title mb-0">
                转发 Token 列表
              </div>
              <div class="flex items-center gap-2">
                <span class="text-gray-500 text-sm">{{ forwardTokenList.length }}/20 个</span>
                <el-button
                  type="primary"
                  size="small"
                  :disabled="forwardTokenList.length >= 20"
                  @click="showCreateTokenDialog = true"
                >
                  + 新增 Token
                </el-button>
              </div>
            </div>

            <el-table :data="forwardTokenList" border size="small" class="w-full">
              <el-table-column label="token" prop="seq" align="center">
                <template #default="{ row }">
                  <span class="font-mono text-xs">{{ row.id }}</span>
                </template>
              </el-table-column>
              <el-table-column label="创建时间" prop="created_at" width="180">
                <template #default="{ row }">
                  {{ formatDate(row.created_at) }}
                </template>
              </el-table-column>
              <el-table-column label="操作" width="80" align="center">
                <template #default="{ row }">
                  <el-button type="danger" link size="small" @click="openDeleteTokenDialog(row.seq)">
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
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

      <!-- 新增 Token 弹窗：前端随机生成 → 保存到后端 → 自动复制到剪贴板并提示 -->
      <el-dialog v-model="showCreateTokenDialog" title="新增转发 Token" width="480px" :close-on-click-modal="false">
        <div v-if="!newPlainToken">
          <p class="text-gray-600 mb-4">
            点击「生成并保存」将随机生成 Token，并返回两种凭证：
            <br />2）Token Secret（用于生成 Authorization: Bearer ... 的签名密钥）
            <br />两者仅展示一次，请务必复制到安全位置保管。
          </p>
        </div>
        <div v-else>
          <el-alert
            type="success"
            title="Token 已生成，并已将 Token Secret 复制到剪贴板，请妥善保管（以下两项仅展示一次）。"
            :closable="false"
            class="mb-4"
          />
          <div class="mb-3">
            <div class="text-xs text-gray-500 mb-1">明文 Token（可用于 X-Forward-Token）</div>
            <el-input v-model="newPlainToken" readonly>
              <template #append>
                <el-button @click="copyToken(newPlainToken)">
                  复制 Token
                </el-button>
              </template>
            </el-input>
          </div>
        </div>
        <template #footer>
          <el-button v-if="!newPlainToken" @click="showCreateTokenDialog = false">
            取消
          </el-button>
          <el-button v-if="!newPlainToken" type="primary" :loading="creatingToken" @click="handleCreateToken">
            生成并保存
          </el-button>
          <el-button
            v-else
            type="primary"
            @click="
              showCreateTokenDialog = false;
              newPlainToken = '';
              initForm();
            "
          >
            完成
          </el-button>
        </template>
      </el-dialog>

      <!-- 删除 Token 弹窗（需密码） -->
      <el-dialog v-model="showDeleteTokenDialog" title="删除转发 Token" width="420px" :close-on-click-modal="false">
        <p class="text-gray-600 mb-4">
          删除操作需要验证您的登录密码，请输入后确认。
        </p>
        <el-input v-model="deleteTokenPassword" type="password" placeholder="请输入您的登录密码" show-password />
        <template #footer>
          <el-button @click="showDeleteTokenDialog = false; deleteTokenPassword = ''; deletingTokenSeq = null">
            取消
          </el-button>
          <el-button type="danger" :loading="deletingToken" @click="handleDeleteToken">
            确认删除
          </el-button>
        </template>
      </el-dialog>

      <!-- 测试配置弹窗 -->
      <el-dialog
        v-model="showTestDialog"
        title="测试邮箱 API 配置"
        width="680px"
        :close-on-click-modal="false"
        destroy-on-close
      >
        <div class="test-dialog-content">
          <!-- 测试用钉钉 ID 输入 -->
          <div class="mb-4">
            <div class="text-sm font-medium mb-2">
              测试用钉钉 ID
              <span class="text-gray-400 font-normal ml-1">（可选，填入真实钉钉 ID 可验证完整流程；留空则用占位符代替）</span>
            </div>
            <el-input
              v-model="testDingId"
              placeholder="例如：0123456789abcdef（留空用占位符）"
              clearable
              @keyup.enter="runTest"
            />
          </div>

          <!-- 测试中状态 -->
          <div v-if="testLoading" class="flex items-center justify-center py-8 text-gray-500">
            <el-icon class="mr-2 animate-spin"><loading /></el-icon>
            正在发送测试请求...
          </div>

          <!-- 测试结果 -->
          <div v-if="testResult && !testLoading">
            <!-- 状态码 -->
            <div class="flex items-center mb-3 gap-3">
              <el-tag :type="testResult.is_valid ? 'success' : 'danger'" size="large">
                HTTP {{ testResult.status_code }}
              </el-tag>
              <span v-if="testResult.is_valid" class="text-green-600 font-medium">配置有效</span>
              <span v-else class="text-red-500 font-medium">{{ testResult.error_message }}</span>
            </div>

            <!-- 邮箱字段解析预览 -->
            <div v-if="testResult.email_field_preview" class="mb-3">
              <div class="text-sm font-medium mb-1">邮箱字段解析</div>
              <el-tag type="success">{{ testResult.email_field_preview }}</el-tag>
            </div>

            <!-- 响应 Body -->
            <div class="mb-3">
              <div class="flex items-center justify-between mb-1">
                <div class="text-sm font-medium">
                  响应 Body
                </div>
                <el-button
                  v-if="testResult.body && typeof testResult.body === 'object'"
                  size="small"
                  text
                  type="danger"
                  class="!text-red-500"
                  @click="testBodyExpanded = !testBodyExpanded"
                >
                  {{ testBodyExpanded ? '折叠' : '展开选择解析' }}
                </el-button>
              </div>
              <div class="test-body-container">
                <div v-if="typeof testResult.body === 'object'" class="json-tree">
                  <div v-if="testBodyExpanded">
                    <div
                      v-for="(item, path) in flattenJSON(testResult.body)"
                      :key="path"
                      class="json-node flex items-center py-1 px-2 hover:bg-blue-50 dark:hover:bg-slate-700 cursor-pointer rounded"
                    >
                      <span class="json-path text-gray-500 text-xs mr-2">{{ path }}</span>
                      <span class="json-value text-sm font-mono">{{ typeof item === 'string' ? item : JSON.stringify(item) }}</span>
                      <el-button
                        v-if="typeof item === 'string' || typeof item === 'number'"
                        size="small"
                        text
                        type="primary"
                        class="ml-auto"
                        @click="selectJsonField(path, item)"
                      >
                        选为用户名字段
                      </el-button>
                    </div>
                  </div>
                  <pre v-else class="text-xs bg-gray-50 dark:bg-slate-800 p-3 rounded overflow-auto max-h-48">{{ JSON.stringify(testResult.body, null, 2) }}</pre>
                </div>
                <pre v-else class="text-xs bg-gray-50 dark:bg-slate-800 p-3 rounded overflow-auto max-h-48">{{ testResult.body }}</pre>
              </div>
            </div>
          </div>

          <!-- 错误提示 -->
          <div v-if="testError" class="mt-2">
            <el-alert :title="testError" type="error" :closable="false" />
          </div>
        </div>
        <template #footer>
          <el-button @click="showTestDialog = false">
            关闭
          </el-button>
          <el-button :loading="testLoading" @click="runTest">
            重新测试
          </el-button>
        </template>
      </el-dialog>
    </el-form>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { QuestionFilled, Loading } from '@element-plus/icons-vue'
import { useClipboard } from '@vueuse/core'
const { copy, isSupported } = useClipboard()
import { getSystemDingTalk, setSystemDingTalk, getForwardTokens, createForwardToken, deleteForwardToken, testEmailApiConfig, getDingTalkTestAuthUrl } from "@/api/gaia/system";

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
  request_param_field: '',   // 旧格式兼容字段
  params: null,              // 新格式: null 表示使用旧格式，[] 或有元素表示新格式
  body_type: 'raw',          // form-data | x-www-form-urlencoded | raw
  headers: {},
  authorization: {
    type: 'none',            // none | bearer | basic
    token: '',
    username: '',
    password: ''
  },
  response_email_field: 'data[0].userName'
})

// Params 参数列表（用于编辑，新格式）
const emailApiParams = ref([])  // [{ key, value_type, value }]

// 转发集成配置
const forwardConfig = ref({ enabled: false, tokens: [] })

// 转发 Token 列表
const forwardTokenList = ref([])

// 新增 Token 弹窗（前端随机生成 → 保存 → 复制到剪贴板）
const showCreateTokenDialog = ref(false)
// 明文 token：可用于 X-Forward-Token 模式
const newPlainToken = ref('')
// token_secret：用于生成 Authorization: Bearer ... 的 HMAC 密钥
const creatingToken = ref(false)

// 删除 Token 弹窗
const showDeleteTokenDialog = ref(false)
const deleteTokenPassword = ref('')
const deletingTokenSeq = ref(null)
const deletingToken = ref(false)

// 格式化日期
const formatDate = (dateStr) => {
  if (!dateStr) return ''
  return new Date(dateStr).toLocaleString('zh-CN', { hour12: false })
}

// 加载转发 Token 列表
const loadForwardTokens = async () => {
  const res = await getForwardTokens()
  if (res.code === 0) {
    forwardTokenList.value = res.data?.tokens || []
  }
}

// 生成随机 Token
const generateToken = () => {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  let token = ''
  for (let i = 0; i < 48; i++) {
    token += chars.charAt(Math.floor(Math.random() * chars.length))
  }
  return token
}

// 复制 Token（使用 VueUse useClipboard，并提示）
const copyToken = async (token) => {
  if (!token) return
  try {
    if (copy) {
      await copy(token)
    } else {
      await navigator.clipboard.writeText(token)
    }
    ElMessage({ type: 'success', message: 'Token 已复制到剪贴板' })
    // eslint-disable-next-line no-unused-vars
  } catch (e) {
    ElMessage({ type: 'warning', message: '复制失败，请手动复制' })
  }
}

// 生成并保存 Token：前端随机生成 → 调用接口保存（后端存 SHA256）→ 自动复制到剪贴板并提示
const handleCreateToken = async () => {
  const token = generateToken()
  creatingToken.value = true
  try {
    const res = await createForwardToken({ token })
    if (res.code === 0) {
      newPlainToken.value = res.data?.token_secret || ''
      // 默认复制 token_secret，方便用于 Bearer Token 生成
      await copyToken(newPlainToken.value)
    } else {
      ElMessage({ type: 'error', message: res.msg || '保存失败' })
    }
  } finally {
    creatingToken.value = false
  }
}

// 打开删除 Token 弹窗
const openDeleteTokenDialog = (seq) => {
  deletingTokenSeq.value = seq
  deleteTokenPassword.value = ''
  showDeleteTokenDialog.value = true
}

// 删除 Token
const handleDeleteToken = async () => {
  if (!deleteTokenPassword.value) {
    ElMessage({ type: 'warning', message: '请输入登录密码' })
    return
  }
  deletingToken.value = true
  const res = await deleteForwardToken(deletingTokenSeq.value, deleteTokenPassword.value)
  deletingToken.value = false
  if (res.code === 0) {
    ElMessage({ type: 'success', message: '删除成功' })
    showDeleteTokenDialog.value = false
    deleteTokenPassword.value = ''
    deletingTokenSeq.value = null
    await initForm()
  } else {
    ElMessage({ type: 'error', message: res.msg || '删除失败' })
  }
}

// 标签页管理
const activeTab = ref('headers')

// Headers编辑器数组
const emailApiHeaders = ref([{ key: '', value: '' }])

// Body编辑器数据（根据body_type不同使用不同格式）
const bodyFormData = ref([{ key: '', value_type: 'string', value: '' }])  // form-data
const bodyUrlEncoded = ref([{ key: '', value_type: 'string', value: '' }])  // x-www-form-urlencoded
const bodyRaw = ref('')  // raw JSON

// Raw 模式光标位置
const rawBodyTextarea = ref(null)
let rawCursorPos = 0

// 测试配置弹窗
const showTestDialog = ref(false)
const testDingId = ref('')
const testLoading = ref(false)
const testResult = ref(null)
const testError = ref('')
const testBodyExpanded = ref(false)

// 验证配置是否有效
const isConfigValid = computed(() => {
  return !!(config.value.corp_id && config.value.agent_id && config.value.app_key && config.value.app_secret);
})

// 是否使用新格式（params 不为 null）
const isNewParamsFormat = computed(() => emailApiConfig.value.params !== null)

// 验证邮箱API配置是否有效
const isEmailApiConfigValid = computed(() => {
  if (!emailApiConfig.value.enabled) {
    return true; // 未启用时认为有效
  }
  // 新格式：只需要 URL、method、response_email_field
  return !!(emailApiConfig.value.url && emailApiConfig.value.method && emailApiConfig.value.response_email_field)
})

// 转发配置生效前的前置条件：至少 1 个 Token + 启用并配置「第三方邮箱配置」
const validateForwardConfigPrerequisites = (showMessage = true) => {
  const hasToken = (forwardTokenList.value?.length || 0) >= 1
  const emailApiEnabled = !!emailApiConfig.value.enabled
  const hasEmailApiURL = !!(emailApiConfig.value?.url && emailApiConfig.value.url.trim() !== '')

  if (!hasToken) {
    if (showMessage) ElMessage({ type: 'warning', message: '请至少新增 1 个转发 Token 后再保存配置' })
    return false
  }
  if (!emailApiEnabled || !hasEmailApiURL) {
    if (showMessage) ElMessage({ type: 'warning', message: '请先启用并配置「第三方邮箱配置」后再保存配置' })
    return false
  }
  return true
}

// Params 管理（新格式）
const getParamsCount = () => {
  return emailApiParams.value.filter(p => p.key).length
}

const addParam = () => {
  // 首次添加时将 params 设为数组，切换到新格式
  if (emailApiConfig.value.params === null) {
    emailApiConfig.value.params = []
  }
  emailApiParams.value.push({ key: '', value_type: 'string', value: '' })
}

const removeParam = (index) => {
  emailApiParams.value.splice(index, 1)
}

// Raw 模式：记录光标位置
const saveRawCursorPos = (event) => {
  rawCursorPos = event.target.selectionStart
}

// Raw 模式：在光标位置插入钉钉 ID 标记（任务 8.8~8.9）
const DING_ID_MARKER = '$<{[ding_id]}>'
const insertDingIdMarker = async () => {
  const text = bodyRaw.value
  const pos = rawCursorPos ?? text.length
  bodyRaw.value = text.substring(0, pos) + DING_ID_MARKER + text.substring(pos)
  rawCursorPos = pos + DING_ID_MARKER.length
  // 恢复光标位置
  await nextTick()
  const textarea = rawBodyTextarea.value?.$el?.querySelector('textarea')
  if (textarea) {
    textarea.selectionStart = rawCursorPos
    textarea.selectionEnd = rawCursorPos
    textarea.focus()
  }
}

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

// Body form-data管理
const addFormDataItem = () => {
  bodyFormData.value.push({ key: '', value_type: 'string', value: '' })
}

const removeFormDataItem = (index) => {
  bodyFormData.value.splice(index, 1)
}

// Body x-www-form-urlencoded管理
const addUrlEncodedItem = () => {
  bodyUrlEncoded.value.push({ key: '', value_type: 'string', value: '' })
}

const removeUrlEncodedItem = (index) => {
  bodyUrlEncoded.value.splice(index, 1)
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

// 仅保存「转发集成」与「第三方钉钉 ID 匹配用户 API」配置（与主保存共用 update，保证整份 config 一致）
const saveForwardAndDingIdConfig = () => {
  update()
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

// 测试连接：先保存当前配置，再打开钉钉授权页，扫码完成后根据回调结果提示成功/失败
const testConnection = async () => {
  if (!isConfigValid.value) {
    ElMessage({
      type: 'warning',
      message: '请先填写完整的应用信息配置'
    });
    return;
  }

  // 先保存配置，使后端能读到最新的 AppKey/AppSecret
  config.value.test = false
  const saveRes = await setSystemDingTalk(config.value)
  if (saveRes.code !== 0) {
    ElMessage({ type: 'error', message: saveRes.msg || '保存失败' })
    return
  }

  let authURL = ''
  try {
    const urlRes = await getDingTalkTestAuthUrl()
    if (urlRes.code !== 0 || !urlRes.data?.auth_url) {
      ElMessage({ type: 'error', message: urlRes.msg || '获取授权地址失败' })
      return
    }
    authURL = urlRes.data.auth_url
  } catch (e) {
    ElMessage({ type: 'error', message: '获取授权地址失败：' + (e.message || '') })
    return
  }

  const width = 520
  const height = 620
  const left = Math.round((window.screen.width - width) / 2)
  const top = Math.round((window.screen.height - height) / 2)
  const popup = window.open(
    authURL,
    'dingtalk_test',
    `width=${width},height=${height},left=${left},top=${top},toolbar=no,menubar=no,scrollbars=yes`
  )
  if (!popup) {
    ElMessage({ type: 'warning', message: '请允许弹窗后重试' })
    return
  }

  const timeoutMs = 120000
  const timeoutId = setTimeout(() => {
    try {
      if (popup && !popup.closed) popup.close()
    } catch (_) {}
    window.removeEventListener('message', onMessage)
    ElMessage({ type: 'info', message: '未在限定时间内完成扫码，已取消' })
  }, timeoutMs)

  const onMessage = (event) => {
    if (event.data?.type !== 'dingtalk_test_result') return
    clearTimeout(timeoutId)
    window.removeEventListener('message', onMessage)
    try {
      if (popup && !popup.closed) popup.close()
    } catch (_) {}
    if (event.data.success) {
      ElMessage({ type: 'success', message: '连接成功：已通过钉钉扫码验证' })
    } else {
      ElMessage({ type: 'error', message: event.data.message || '验证失败' })
    }
  }
  window.addEventListener('message', onMessage)
}

// 监听URL变化，自动启用/禁用邮箱API配置
watch(() => emailApiConfig.value.url, (newUrl) => {
  emailApiConfig.value.enabled = !!newUrl && newUrl.trim() !== ''
})

// 监听 params 数组变化，同步到 emailApiConfig.params
watch(emailApiParams, (newParams) => {
  if (newParams.length > 0 || emailApiConfig.value.params !== null) {
    emailApiConfig.value.params = newParams.length > 0 ? newParams : []
  }
}, { deep: true })

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
          const api = configData.email_api
          const url = api.url || ''
          emailApiConfig.value = {
            enabled: !!url && url.trim() !== '',
            url: url,
            method: api.method || 'GET',
            request_param_field: api.request_param_field || '',
            params: api.params !== undefined ? api.params : null,  // 保留新旧格式标识
            body_type: api.body_type || 'raw',
            headers: api.headers || {},
            authorization: api.authorization || { type: 'none', token: '', username: '', password: '' },
            response_email_field: api.response_email_field || 'data[0].userName'
          }

          // 转换headers为数组格式供编辑
          emailApiHeaders.value = headersObjectToArray(api.headers)

          // 加载 params（新格式）
          if (Array.isArray(api.params)) {
            emailApiParams.value = api.params.map(p => ({
              key: p.key || '',
              value_type: p.value_type || 'string',
              value: p.value || ''
            }))
          } else {
            emailApiParams.value = []
          }

          // 转换body数据（新格式支持 value_type）
          if (api.body_data) {
            const toBodyField = (item) => ({
              key: item.key || '',
              value_type: item.value_type || 'string',
              value: item.value || ''
            })
            const formDataRaw = api.body_data.form_data || []
            bodyFormData.value = formDataRaw.length > 0
              ? formDataRaw.map(toBodyField)
              : [{ key: '', value_type: 'string', value: '' }]

            const urlencodedRaw = api.body_data.urlencoded || []
            bodyUrlEncoded.value = urlencodedRaw.length > 0
              ? urlencodedRaw.map(toBodyField)
              : [{ key: '', value_type: 'string', value: '' }]

            bodyRaw.value = api.body_data.raw || ''
          } else {
            bodyFormData.value = [{ key: '', value_type: 'string', value: '' }]
            bodyUrlEncoded.value = [{ key: '', value_type: 'string', value: '' }]
            bodyRaw.value = ''
          }
        }

        // 解析转发集成配置
        if (configData.forward_config) {
          forwardConfig.value = {
            enabled: configData.forward_config.enabled || false,
            tokens: configData.forward_config.tokens || [],
          }
        }
      } catch (e) {
        console.error('解析邮箱API配置失败:', e)
      }
    }
  }
  // 加载 Token 列表
  await loadForwardTokens()
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

  // 若已配置转发 Token，则在保存前校验前置条件（至少 1 个 Token + 启用并配置「第三方邮箱配置」）
  if ((forwardTokenList.value?.length || 0) > 0 && !validateForwardConfigPrerequisites(true)) {
    return
  }

  // 将emailApiConfig合并到config字段中
  const headers = headersArrayToObject()
  const emailCfg = buildCurrentEmailConfig()

  const configData = {
    email_api: {
      ...emailCfg,
      headers,
    },
    forward_config: {
      enabled: forwardConfig.value.enabled,
      tokens: forwardConfig.value.tokens,
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

// 测试配置相关（任务 9.2~9.10）
const openTestDialog = async () => {
  testResult.value = null
  testError.value = ''
  testBodyExpanded.value = false
  testDingId.value = ''
  showTestDialog.value = true
  // 打开即自动执行一次测试
  await runTest()
}

const buildCurrentEmailConfig = () => {
  const headers = headersArrayToObject()
  const bodyData = {}
  if (emailApiConfig.value.method !== 'GET') {
    if (emailApiConfig.value.body_type === 'form-data') {
      bodyData.form_data = bodyFormData.value.filter(item => item.key).map(item => ({
        key: item.key,
        value_type: item.value_type || 'string',
        value: item.value || ''
      }))
    } else if (emailApiConfig.value.body_type === 'x-www-form-urlencoded') {
      bodyData.urlencoded = bodyUrlEncoded.value.filter(item => item.key).map(item => ({
        key: item.key,
        value_type: item.value_type || 'string',
        value: item.value || ''
      }))
    } else {
      bodyData.raw = bodyRaw.value
    }
  }

  const params = isNewParamsFormat.value
    ? emailApiParams.value.filter(p => p.key).map(p => ({
        key: p.key,
        value_type: p.value_type || 'string',
        value: p.value || ''
      }))
    : null

  return {
    ...emailApiConfig.value,
    headers,
    body_data: bodyData,
    params,
  }
}

const runTest = async () => {
  testLoading.value = true
  testError.value = ''
  testResult.value = null
  try {
    const config = buildCurrentEmailConfig()
    const res = await testEmailApiConfig({
      config,
      test_ding_id: testDingId.value || undefined
    })
    if (res.code === 0) {
      testResult.value = res.data
      testBodyExpanded.value = false
    } else {
      testError.value = res.msg || '测试失败'
    }
  } catch (e) {
    testError.value = '请求错误：' + e.message
  } finally {
    testLoading.value = false
  }
}

// 将 JSON 对象展平为路径-值映射（用于 JSON 树选择）
const flattenJSON = (obj, prefix = '', result = {}) => {
  if (obj === null || typeof obj !== 'object') {
    result[prefix || 'value'] = obj
    return result
  }
  if (Array.isArray(obj)) {
    obj.forEach((item, i) => {
      flattenJSON(item, prefix ? `${prefix}[${i}]` : `[${i}]`, result)
    })
  } else {
    Object.entries(obj).forEach(([k, v]) => {
      const path = prefix ? `${prefix}.${k}` : k
      if (v !== null && typeof v === 'object') {
        flattenJSON(v, path, result)
      } else {
        result[path] = v
      }
    })
  }
  return result
}

// 选择 JSON 字段作为邮箱提取路径（任务 9.8~9.9）
const selectJsonField = (path, value) => {
  if (typeof value === 'string' || typeof value === 'number') {
    emailApiConfig.value.response_email_field = path
    ElMessage({ type: 'success', message: `已选择字段：${path}` })
    // 选中后关闭测试弹窗
    showTestDialog.value = false
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
