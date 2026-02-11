<template>
  <div
    id="userLayout"
    class="w-full h-full relative"
  >
    <div
      class="rounded-lg flex items-center justify-evenly w-full h-full md:w-screen md:h-screen md:bg-[#194bfb] bg-white"
    >
      <div class="md:w-3/5 w-10/12 h-full flex items-center justify-evenly">
        <div class="oblique h-[130%] w-3/5 bg-white dark:bg-slate-900 transform -rotate-12 absolute -ml-52" />
        <!-- 分割斜块 -->
        <div class="z-[999] pt-12 pb-10 md:w-96 w-full  rounded-lg flex flex-col justify-between box-border">
          <div>
            <div class="flex items-center justify-center">

              <img
                class="w-24"
                :src="$GIN_VUE_ADMIN.appLogo"
                alt
              >
            </div>
            <div class="mb-9">
              <p class="text-center text-4xl font-bold">{{ $GIN_VUE_ADMIN.appName }}</p>
              <p class="text-center text-sm font-normal text-gray-500 mt-2.5">A management platform for Dify-Plus</p>
              <p v-if="redirectUri" class="text-center text-xs text-blue-600 mt-2">登录后将跳回第三方应用</p>
            </div>
            <el-form
              ref="loginForm"
              :model="loginFormData"
              :rules="rules"
              :validate-on-rule-change="false"
              @keyup.enter="submitForm"
            >
              <!--  新增是否已经初始化判断 Begin -->
              <template
                v-if="showInit"
              >
                <el-form-item
                  prop="username"
                  class="mb-6"
                >
                  <el-input
                    v-model="loginFormData.username"
                    size="large"
                    placeholder="请输入dify的第一个帐号,即为管理员帐号"
                    suffix-icon="user"
                  />
                </el-form-item>
                <el-form-item
                  prop="password"
                  class="mb-6"
                >
                  <el-input
                    v-model="loginFormData.password"
                    show-password
                    size="large"
                    type="password"
                    placeholder="请输入密码"
                  />
                </el-form-item>
                <el-form-item
                  v-if="loginFormData.openCaptcha"
                  prop="captcha"
                  class="mb-6"
                >
                  <div class="flex w-full justify-between">
                    <el-input
                      v-model="loginFormData.captcha"
                      placeholder="请输入验证码"
                      size="large"
                      class="flex-1 mr-5"
                    />
                    <div class="w-1/3 h-11 bg-[#c3d4f2] rounded">
                      <img
                        v-if="picPath"
                        class="w-full h-full"
                        :src="picPath"
                        alt="请输入验证码"
                        @click="loginVerify()"
                      >
                    </div>
                  </div>
                </el-form-item>
                <el-form-item class="mb-6">
                  <el-button
                    class="shadow shadow-active h-11 w-full"
                    type="primary"
                    size="large"
                    @click="submitForm"
                  >账号密码登录</el-button>
                </el-form-item>
                <!-- 钉钉 / OAuth2 登录：仅在有 redirect_uri（第三方回调）时显示 -->
                <el-form-item
                  v-if="loginOptions.dingtalk.enabled && redirectUri"
                  class="mb-6"
                >
                  <el-button
                    class="shadow h-11 w-full"
                    size="large"
                    @click="dingtalkLoginJump"
                  >
                    钉钉登录
                  </el-button>
                </el-form-item>
                <el-form-item
                  v-if="loginOptions.oauth2.enabled && redirectUri"
                  class="mb-6"
                >
                  <el-button
                    class="shadow shadow-blue-600 h-11 w-full"
                    size="large"
                    @click="oauth2LoginJump"
                  >
                    OAuth2 登录
                  </el-button>
                </el-form-item>
              </template>
              <!--  新增是否已经初始化判断 End -->
              <el-form-item
                v-else
                class="mb-6"
              >
                <el-button
                  class="shadow shadow-active h-11 w-full"
                  type="primary"
                  size="large"
                  @click="checkInit"
                >前往初始化</el-button>

              </el-form-item>
            </el-form>
          </div>
        </div>
      </div>
<!--      <div class="hidden md:block w-1/2 h-full float-right bg-[#194bfb]"><img-->
<!--        class="h-full"-->
<!--        src="@/assets/login_right_banner.jpg"-->
<!--        alt="banner"-->
<!--      ></div>-->
    </div>

    <BottomInfo class="left-0 right-0 absolute bottom-3 mx-auto  w-full z-20">
      <div class="links items-center justify-center gap-2 hidden md:flex">
        <a
          href="https://www.gin-vue-admin.com/"
          target="_blank"
        >
          <img
            src="@/assets/docs.png"
            class="w-8 h-8"
            alt="文档"
          >
        </a>
        <a
          href="https://support.qq.com/product/371961"
          target="_blank"
        >
          <img
            src="@/assets/kefu.png"
            class="w-8 h-8"
            alt="客服"
          >
        </a>
        <a
          href="https://github.com/flipped-aurora/gin-vue-admin"
          target="_blank"
        >
          <img
            src="@/assets/github.png"
            class="w-8 h-8"
            alt="github"
          >
        </a>
        <a
          href="https://space.bilibili.com/322210472"
          target="_blank"
        >
          <img
            src="@/assets/video.png"
            class="w-8 h-8"
            alt="视频站"
          >
        </a>
      </div>
    </BottomInfo>
  </div>
</template>

<script setup>
import { captcha } from '@/api/user'
import { checkDB } from '@/api/initdb'
import { getGaiaLoginOptions } from '@/api/user_extend'
import BottomInfo from '@/components/bottomInfo/bottomInfo.vue'
import { reactive, ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter, useRoute } from 'vue-router'
import { useUserStore } from '@/pinia/modules/user'

defineOptions({
  name: "Login",
})

const router = useRouter()
const route = useRoute()

// 第三方回调参数（用于登录成功后跳回第三方并带 token）
const redirectUri = ref(route.query.redirect_uri || '')
const thirdPartyState = ref(route.query.state || '')

// Gaia 登录方式（钉钉/OAuth2）
const loginOptions = reactive({
  dingtalk: { enabled: false, auth_url: '' },
  oauth2: { enabled: false, auth_url: '' }
})
const showInit = ref(false)
// 验证函数
const checkUsername = (rule, value, callback) => {
  if (value.length < 5) {
    return callback(new Error('请输入正确的用户名'))
  } else {
    callback()
  }
}
const checkPassword = (rule, value, callback) => {
  if (value.length < 6) {
    return callback(new Error('请输入正确的密码'))
  } else {
    callback()
  }
}

// 获取验证码
const loginVerify = async() => {
  const ele = await captcha()
  rules.captcha.push({
    max: ele.data.captchaLength,
    min: ele.data.captchaLength,
    message: `请输入${ele.data.captchaLength}位验证码`,
    trigger: 'blur',
  })
  picPath.value = ele.data.picPath
  loginFormData.captchaId = ele.data.captchaId
  loginFormData.openCaptcha = ele.data.openCaptcha
}
loginVerify()

// 登录相关操作
const loginForm = ref(null)
const picPath = ref('')
const loginFormData = reactive({
  username: '',
  password: '',
  captcha: '',
  captchaId: '',
  openCaptcha: false,
})
const rules = reactive({
  username: [{ validator: checkUsername, trigger: 'blur' }],
  password: [{ validator: checkPassword, trigger: 'blur' }],
  captcha: [
    {
      message: '验证码格式不正确',
      trigger: 'blur',
    },
  ],
})

const userStore = useUserStore()
const login = async() => {
  return await userStore.LoginIn(loginFormData, {
    redirect_uri: redirectUri.value || undefined,
    state: thirdPartyState.value || undefined,
  })
}
const submitForm = () => {
  loginForm.value.validate(async(v) => {
    if (!v) {
      // 未通过前端静态验证
      ElMessage({
        type: 'error',
        message: '请正确填写登录信息',
        showClose: true,
      })
      await loginVerify()
      return false
    }

    // 通过验证，请求登陆
    const flag = await login()

    // 登陆失败，刷新验证码
    if (!flag) {
      await loginVerify()
      return false
    }

    // 登陆成功
    return true
  })
}

// 跳转初始化
const checkInit = async() => {
  const res = await checkDB()
  if (res.code === 0) {
    if (res.data?.needInit) {
      userStore.NeedInit()
      await router.push({name: 'Init'})
    } else {
      ElMessage({
        type: 'info',
        message: '已配置数据库信息，无法初始化',
      })
    }
  }
}

// 新增是否已经初始化判断
const showInitExtend = async() => {
  const res = await checkDB()
  if (res.code === 0) {
    showInit.value = !res.data?.needInit
  }

}
showInitExtend()

// 已登录且带 redirect_uri 时直接回调第三方
const tryRedirectWithToken = async () => {
  if (!redirectUri.value || !userStore.token) return false
  const res = await userStore.GetUserInfo()
  if (res?.code === 0) {
    const sep = redirectUri.value.includes('?') ? '&' : '?'
    const url = redirectUri.value + sep + 'token=' + encodeURIComponent(userStore.token) + (thirdPartyState.value ? '&state=' + encodeURIComponent(thirdPartyState.value) : '')
    window.location.href = url
    return true
  }
  return false
}

// 拉取登录方式并检测已登录回调
const loadLoginOptionsAndMaybeRedirect = async () => {
  const didRedirect = await tryRedirectWithToken()
  if (didRedirect) return
  try {
    const res = await getGaiaLoginOptions({ origin: window.location.origin })
    if (res?.code === 0 && res.data) {
      if (res.data.dingtalk) {
        loginOptions.dingtalk.enabled = res.data.dingtalk.enabled
        loginOptions.dingtalk.auth_url = res.data.dingtalk.auth_url || ''
      }
      if (res.data.oauth2) {
        loginOptions.oauth2.enabled = res.data.oauth2.enabled
        loginOptions.oauth2.auth_url = res.data.oauth2.auth_url || ''
      }
    }
  } catch (_) {}
}

// 钉钉登录：保存回调参数并跳转钉钉授权
const dingtalkLoginJump = () => {
  sessionStorage.setItem('gaia_login_redirect_uri', redirectUri.value)
  sessionStorage.setItem('gaia_login_state', thirdPartyState.value)
  if (loginOptions.dingtalk.auth_url) window.location.href = loginOptions.dingtalk.auth_url
}

// OAuth2 登录：保存回调参数并跳转 OAuth2 授权
const oauth2LoginJump = () => {
  sessionStorage.setItem('gaia_login_redirect_uri', redirectUri.value)
  sessionStorage.setItem('gaia_login_state', thirdPartyState.value)
  if (loginOptions.oauth2.auth_url) window.location.href = loginOptions.oauth2.auth_url
}

onMounted(() => {
  loadLoginOptionsAndMaybeRedirect()
})
</script>
