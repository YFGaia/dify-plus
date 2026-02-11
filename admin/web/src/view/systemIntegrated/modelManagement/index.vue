<template>
  <div class="model-management">
    <el-card class="box-card">
      <template #header>
        <div class="card-header">
          <span class="title">模型管理</span>
          <el-button
            type="primary"
            :loading="saving"
            @click="saveAll"
          >
            保存配置
          </el-button>
        </div>
      </template>

      <div v-loading="loading" class="provider-list">
        <el-empty
          v-if="!loading && providerList.length === 0"
          description="暂无提供商配置"
        />

        <div
          v-for="provider in providerList"
          :key="provider.provider_name"
          class="provider-item"
        >
          <div class="provider-header">
            <div class="provider-info">
              <el-icon class="provider-icon">
                <cpu />
              </el-icon>
              <span class="provider-name">{{ getProviderDisplayName(provider.provider_name) }}</span>
              <el-tag
                v-if="provider.enabled"
                type="success"
                size="small"
              >
                已开启
              </el-tag>
              <el-tag
                v-else
                type="info"
                size="small"
              >
                已关闭
              </el-tag>
            </div>
            <div class="provider-actions">
              <el-button
                size="small"
                :type="provider.enabled ? 'danger' : 'success'"
                @click="toggleProvider(provider)"
              >
                {{ provider.enabled ? '关闭' : '开启' }}
              </el-button>
              <el-button
                size="small"
                @click="testCredentials(provider.provider_name)"
              >
                测试凭证
              </el-button>
            </div>
          </div>

          <el-collapse-transition>
            <div v-if="provider.enabled" class="provider-models">
              <div class="models-header">
                <span class="models-title">可用模型</span>
                <div class="models-actions">
                  <el-button
                    size="small"
                    text
                    type="primary"
                    @click="selectAllModels(provider)"
                  >
                    全选
                  </el-button>
                  <el-button
                    size="small"
                    text
                    type="info"
                    @click="clearAllModels(provider)"
                  >
                    清空
                  </el-button>
                </div>
              </div>

              <div v-if="provider.available_models && provider.available_models.length > 0" class="models-select-wrapper">
                <el-select
                  v-model="provider.models"
                  multiple
                  filterable
                  collapse-tags
                  collapse-tags-tooltip
                  :max-collapse-tags="5"
                  placeholder="请选择模型"
                  class="models-select"
                  @change="onModelSelectChange(provider)"
                >
                  <el-option
                    v-for="model in provider.available_models"
                    :key="model.id"
                    :label="model.name"
                    :value="model.id"
                  />
                </el-select>
                <div class="selected-count">
                  已选择 {{ provider.models?.length || 0 }} / {{ provider.available_models.length }} 个模型
                </div>
              </div>

              <el-empty
                v-else
                description="未找到可用模型，请先在Dify中配置该提供商"
                :image-size="80"
              />
            </div>
          </el-collapse-transition>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Cpu } from '@element-plus/icons-vue'
import {
  getProviderListApi,
  updateProviderConfigApi,
  testProviderCredentialsApi
} from '@/api/modelProvider'

defineOptions({
  name: 'IntegratedModelManagement'
})

const loading = ref(false)
const saving = ref(false)
const providerList = ref([])

// 提供商显示名称映射
const providerDisplayNames = {
  openai: 'OpenAI',
  tongyi: '千问（通义）',
  google: 'Google Gemini'
}

const getProviderDisplayName = (providerName) => {
  return providerDisplayNames[providerName] || providerName
}

// 获取提供商列表
const getProviderList = async() => {
  loading.value = true
  try {
    const res = await getProviderListApi()
    if (res.code === 0) {
      // 处理数据，添加selectedModelsSet用于checkbox绑定
      providerList.value = res.data.map(provider => {
        const selectedModelsSet = {}
        if (provider.models && Array.isArray(provider.models)) {
          provider.models.forEach(modelId => {
            selectedModelsSet[modelId] = true
          })
        }
        return {
          ...provider,
          selectedModelsSet
        }
      })
    } else {
      ElMessage.error(res.msg || '获取提供商列表失败')
    }
  } catch (error) {
    console.error('获取提供商列表失败', error)
    ElMessage.error('获取提供商列表失败')
  } finally {
    loading.value = false
  }
}

// 切换提供商开关
const toggleProvider = (provider) => {
  provider.enabled = !provider.enabled
  if (!provider.enabled) {
    // 关闭时清空选中的模型
    provider.selectedModelsSet = {}
    provider.models = []
  }
}

// 下拉框选择变化
const onModelSelectChange = (provider) => {
  // 同步更新 selectedModelsSet（保持兼容性）
  provider.selectedModelsSet = {}
  if (provider.models && Array.isArray(provider.models)) {
    provider.models.forEach(modelId => {
      provider.selectedModelsSet[modelId] = true
    })
  }
}

// 全选模型
const selectAllModels = (provider) => {
  if (provider.available_models && provider.available_models.length > 0) {
    provider.models = provider.available_models.map(model => model.id)
    onModelSelectChange(provider)
  }
}

// 清空模型选择
const clearAllModels = (provider) => {
  provider.selectedModelsSet = {}
  provider.models = []
}

// 保存所有配置
const saveAll = async() => {
  saving.value = true
  try {
    // 逐个保存提供商配置
    for (const provider of providerList.value) {
      await updateProviderConfigApi({
        provider_name: provider.provider_name,
        enabled: provider.enabled,
        models: provider.models || []
      })
    }
    ElMessage.success('保存成功')
  } catch (error) {
    console.error('保存配置失败', error)
    ElMessage.error('保存配置失败')
  } finally {
    saving.value = false
  }
}

// 测试凭证
const testCredentials = async(providerName) => {
  try {
    const res = await testProviderCredentialsApi(providerName)
    if (res.code === 0) {
      ElMessageBox.alert(
        `提供商: ${res.data.provider}\nAPI Key: ${res.data.api_key}\n凭证状态: ${res.data.has_api_key ? '已配置' : '未配置'}`,
        '凭证测试结果',
        {
          confirmButtonText: '确定',
          type: 'success'
        }
      )
    } else {
      ElMessage.error(res.msg || '测试失败')
    }
  } catch (error) {
    console.error('测试凭证失败', error)
    ElMessage.error('测试凭证失败：' + (error.message || '未知错误'))
  }
}

onMounted(() => {
  getProviderList()
})
</script>

<style scoped lang="scss">
.model-management {
  padding: 20px;

  .box-card {
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;

      .title {
        font-size: 18px;
        font-weight: 600;
      }
    }
  }

  .provider-list {
    min-height: 400px;
  }

  .provider-item {
    border: 1px solid #e4e7ed;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 20px;
    transition: all 0.3s;

    &:hover {
      box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
    }

    .provider-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;

      .provider-info {
        display: flex;
        align-items: center;
        gap: 12px;

        .provider-icon {
          font-size: 24px;
          color: #409eff;
        }

        .provider-name {
          font-size: 16px;
          font-weight: 600;
        }
      }

      .provider-actions {
        display: flex;
        gap: 8px;
      }
    }

    .provider-models {
      background-color: #f5f7fa;
      border-radius: 8px;
      padding: 20px;

      .models-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 16px;

        .models-title {
          font-weight: 600;
          color: #303133;
          font-size: 14px;
        }

        .models-actions {
          display: flex;
          gap: 8px;
        }
      }

      .models-select-wrapper {
        .models-select {
          width: 100%;
        }

        .selected-count {
          margin-top: 12px;
          font-size: 12px;
          color: #909399;
        }
      }
    }
  }
}

// 优化下拉框样式
:deep(.el-select) {
  .el-select__tags {
    flex-wrap: wrap;
    max-height: 120px;
    overflow-y: auto;
  }

  .el-tag {
    margin: 2px 4px 2px 0;
  }
}

:deep(.el-select-dropdown) {
  .el-select-dropdown__item {
    padding: 8px 16px;
    
    &.is-selected {
      font-weight: 600;
      color: #409eff;
      background-color: #ecf5ff;
    }
  }
}
</style>
