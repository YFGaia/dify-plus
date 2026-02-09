<template>
  <div class="app-version">
    <div class="page-header flex flex-wrap items-center justify-between gap-4">
      <div class="flex">
        <p class="text-gray-500 mt-2">
          版本列表按创建时间倒序；客户端 GET /latest 取最新一条。支持拖拽上传，系统将根据文件名自动识别平台与架构。<br />
          本功能主要为第三方软件进行版本管理
        </p>
      </div>
      <div class="flex gap-2">
        <el-button type="primary" icon="Plus" @click="openTokenDialog">Token 配置</el-button>
        <el-button type="primary" icon="Plus" @click="openAddDialog">新增版本</el-button>
      </div>
    </div>

    <!-- 版本列表 -->
    <div class="card mt-6">
      <el-table v-loading="loading" :data="releases" stripe>
        <el-table-column prop="created_at" label="上传时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column prop="version" label="版本号" width="120" />
        <el-table-column prop="release_notes" label="更新说明" min-width="200">
          <template #default="{ row }">
            <span class="line-clamp-2">{{ row.release_notes || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="安装包" width="220">
          <template #default="{ row }">
            <div class="flex flex-wrap gap-1">
              <el-tag v-for="d in row.downloads" :key="d.id" size="small" type="info">
                {{ platformArchLabel(d.platform, d.arch) }}
              </el-tag>
              <span v-if="!row.downloads?.length" class="text-gray-400">暂无</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="openEditDialog(row)">编辑</el-button>
            <el-button type="primary" link size="small" @click="openUploadDialog(row)">上传安装包</el-button>
            <el-button type="danger" link size="small" @click="removeDownloadConfirm(row)">删除包</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- Token 配置弹窗 -->
    <el-dialog v-model="tokenDialogVisible" title="链接 Token 配置" width="480" @closed="tokenForm.link_token = ''; tokenVerified = false">
      <div class="flex gap-2 items-center">
        <el-input
          v-model="tokenForm.link_token"
          :type="tokenVerified ? 'text' : 'password'"
          :placeholder="tokenVerified ? '留空表示不校验' : '已配置时需验证密码后查看'"
          show-password
          clearable
          class="flex-1"
        />
        <el-button type="primary" icon="Refresh" @click="randomGenerateToken">随机生成</el-button>
        <el-button v-if="hasToken && !tokenVerified" type="default" @click="showPasswordVerify">查看</el-button>
      </div>
      <p class="text-gray-500 text-sm mt-2">配置后客户端 GET /latest 需传 token=xxx；清空并保存则取消校验。</p>
      <template #footer>
        <el-button @click="tokenDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveToken">保存</el-button>
      </template>
    </el-dialog>

    <!-- 查看 Token 密码验证 -->
    <el-dialog v-model="passwordVerifyVisible" title="验证身份" width="360" append-to-body>
      <el-input
        v-model="passwordVerifyValue"
        type="password"
        placeholder="请输入登录密码"
        show-password
        @keyup.enter="confirmPasswordVerify"
      />
      <template #footer>
        <el-button @click="passwordVerifyVisible = false">取消</el-button>
        <el-button type="primary" :loading="passwordVerifyLoading" @click="confirmPasswordVerify">确定</el-button>
      </template>
    </el-dialog>

    <!-- 新增版本对话框：版本号、说明、拖拽上传 -->
    <el-dialog
      v-model="addDialogVisible"
      title="新增版本"
      width="560"
      :close-on-click-modal="false"
      @closed="resetAddForm"
    >
      <el-form ref="addFormRef" :model="addForm" :rules="addRules" label-width="100px">
        <el-form-item label="版本号" prop="version">
          <el-input v-model="addForm.version" placeholder="例如 0.0.5" />
        </el-form-item>
        <el-form-item label="更新说明" prop="release_notes">
          <el-input v-model="addForm.release_notes" type="textarea" :rows="3" placeholder="选填" />
        </el-form-item>
        <el-form-item label="安装包">
          <div
            class="upload-drop border-2 border-dashed rounded-lg p-8 text-center transition-colors"
            :class="{ 'border-primary bg-blue-50': dragOver, 'border-gray-300': !dragOver }"
            @drop.prevent="onDrop"
            @dragover.prevent="dragOver = true"
            @dragleave="dragOver = false"
            @click="triggerFileSelect"
          >
            <input
              ref="addFileInput"
              type="file"
              multiple
              accept=".dmg,.exe,.deb,.AppImage,.appimage"
              class="hidden"
              @change="onFileSelect"
            >
            <p class="text-gray-600">
              <el-icon class="text-4xl mb-2"><UploadFilled /></el-icon><br>
              拖拽文件到此处，或点击选择<br>
              <span class="text-sm text-gray-400">.dmg / .exe / .deb / .AppImage，将自动识别平台与架构</span>
            </p>
            <ul v-if="addForm.files.length" class="mt-3 text-left text-sm space-y-1">
              <li v-for="(f, i) in addForm.files" :key="i" class="flex items-center justify-between">
                <span>{{ f.name }}</span>
                <el-button type="danger" link size="small" @click.stop="removeAddFile(i)">移除</el-button>
              </li>
            </ul>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="addSubmitting" @click="submitAddVersion">
          创建并上传
        </el-button>
      </template>
    </el-dialog>

    <!-- 编辑版本对话框 -->
    <el-dialog v-model="editDialogVisible" title="编辑版本" width="520" @closed="editingRelease = null">
      <el-form v-if="editingRelease" :model="editForm" label-width="100px">
        <el-form-item label="版本号">
          <el-input v-model="editForm.version" />
        </el-form-item>
        <el-form-item label="更新说明">
          <el-input v-model="editForm.release_notes" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSubmitting" @click="submitEditVersion">保存</el-button>
      </template>
    </el-dialog>

    <!-- 为已有版本上传安装包 -->
    <el-dialog v-model="uploadDialogVisible" title="上传安装包" width="500" @closed="uploadForm.files = []; uploadForm.releaseId = null">
      <p v-if="uploadForm.releaseId" class="mb-3 text-gray-600">版本 ID: {{ uploadForm.releaseId }}，拖拽或选择文件，将自动识别平台与架构。</p>
      <div
        class="upload-drop border-2 border-dashed rounded-lg p-6 text-center"
        :class="{ 'border-primary bg-blue-50': uploadDragOver, 'border-gray-300': !uploadDragOver }"
        @drop.prevent="onUploadDrop"
        @dragover.prevent="uploadDragOver = true"
        @dragleave="uploadDragOver = false"
        @click="uploadFileInput?.click()"
      >
        <input ref="uploadFileInput" type="file" multiple accept=".dmg,.exe,.deb,.AppImage,.appimage" class="hidden" @change="onUploadFileSelect">
        <p class="text-gray-600">拖拽或点击选择 .dmg / .exe / .deb / .AppImage</p>
        <ul v-if="uploadForm.files.length" class="mt-2 text-sm text-left space-y-1">
          <li v-for="(f, i) in uploadForm.files" :key="i" class="flex justify-between">
            <span>{{ f.name }}</span>
            <el-button type="danger" link size="small" @click.stop="uploadForm.files.splice(i, 1)">移除</el-button>
          </li>
        </ul>
      </div>
      <template #footer>
        <el-button @click="uploadDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploadSubmitting" @click="submitUpload">上传</el-button>
      </template>
    </el-dialog>

    <!-- 删除包：选择要删的 platform/arch -->
    <el-dialog v-model="deleteDownloadVisible" title="删除安装包" width="400">
      <p class="mb-3">选择要删除的安装包：</p>
      <el-radio-group v-model="deleteDownloadTarget">
        <el-radio
          v-for="d in deleteDownloadCandidates"
          :key="d.platform + d.arch"
          :label="d.platform + '/' + d.arch"
        >
          {{ platformArchLabel(d.platform, d.arch) }} - {{ d.file_name }}
        </el-radio>
      </el-radio-group>
      <template #footer>
        <el-button @click="deleteDownloadVisible = false">取消</el-button>
        <el-button type="danger" :loading="deleteSubmitting" @click="confirmDeleteDownload">删除</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import {
  getAppVersionToken,
  setAppVersionToken,
  revealAppVersionToken,
  getAppVersionReleases,
  createAppVersionRelease,
  getAppVersionRelease,
  updateAppVersionRelease,
  uploadAppVersionPackage,
  deleteAppVersionDownload
} from '@/api/gaia/appVersion'

defineOptions({ name: 'AppVersion' })

const loading = ref(false)
const releases = ref([])
const tokenDialogVisible = ref(false)
const tokenForm = reactive({ link_token: '' })
const hasToken = ref(false)       // 是否已配置过 Token（脱敏 ********）
const tokenVerified = ref(false)  // 是否已通过密码验证看到明文
const passwordVerifyVisible = ref(false)
const passwordVerifyValue = ref('')
const passwordVerifyLoading = ref(false)

const addDialogVisible = ref(false)
const addFormRef = ref(null)
const addForm = reactive({ version: '', release_notes: '', files: [] })
const addRules = { version: [{ required: true, message: '请输入版本号', trigger: 'blur' }] }
const addFileInput = ref(null)
const dragOver = ref(false)
const addSubmitting = ref(false)

const editDialogVisible = ref(false)
const editingRelease = ref(null)
const editForm = reactive({ version: '', release_notes: '' })
const editSubmitting = ref(false)

const uploadDialogVisible = ref(false)
const uploadFileInput = ref(null)
const uploadDragOver = ref(false)
const uploadForm = reactive({ releaseId: null, files: [] })
const uploadSubmitting = ref(false)

const deleteDownloadVisible = ref(false)
const deleteDownloadRelease = ref(null)
const deleteDownloadCandidates = ref([])
const deleteDownloadTarget = ref('')
const deleteSubmitting = ref(false)

function formatTime(t) {
  if (!t) return '-'
  try {
    const d = new Date(t)
    return d.toLocaleString('zh-CN')
  } catch {
    return t
  }
}

function platformArchLabel(platform, arch) {
  const p = { darwin: 'macOS', win32: 'Windows', linux: 'Linux' }[platform] || platform
  return `${p} ${arch}`
}

async function loadReleases() {
  loading.value = true
  try {
    const res = await getAppVersionReleases()
    releases.value = res.data || []
  } catch (e) {
    ElMessage.error(e?.response?.data?.msg || '加载版本列表失败')
  } finally {
    loading.value = false
  }
}

function openTokenDialog() {
  tokenDialogVisible.value = true
  tokenVerified.value = false
  getAppVersionToken().then(res => {
    const t = res.data?.link_token ?? ''
    hasToken.value = t === '********'
    tokenForm.link_token = hasToken.value ? '' : t
  }).catch(() => {})
}

function randomGenerateToken() {
  const bytes = new Uint8Array(24)
  crypto.getRandomValues(bytes)
  const token = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
  tokenForm.link_token = token
  try {
    navigator.clipboard.writeText(token)
    ElMessage.success('已生成并复制到剪贴板')
  } catch {
    ElMessage.success('已生成，请手动复制')
  }
}

function showPasswordVerify() {
  passwordVerifyValue.value = ''
  passwordVerifyVisible.value = true
}

async function confirmPasswordVerify() {
  if (!passwordVerifyValue.value.trim()) {
    ElMessage.warning('请输入登录密码')
    return
  }
  passwordVerifyLoading.value = true
  try {
    const res = await revealAppVersionToken({ password: passwordVerifyValue.value })
    const token = res.data?.token ?? ''
    tokenForm.link_token = token
    tokenVerified.value = true
    passwordVerifyVisible.value = false
    ElMessage.success('验证成功')
  } catch (e) {
    ElMessage.error(e?.response?.data?.msg || '密码错误')
  } finally {
    passwordVerifyLoading.value = false
  }
}

async function saveToken() {
  try {
    let payload = tokenForm.link_token
    if (hasToken.value && !tokenVerified.value && payload === '') payload = '********' // 未查看且未改，不更新
    else if (payload === '') payload = ''
    await setAppVersionToken({ link_token: payload })
    ElMessage.success('已保存')
    tokenDialogVisible.value = false
  } catch (e) {
    ElMessage.error(e?.response?.data?.msg || '保存失败')
  }
}

function openAddDialog() {
  addDialogVisible.value = true
}

function resetAddForm() {
  addForm.version = ''
  addForm.release_notes = ''
  addForm.files = []
  addFormRef.value?.resetFields()
}

function triggerFileSelect() {
  addFileInput.value?.click()
}

function onDrop(e) {
  dragOver.value = false
  const list = Array.from(e.dataTransfer?.files || []).filter(f => {
    const n = f.name.toLowerCase()
    return n.endsWith('.dmg') || n.endsWith('.exe') || n.endsWith('.deb') || n.endsWith('.appimage')
  })
  addForm.files.push(...list)
}

function onFileSelect(e) {
  const list = Array.from(e.target?.files || [])
  addForm.files.push(...list)
  if (addFileInput.value) addFileInput.value.value = ''
}

function removeAddFile(i) {
  addForm.files.splice(i, 1)
}

async function submitAddVersion() {
  await addFormRef.value?.validate().catch(() => {})
  if (!addForm.version.trim()) {
    ElMessage.warning('请输入版本号')
    return
  }
  addSubmitting.value = true
  try {
    const createRes = await createAppVersionRelease({
      version: addForm.version.trim(),
      release_notes: addForm.release_notes?.trim() || ''
    })
    const releaseId = createRes.data?.id
    if (!releaseId) throw new Error('创建版本失败')
    for (const file of addForm.files) {
      const fd = new FormData()
      fd.append('file', file)
      await uploadAppVersionPackage(releaseId, fd)
    }
    ElMessage.success('版本已创建并上传完成')
    addDialogVisible.value = false
    await loadReleases()
  } catch (e) {
    ElMessage.error(e?.response?.data?.msg || e?.message || '操作失败')
  } finally {
    addSubmitting.value = false
  }
}

function openUploadDialog(row) {
  uploadForm.releaseId = row.id
  uploadForm.files = []
  uploadDialogVisible.value = true
}

function onUploadDrop(e) {
  uploadDragOver.value = false
  const list = Array.from(e.dataTransfer?.files || []).filter(f => {
    const n = f.name.toLowerCase()
    return n.endsWith('.dmg') || n.endsWith('.exe') || n.endsWith('.deb') || n.endsWith('.appimage')
  })
  uploadForm.files.push(...list)
}

function onUploadFileSelect(e) {
  const list = Array.from(e.target?.files || [])
  uploadForm.files.push(...list)
  if (uploadFileInput.value) uploadFileInput.value.value = ''
}

async function submitUpload() {
  if (!uploadForm.releaseId || !uploadForm.files.length) {
    ElMessage.warning('请选择要上传的文件')
    return
  }
  uploadSubmitting.value = true
  try {
    for (const file of uploadForm.files) {
      const fd = new FormData()
      fd.append('file', file)
      await uploadAppVersionPackage(uploadForm.releaseId, fd)
    }
    ElMessage.success('上传完成')
    uploadDialogVisible.value = false
    await loadReleases()
  } catch (e) {
    ElMessage.error(e?.response?.data?.msg || '上传失败')
  } finally {
    uploadSubmitting.value = false
  }
}

function openEditDialog(row) {
  editingRelease.value = row
  editForm.version = row.version
  editForm.release_notes = row.release_notes || ''
  editDialogVisible.value = true
}

async function submitEditVersion() {
  if (!editingRelease.value) return
  editSubmitting.value = true
  try {
    await updateAppVersionRelease(editingRelease.value.id, {
      version: editForm.version,
      release_notes: editForm.release_notes
    })
    ElMessage.success('已保存')
    editDialogVisible.value = false
    await loadReleases()
  } catch (e) {
    ElMessage.error(e?.response?.data?.msg || '保存失败')
  } finally {
    editSubmitting.value = false
  }
}

function removeDownloadConfirm(row) {
  if (!row.downloads?.length) {
    ElMessage.info('该版本暂无安装包')
    return
  }
  deleteDownloadRelease.value = row
  deleteDownloadCandidates.value = row.downloads
  deleteDownloadTarget.value = row.downloads[0] ? row.downloads[0].platform + '/' + row.downloads[0].arch : ''
  deleteDownloadVisible.value = true
}

async function confirmDeleteDownload() {
  if (!deleteDownloadRelease.value || !deleteDownloadTarget.value) return
  const [platform, arch] = deleteDownloadTarget.value.split('/')
  deleteSubmitting.value = true
  try {
    await deleteAppVersionDownload(deleteDownloadRelease.value.id, { platform, arch })
    ElMessage.success('已删除')
    deleteDownloadVisible.value = false
    await loadReleases()
  } catch (e) {
    ElMessage.error(e?.response?.data?.msg || '删除失败')
  } finally {
    deleteSubmitting.value = false
  }
}

onMounted(() => {
  loadReleases()
})
</script>

<style scoped>
.app-version .card { padding: 1rem 1.5rem; border-radius: 8px; border: 1px solid var(--el-border-color); }
.upload-drop { cursor: pointer; }
.line-clamp-2 { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.border-primary { border-color: var(--el-color-primary); }
</style>
