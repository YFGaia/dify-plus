<template>
  <div>
    <div class="gva-search-box">
      <el-form
        ref="searchForm"
        :inline="true"
        :model="searchInfo"
      >
        <el-form-item label="用户名">
          <el-input
            v-model="searchInfo.username"
            placeholder="用户名"
          />
        </el-form-item>
        <el-form-item label="昵称">
          <el-input
            v-model="searchInfo.nickname"
            placeholder="昵称"
          />
        </el-form-item>
        <el-form-item label="手机号">
          <el-input
            v-model="searchInfo.phone"
            placeholder="手机号"
          />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input
            v-model="searchInfo.email"
            placeholder="邮箱"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            icon="search"
            @click="onSubmit"
          >
            查询
          </el-button>
          <el-button
            icon="refresh"
            @click="onReset"
          >
            重置
          </el-button>
        </el-form-item>
        <el-form-item style="position: absolute;right: 0;">
          <el-button
            type="warning"
            icon="refresh"
            @click="toSyncUser"
          >
            同步用户
          </el-button>
          <el-button
            type="primary"
            icon="plus"
            @click="addUser"
          >
            新增用户
          </el-button>
        </el-form-item>
      </el-form>
    </div>
    <div class="gva-table-box">
      <el-table
        :data="tableData"
        row-key="ID"
      >
        <el-table-column
          align="left"
          label="头像"
          min-width="75"
        >
          <template #default="scope">
            <CustomPic
              style="margin-top:8px"
              :pic-src="scope.row.headerImg"
            />
          </template>
        </el-table-column>
        <el-table-column
          align="left"
          label="ID"
          min-width="50"
          prop="ID"
        />
        <el-table-column
          align="left"
          label="用户名"
          min-width="150"
          prop="userName"
        />
        <el-table-column
          align="left"
          label="昵称"
          min-width="150"
          prop="nickName"
        />
        <el-table-column
          align="left"
          label="手机号"
          min-width="180"
          prop="phone"
        />
        <el-table-column
          align="left"
          label="邮箱"
          min-width="180"
          prop="email"
        />
        <el-table-column
          align="left"
          label="用户角色"
          min-width="200"
        >
          <template #default="scope">
            <el-cascader
              v-model="scope.row.authorityIds"
              :options="authOptions"
              :show-all-levels="false"
              collapse-tags
              :props="{ multiple:true,checkStrictly: true,label:'authorityName',value:'authorityId',disabled:'disabled',emitPath:false}"
              :clearable="false"
              @visible-change="(flag)=>{changeAuthority(scope.row,flag,0)}"
              @remove-tag="(removeAuth)=>{changeAuthority(scope.row,false,removeAuth)}"
            />
          </template>
        </el-table-column>
        <el-table-column
          align="left"
          label="启用"
          min-width="150"
        >
          <template #default="scope">
            <el-switch
              v-model="scope.row.enable"
              inline-prompt
              :active-value="1"
              :inactive-value="2"
              @change="()=>{switchEnable(scope.row)}"
            />
          </template>
        </el-table-column>

        <el-table-column
          label="操作"
          min-width="250"
          fixed="right"
        >
          <template #default="scope">
            <el-button
              type="primary"
              link
              icon="delete"
              @click="deleteUserFunc(scope.row)"
            >删除</el-button>
            <el-button
              type="primary"
              link
              icon="edit"
              @click="openEdit(scope.row)"
            >编辑</el-button>
            <el-button
              type="primary"
              link
              icon="magic-stick"
              @click="resetPasswordFunc(scope.row)"
            >重置密码</el-button>
          </template>
        </el-table-column>

      </el-table>
      <div class="gva-pagination">
        <el-pagination
          :current-page="page"
          :page-size="pageSize"
          :page-sizes="[10, 30, 50, 100]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="handleCurrentChange"
          @size-change="handleSizeChange"
        />
      </div>
    </div>
    <el-drawer
      v-model="addUserDialog"
      size="60%"
      :show-close="false"
      :close-on-press-escape="false"
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="flex justify-between items-center">
          <span class="text-lg">用户</span>
          <div>
            <el-button @click="closeAddUserDialog">取 消</el-button>
            <el-button
              type="primary"
              @click="enterAddUserDialog"
            >确 定</el-button>
          </div>
        </div>
      </template>

      <el-form
        ref="userForm"
        :rules="rules"
        :model="userInfo"
        label-width="80px"
      >
        <el-form-item
          label="邮箱"
          prop="email"
        >
          <el-input v-model="userInfo.email" />
        </el-form-item>
        <el-form-item
          v-if="dialogFlag === 'add'"
          label="用户名"
          prop="userName"
        >
          <el-input v-model="userInfo.userName" />
        </el-form-item>
        <el-form-item
          label="昵称"
          prop="nickName"
        >
          <el-input v-model="userInfo.nickName" />
        </el-form-item>
        <el-form-item
          v-if="dialogFlag === 'add'"
          label="密码"
          prop="password"
        >
          <el-input
            v-model="userInfo.password"
            style="max-width: 600px"
            placeholder="Please input"
          >
            <template #append>
              <el-button-group class="ml-4">
                <el-button
                  @click="passwordClose"
                  type="primary"
                  :icon="Close"
                />
                <el-button
                  @click="passwordRefresh"
                  type="primary"
                  :icon="Refresh"
                />
                <el-button
                  @click="passwordCopy"
                  type="primary"
                  :icon="CopyDocument"
                />
              </el-button-group>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item
          label="手机号"
          prop="phone"
        >
          <el-input v-model="userInfo.phone" />
        </el-form-item>
        <el-form-item
          label="用户角色"
          prop="authorityId"
        >
          <el-cascader
            v-model="userInfo.authorityIds"
            style="width:100%"
            :options="authOptions"
            :show-all-levels="false"
            :props="{ multiple:true,checkStrictly: true,label:'authorityName',value:'authorityId',disabled:'disabled',emitPath:false}"
            :clearable="false"
          />
        </el-form-item>
        <el-form-item
          label="启用"
          prop="disabled"
        >
          <el-switch
            v-model="userInfo.enable"
            inline-prompt
            :active-value="1"
            :inactive-value="2"
          />
        </el-form-item>
        <el-form-item
          label="头像"
          label-width="80px"
        >
          <SelectImage
            v-model="userInfo.headerImg"
          />
        </el-form-item>
      </el-form>
    </el-drawer>
  </div>
</template>

<script setup>

import {
  syncUser, // Extend: sync user
  getUserList,
  setUserAuthorities,
  register,
  deleteUser
} from '@/api/user'

import {
  Close,
  Refresh,
  CopyDocument,
} from '@element-plus/icons-vue'

import { getAuthorityList } from '@/api/authority'
import CustomPic from '@/components/customPic/index.vue'
import { setUserInfo, resetPassword } from '@/api/user.js'

import { nextTick, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import SelectImage from '@/components/selectImage/selectImage.vue'
import randomPasswd from "@/utils/passwd";
import { useClipboard } from '@vueuse/core'

defineOptions({
  name: 'UserList',
})

const searchInfo = ref({
  username: '',
  nickname: '',
  phone: '',
  email: ''
})

const onSubmit = () => {
  page.value = 1
  getTableData()
}

const onReset = () => {
  searchInfo.value = {
    username: '',
    nickname: '',
    phone: '',
    email: ''
  }
  getTableData()
}
// 初始化相关
const setAuthorityOptions = (AuthorityData, optionsData) => {
  AuthorityData &&
        AuthorityData.forEach(item => {
          if (item.children && item.children.length) {
            const option = {
              authorityId: item.authorityId,
              authorityName: item.authorityName,
              children: []
            }
            setAuthorityOptions(item.children, option.children)
            optionsData.push(option)
          } else {
            const option = {
              authorityId: item.authorityId,
              authorityName: item.authorityName
            }
            optionsData.push(option)
          }
        })
}

const page = ref(1)
const total = ref(0)
const pageSize = ref(10)
const tableData = ref([])
const { copy, isSupported } = useClipboard()
// 分页
const handleSizeChange = (val) => {
  pageSize.value = val
  getTableData()
}

const handleCurrentChange = (val) => {
  page.value = val
  getTableData()
}
// 查询
const getTableData = async() => {
  const table = await getUserList({ page: page.value, pageSize: pageSize.value, ...searchInfo.value })
  if (table.code === 0) {
    tableData.value = table.data.list
    total.value = table.data.total
    page.value = table.data.page
    pageSize.value = table.data.pageSize
  }
}

watch(() => tableData.value, () => {
  setAuthorityIds()
})

const initPage = async() => {
  getTableData()
  const res = await getAuthorityList()
  setOptions(res.data)
}

initPage()

const resetPasswordFunc = (row) => {
  ElMessageBox.prompt(`是否修改 ${row.nickName} 密码`, '请注意', {
    confirmButtonText: '修改',
    cancelButtonText: '取消',
    inputPattern: /^(?=.*[a-zA-Z])(?=.*\d).{8,}$/,
    inputErrorMessage: '设置8~26个字的密码，至少包含一个英文一个数字',
  }).then(async ({value}) => {
    const res = await resetPassword({
      Password: value.trim(),
      ID: row.ID,
    })
    if (res.code === 0) {
      ElMessage({
        type: 'success',
        message: res.msg,
      })
    } else {
      ElMessage({
        type: 'error',
        message: res.msg,
      })
    }
  })
}
const setAuthorityIds = () => {
  tableData.value && tableData.value.forEach((user) => {
    user.authorityIds = user.authorities && user.authorities.map(i => {
      return i.authorityId
    })
  })
}

const authOptions = ref([])
const setOptions = (authData) => {
  authOptions.value = []
  setAuthorityOptions(authData, authOptions.value)
}

const deleteUserFunc = async(row) => {
  ElMessageBox.confirm('确定要删除吗?', '提示', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning'
  }).then(async() => {
    const res = await deleteUser({ id: row.ID })
    if (res.code === 0) {
      ElMessage.success('删除成功')
      await getTableData()
    }
  })
}

// 弹窗相关
const userInfo = ref({
  userName: '',
  password: '',
  nickName: '',
  headerImg: '',
  authorityId: '',
  authorityIds: [],
  enable: 1,
})

const rules = ref({
  userName: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 4, message: '最低4位字符', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入用户密码', trigger: 'blur' },
    { min: 6, message: '最低6位字符', trigger: 'blur' }
  ],
  nickName: [
    { required: true, message: '请输入用户昵称', trigger: 'blur' }
  ],
  phone: [
    { pattern: /^1([38][0-9]|4[014-9]|[59][0-35-9]|6[2567]|7[0-8])\d{8}$/, message: '请输入合法手机号', trigger: 'blur' },
  ],
  email: [
    { required: true, message: '请输入用户邮箱', trigger: 'blur' },
    { pattern: /^[\w\.!#$%&'*+\-/=?^_`{|}~]+@([\w-]+\.)+[\w-]{2,}$/g, message: '请输入正确的邮箱', trigger: 'blur' },
  ],
  authorityId: [
    { required: true, message: '请选择用户角色', trigger: 'blur' }
  ]
})
const userForm = ref(null)
// 清除密码
const passwordClose = () => {
  userInfo.value.password = "";
}
// 随机生成密码
const passwordRefresh = () => {
  const passwd = randomPasswd(16)
  userInfo.value.password = passwd;
  if (!isSupported) {
    ElMessage({
      type: 'error',
      message: "请手动复制密码",
    })
    return;
  }
  copy(passwd)
  ElMessage({
    type: 'success',
    message: "密码已复制",
  })

}
// 复制密码
const passwordCopy = () => {
  const passwd = userInfo.value.password.trim()
  if (passwd.length === 0)
    ElMessage({
      type: 'error',
      message: "没有设置密码",
    })
  if (isSupported) {
    copy(passwd)
    ElMessage({
      type: 'success',
      message: "复制成功",
    })
  }
  else
    ElMessage({
      type: 'error',
      message: "复制失败",
    })
}
const enterAddUserDialog = async() => {
  userInfo.value.authorityId = userInfo.value.authorityIds[0]
  userForm.value.validate(async valid => {
    if (valid) {
      const req = {
        ...userInfo.value
      }
      if (dialogFlag.value === 'add') {
        const res = await register(req)
        if (res.code === 0) {
          ElMessage({ type: 'success', message: '创建成功' })
          await getTableData()
          closeAddUserDialog()
        }
      }
      if (dialogFlag.value === 'edit') {
        const res = await setUserInfo(req)
        if (res.code === 0) {
          ElMessage({ type: 'success', message: '编辑成功' })
          await getTableData()
          closeAddUserDialog()
        }
      }
    }
  })
}

const addUserDialog = ref(false)
const closeAddUserDialog = () => {
  userForm.value.resetFields()
  userInfo.value.headerImg = ''
  userInfo.value.authorityIds = []
  addUserDialog.value = false
}

const dialogFlag = ref('add')

const addUser = () => {
  dialogFlag.value = 'add'
  addUserDialog.value = true
}

const toSyncUser = async () => {
  const res = await syncUser()
  if (res.code === 0) {
    ElMessage.success('同步成功')
    await getTableData()
  }
}

const tempAuth = {}
const changeAuthority = async(row, flag, removeAuth) => {
  if (flag) {
    if (!removeAuth) {
      tempAuth[row.ID] = [...row.authorityIds]
    }
    return
  }
  await nextTick()
  const res = await setUserAuthorities({
    ID: row.ID,
    authorityIds: row.authorityIds
  })
  if (res.code === 0) {
    ElMessage({ type: 'success', message: '角色设置成功' })
  } else {
    if (!removeAuth) {
      row.authorityIds = [...tempAuth[row.ID]]
      delete tempAuth[row.ID]
    } else {
      row.authorityIds = [removeAuth, ...row.authorityIds]
    }
  }
}

const openEdit = (row) => {
  dialogFlag.value = 'edit'
  userInfo.value = JSON.parse(JSON.stringify(row))
  addUserDialog.value = true
}

const switchEnable = async(row) => {
  userInfo.value = JSON.parse(JSON.stringify(row))
  await nextTick()
  const req = {
    ...userInfo.value
  }
  const res = await setUserInfo(req)
  if (res.code === 0) {
    ElMessage({ type: 'success', message: `${req.enable === 2 ? '禁用' : '启用'}成功` })
    await getTableData()
    userInfo.value.headerImg = ''
    userInfo.value.authorityIds = []
  }
}

</script>

<style lang="scss">
  .header-img-box {
    @apply w-52 h-52 border border-solid border-gray-300 rounded-xl flex justify-center items-center cursor-pointer;
 }
</style>
