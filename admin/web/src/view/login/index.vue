<!-- 二开部分：重新设计看起来更现代化的登陆页面 -->
<template>
  <div
    id="userLayout"
    class="w-full h-full relative min-h-screen bg-slate-50 dark:bg-[#0a0a1a] overflow-hidden"
  >
    <!-- 动态背景层 -->
    <div class="absolute inset-0 z-0 overflow-hidden">
      <!-- 流动渐变背景 -->
      <div class="absolute inset-0 bg-gradient-to-br from-[#667eea] via-[#764ba2] to-[#6b46c1] animate-gradient-flow" />
      
      <!-- 抽象几何装饰 -->
      <div class="absolute top-1/4 -right-20 w-96 h-96 bg-white/10 rounded-full blur-[100px]" />
      <div class="absolute bottom-1/3 -left-32 w-80 h-80 bg-purple-300/20 rounded-3xl rotate-45 blur-[80px]" />
      
      <!-- 动态粒子 -->
      <div class="absolute inset-0 opacity-20 particle-network" />
    </div>

    <!-- 主内容容器 -->
    <div class="relative z-10 flex items-center justify-center w-full h-screen p-4">
      <!-- 玻璃拟态面板 -->
      <div class="w-full max-w-md rounded-2xl backdrop-blur-xl bg-white/90 dark:bg-[rgba(15,15,35,0.9)] shadow-2xl border border-white/20">
        <div class="z-[999] pt-12 pb-10 px-8 rounded-xl flex flex-col justify-between box-border">
          <div>
            <!-- Logo区域 -->
            <div class="flex items-center justify-center mb-8">
              <img
                class="w-28 transition-transform hover:scale-105"
                :src="$GIN_VUE_ADMIN.appLogo"
                alt="App Logo"
              >
            </div>
            
            <!-- 标题区域 -->
            <div class="mb-10 text-center">
              <h1 class="text-4xl font-extrabold bg-gradient-to-r from-blue-600 to-purple-600 bg-clip-text text-transparent">
                {{ $GIN_VUE_ADMIN.appName }}
              </h1>
              <p class="mt-3 text-sm font-medium text-slate-500 dark:text-slate-400">
                A management platform for Dify-Plus
              </p>
            </div>

            <!-- 表单区域 -->
            <el-form
              ref="loginForm"
              :model="loginFormData"
              :rules="rules"
              :validate-on-rule-change="false"
              @keyup.enter="submitForm"
            >
              <!-- 初始化判断 -->
              <template v-if="showInit">
                <!-- 用户名输入 -->
                <el-form-item prop="username" class="mb-6">
                  <el-input
                    v-model="loginFormData.username"
                    size="large"
                    placeholder="请输入管理员账号"
                    suffix-icon="user"
                    class="[&>.el-input__wrapper]:shadow-none [&>.el-input__wrapper]:rounded-lg [&>.el-input__wrapper]:bg-slate-50/50"
                  />
                </el-form-item>

                <!-- 密码输入 -->
                <el-form-item prop="password" class="mb-6">
                  <el-input
                    v-model="loginFormData.password"
                    show-password
                    size="large"
                    type="password"
                    placeholder="请输入密码"
                    class="[&>.el-input__wrapper]:shadow-none [&>.el-input__wrapper]:rounded-lg [&>.el-input__wrapper]:bg-slate-50/50"
                  />
                </el-form-item>

                <!-- 验证码区域 -->
                <el-form-item v-if="loginFormData.openCaptcha" prop="captcha" class="mb-6">
                  <div class="flex gap-3 w-full">
                    <el-input
                      v-model="loginFormData.captcha"
                      placeholder="验证码"
                      size="large"
                      class="flex-1 [&>.el-input__wrapper]:shadow-none [&>.el-input__wrapper]:rounded-lg [&>.el-input__wrapper]:bg-slate-50/50"
                    />
                    <div class="w-1/3 overflow-hidden rounded-lg border border-slate-200/50 cursor-pointer">
                      <img
                        v-if="picPath"
                        class="w-full h-11 object-cover hover:opacity-90 transition-opacity"
                        :src="picPath"
                        alt="验证码"
                        @click="loginVerify()"
                      >
                    </div>
                  </div>
                </el-form-item>

                <!-- 登录按钮 -->
                <el-form-item class="mb-6">
                  <el-button
                    class="w-full h-11 rounded-lg bg-gradient-to-r from-blue-600 to-purple-600 hover:from-blue-500 hover:to-purple-500 
                           text-white font-semibold shadow-lg shadow-blue-500/30 border-0 transition-all"
                    size="large"
                    @click="submitForm"
                  >
                    登 录
                  </el-button>
                </el-form-item>
              </template>

              <!-- 初始化按钮 -->
              <el-form-item v-else class="mb-6">
                <el-button
                  class="w-full h-11 rounded-lg bg-gradient-to-r from-blue-600 to-purple-600 hover:from-blue-500 hover:to-purple-500 
                         text-white font-semibold shadow-lg shadow-blue-500/30 border-0"
                  size="large"
                  @click="checkInit"
                >
                  前往初始化
                </el-button>
              </el-form-item>

              <!-- OA登录 -->
              <el-form-item class="mb-6">
                <el-button
                  class="w-full h-11 rounded-lg bg-gradient-to-r from-slate-600 to-slate-500 hover:from-slate-500 hover:to-slate-400
                         text-white font-semibold shadow-lg shadow-slate-500/20 border-0"
                  size="large"
                  disabled
                  @click="oaLoginJump"
                >
                  OAuth2 登录 (敬请期待)
                </el-button>
              </el-form-item>
            </el-form>
          </div>
        </div>
      </div>
    </div>

    <!-- 底部链接 -->
    <BottomInfo class="absolute bottom-3 left-0 right-0 mx-auto w-full z-20">
      <div class="flex items-center justify-center gap-4 md:gap-6">
        <a v-for="(link, index) in socialLinks" 
           :key="index"
           :href="link.url" 
           target="_blank"
           class="p-2 rounded-full bg-white/10 backdrop-blur-sm hover:bg-white/20 transition-colors">
          <img 
            :src="link.icon" 
            class="w-6 h-6 md:w-7 md:h-7" 
            :alt="link.alt"
          >
        </a>
      </div>
    </BottomInfo>
  </div>
</template>

<style>

/* 动态渐变动画 */
@keyframes gradient-flow {
  0% { background-position: 0% 50%; }
  50% { background-position: 100% 50%; }
  100% { background-position: 0% 50%; }
}

.animate-gradient-flow {
  background-size: 200% 200%;
  animation: gradient-flow 15s ease infinite;
}

/* 粒子效果 */
.particle-network::after {
  content: '';
  position: absolute;
  inset: 0;
  background-image: 
    radial-gradient(circle at 20% 30%, rgba(255,255,255,0.15) 0%, transparent 30%),
    radial-gradient(circle at 80% 70%, rgba(255,255,255,0.1) 0%, transparent 35%);
  pointer-events: none;
}

/* 暗黑模式适配 */
.dark .particle-network::after {
  background-image: 
    radial-gradient(circle at 20% 30%, rgba(0,0,0,0.2) 0%, transparent 30%),
    radial-gradient(circle at 80% 70%, rgba(0,0,0,0.15) 0%, transparent 35%);
}
</style>

<script setup>

import { captcha } from '@/api/user'
import { checkDB } from '@/api/initdb'
import BottomInfo from '@/components/bottomInfo/bottomInfo.vue'
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/pinia/modules/user'

defineOptions({
  name: "Login",
})

const router = useRouter()
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
  return await userStore.LoginIn(loginFormData)
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

// 跳转oa登录链接
const oaLoginJump = () => {
  const clientId = import.meta.env.VITE_OA_LOGIN_CLINET_ID
  const oaUrl = import.meta.env.VITE_OA_URL
  const redirect_uri = window.location.origin + '#/loginCallback'
  // 获取loginCallback该路由的完整url

  const jumpUrl = oaUrl + '?client_id=' + clientId + '&redirect_uri=' + encodeURIComponent(redirect_uri) + '&state='
  console.log(jumpUrl)
  window.location.href = jumpUrl
}



</script>
