<template>
  <main class="v4-login" :class="{ 'is-motion-paused': !backgroundAnimationActive }" aria-labelledby="login-title">
    <div class="login-cosmos" aria-hidden="true"><i></i><i></i><i></i><b></b><b></b><b></b></div>
    <header class="v4-login-brand">
      <n-button quaternary size="small" :disabled="reducedMotion" :aria-pressed="backgroundAnimationActive"
        @click="backgroundMotion = !backgroundMotion">{{ reducedMotion ? '系统已减少动效' : backgroundMotion ? '暂停背景动效' : '开启背景动效' }}</n-button>
      <n-select class="login-theme-select" aria-label="登录页主题" :value="loginTheme" :options="loginThemeOptions" @update:value="chooseLoginTheme" />
    </header>
    <section class="v4-login-card">
      <div class="login-capsule-brand"><div class="login-indicators" aria-hidden="true"><i></i><i></i><i></i></div>
        <img :src="logoImage" alt="EIC 企智枢" /><span>ENTERPRISE INTELLIGENCE</span></div>
      <h1 id="login-title">企智枢AI后台管理系统</h1><p class="v4-login-subtitle">欢迎登录企智枢AI后台管理系统</p>
      <n-tabs v-model:value="activeTab" type="line" animated>
        <n-tab-pane name="password" tab="密码登录">
          <n-form ref="passwordFormRef" :model="passwordForm" :rules="passwordRules" label-placement="top"
            @submit.prevent="handlePasswordLogin">
            <n-form-item label="用户名" path="username">
              <n-input v-model:value="passwordForm.username" placeholder="用户名 / 邮箱 / 手机号" autocomplete="username" size="large">
                <template #prefix><PersonOutline /></template>
              </n-input>
            </n-form-item>
            <n-form-item label="密码" path="password">
              <n-input v-model:value="passwordForm.password" type="password" show-password-on="click" autocomplete="current-password" size="large" placeholder="请输入密码">
                <template #prefix><LockClosedOutline /></template>
              </n-input>
            </n-form-item>
            <n-form-item label="租户 ID" path="tenantId">
              <n-input v-model:value="passwordForm.tenantId" placeholder="请输入租户 ID" size="large">
                <template #prefix><BusinessOutline /></template>
              </n-input>
            </n-form-item>
            <div class="v4-login-options"><n-checkbox v-model:checked="passwordForm.remember">记住账号</n-checkbox>
              <n-button text @click="showUnavailable">忘记密码？</n-button></div>
            <n-button type="primary" attr-type="submit" block size="large" :loading="loading" class="v4-login-submit">登录</n-button>
          </n-form>
        </n-tab-pane>
        <n-tab-pane name="sms" tab="验证码登录">
          <n-alert type="info" title="验证码登录暂未开放">请使用密码登录，或联系管理员。</n-alert>
          <n-form :model="smsForm" label-placement="top" @submit.prevent="handleSmsLogin">
            <n-form-item label="手机号"><n-input v-model:value="smsForm.phone" autocomplete="tel" placeholder="手机号" /></n-form-item>
            <n-form-item label="租户 ID"><n-input v-model:value="smsForm.tenantId" placeholder="租户 ID" /></n-form-item>
            <n-form-item label="验证码"><n-input v-model:value="smsForm.captcha" autocomplete="one-time-code" placeholder="验证码" />
              <n-button @click="getCaptcha">获取验证码</n-button></n-form-item>
            <n-button block attr-type="submit">验证码登录</n-button>
          </n-form>
        </n-tab-pane>
      </n-tabs>
      <div class="login-experience-actions"><n-button secondary @click="router.push('/experience?mode=public')">体验演示</n-button>
        <n-button secondary @click="experienceVisible = true">限时体验</n-button></div>
      <footer class="v4-login-footer">还没有账户？<n-button text type="primary" @click="openRegister">立即注册</n-button>
        <small>注册需使用管理员生成的 24 小时有效注册码</small></footer>
    </section>
    <ExperienceAccessDialog v-model:show="experienceVisible" @entered="router.push('/experience')" />
    <n-modal v-model:show="registerVisible" preset="card" title="注册账号" class="v4-register-modal"
      :mask-closable="!registerLoading" :close-on-esc="!registerLoading" :closable="!registerLoading">
      <n-form :model="registerForm" label-placement="top" @submit.prevent="handleRegister">
        <n-form-item label="注册码" required>
          <n-input v-model:value="registerForm.registrationCode" placeholder="管理员提供的 24 小时有效注册码" :maxlength="128" autocomplete="off" />
        </n-form-item>
        <n-form-item label="用户名" required><n-input v-model:value="registerForm.username" placeholder="至少 2 个字符" /></n-form-item>
        <n-form-item label="密码" required><n-input v-model:value="registerForm.password" type="password" show-password-on="click" placeholder="至少 8 位" autocomplete="new-password" /></n-form-item>
        <n-form-item label="租户 ID" required><n-input v-model:value="registerForm.tenantId" placeholder="租户 ID" /></n-form-item>
        <n-form-item label="昵称"><n-input v-model:value="registerForm.nickname" placeholder="可选" /></n-form-item>
        <n-form-item label="部门" required><n-select v-model:value="registerForm.departmentId" filterable clearable :options="registerDepartments" placeholder="请选择部门" /></n-form-item>
        <n-form-item label="岗位" required><n-select v-model:value="registerForm.positionId" filterable clearable :options="registerPositions" placeholder="请选择岗位" /></n-form-item>
        <div class="v4-register-actions"><n-button :disabled="registerLoading" @click="registerVisible = false">取消</n-button>
          <n-button type="primary" attr-type="submit" :loading="registerLoading">注册</n-button></div>
      </n-form>
    </n-modal>
  </main>
</template>

<script setup>
import { computed, ref, reactive, defineOptions, onMounted, onBeforeUnmount, watch } from "vue";
import { useRouter } from "vue-router"; 
import { PersonOutline, LockClosedOutline, BusinessOutline } from '@vicons/ionicons5';
import { NInput, NButton, NForm, NFormItem, NCheckbox, NTabs, NTabPane, NModal, NSelect, NAlert } from 'naive-ui';
import { setAppearance } from '@/app/appearance';
import { message } from "@/app/ui/feedback";
import { login, getTokenUser, register, getRegisterOptions } from "../api/userapi";
import { getUserPermissions } from "../api/baseapi";
import logoImage from '../assets/eic-logo.svg';
import ExperienceAccessDialog from '@/components/ExperienceAccessDialog.vue';
import "@/app/styles/login.css";

defineOptions({ name: "LoginPage" });
const activeTab = ref("password");
const experienceVisible = ref(false);
const loginTheme = ref(localStorage.getItem('eic-login-theme') || 'system');
const loginThemeOptions = [{ label: '跟随系统', value: 'system' }, { label: '白天模式', value: 'light' }, { label: '夜晚模式', value: 'dark' }];
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
const reducedMotion = ref(motionPreference.matches);
const backgroundMotion = ref(true);
const backgroundAnimationActive = computed(() => backgroundMotion.value && !reducedMotion.value);
const syncMotionPreference = event => { reducedMotion.value = event.matches; };
const applyLoginTheme = () => setAppearance(loginTheme.value === 'system' ? (systemTheme.matches ? 'dark' : 'light') : loginTheme.value);
const chooseLoginTheme = value => {
  loginTheme.value = value;
  try { localStorage.setItem('eic-login-theme', value); } catch { /* 仍应用本次选择。 */ }
  applyLoginTheme();
};
onMounted(() => {
  applyLoginTheme();
  systemTheme.addEventListener('change', applyLoginTheme);
  motionPreference.addEventListener('change', syncMotionPreference);
});
onBeforeUnmount(() => {
  systemTheme.removeEventListener('change', applyLoginTheme);
  motionPreference.removeEventListener('change', syncMotionPreference);
});
const loading = ref(false);
const registerVisible = ref(false);
const registerLoading = ref(false);
const router = useRouter();
const registerForm = reactive({
  registrationCode: "",
  username: "",
  password: "",
  tenantId: "1000",
  nickname: "",
  departmentId: undefined,
  positionId: undefined,
});
const registerDepartments = ref([]);
const registerPositions = ref([]);
// 密码登录表单
const passwordForm = reactive({
  username: "",
  password: "",
  tenantId: "1000",
  remember: false,
});

// 验证码登录表单
const smsForm = reactive({
  phone: "",
  tenantId: "",
  captcha: "",
});

// 验证码按钮状态


// 获取验证码
const getCaptcha = () => message.info('验证码登录暂未开放');

// 密码登录
const passwordFormRef = ref(null);
const passwordRules = {
  username: { required: true, message: '请输入用户名', trigger: ['input', 'blur'] },
  password: { required: true, message: '请输入密码', trigger: ['input', 'blur'] },
  tenantId: { required: true, message: '请输入租户 ID', trigger: ['input', 'blur'] },
};
const handlePasswordLogin = async () => {
  if (loading.value) return;
  try { await passwordFormRef.value?.validate(); } catch { return; }
  loading.value = true;
  try {
    const response = await login(passwordForm.username, passwordForm.password, passwordForm.tenantId);
    if (response?.code !== 200 || !response.isSuccess || typeof response.data !== 'string' || !response.data) {
      message.error(response?.msg || '登录未成功，请检查账号和租户');
      return;
    }
    localStorage.setItem('token', response.data);
    if (passwordForm.remember) {
      localStorage.setItem('savedLoginInfo', JSON.stringify({
        username: passwordForm.username, tenantId: passwordForm.tenantId, remember: true,
      }));
    } else localStorage.removeItem('savedLoginInfo');
    // 页面壳层仍会重新核对权限；不把旧账号的缓存传给新会话。
    localStorage.removeItem('user');
    localStorage.removeItem('UserPermissions');
    await Promise.allSettled([
      getTokenUser().then(r => { if (r?.code === 200) localStorage.setItem('user', JSON.stringify(r.data)); }),
      getUserPermissions().then(r => { if (r?.code === 200) localStorage.setItem('UserPermissions', JSON.stringify(r.data)); }),
    ]);
    message.success('登录成功');
    await router.push('/home/my/ai-chat');
  } catch { /* 拦截器负责请求错误提示，不记录含密码的异常对象。 */ }
  finally { loading.value = false; }
};

// 验证码登录
const showUnavailable = () => {
  message.info("该功能暂未开放");
};

let registerOptionsSequence = 0;
const loadRegisterOptions = async () => {
  const sequence = ++registerOptionsSequence;
  registerDepartments.value = [];
  registerPositions.value = [];
  try {
    const tenantId = registerForm.tenantId || passwordForm.tenantId || "1000";
    const response = await getRegisterOptions(tenantId);
    if (sequence !== registerOptionsSequence || tenantId !== (registerForm.tenantId || passwordForm.tenantId || '1000')) return;
    const data = response?.data || {};
    registerDepartments.value = (data.departments || []).map((item) => ({
      value: item.id,
      label: item.name,
    }));
    registerPositions.value = (data.positions || []).map((item) => ({
      value: item.id,
      label: item.name,
    }));
  } catch (error) {
    if (sequence !== registerOptionsSequence) return;
    registerDepartments.value = [];
    registerPositions.value = [];
  }
};

const openRegister = () => {
  registerForm.registrationCode = "";
  registerForm.tenantId = passwordForm.tenantId || "1000";
  registerForm.departmentId = undefined;
  registerForm.positionId = undefined;
  registerVisible.value = true;
  loadRegisterOptions();
};

const handleRegister = async () => {
  if (!registerForm.registrationCode.trim()) {
    message.warning("请输入注册码，请联系 admin 管理员获取");
    return;
  }
  if (!registerForm.username || registerForm.username.trim().length < 2) {
    message.warning("请输入至少 2 个字符的用户名");
    return;
  }
  if (!registerForm.password || registerForm.password.length < 8) {
    message.warning("密码至少 8 位");
    return;
  }
  if (!registerForm.tenantId) {
    message.warning("请输入租户 ID");
    return;
  }
  if (!registerForm.departmentId) {
    message.warning("请选择部门");
    return;
  }
  if (!registerForm.positionId) {
    message.warning("请选择岗位");
    return;
  }
  registerLoading.value = true;
  try {
    const response = await register(
      registerForm.username.trim(),
      registerForm.password,
      registerForm.tenantId,
      {
        nickName: registerForm.nickname,
        registrationCode: registerForm.registrationCode.trim(),
        departmentId: registerForm.departmentId,
        positionId: registerForm.positionId,
      }
    );
    if (response && response.code === 200) {
      message.success("注册成功，账号待管理员审核通过后才能登录");
      passwordForm.username = registerForm.username.trim();
      passwordForm.tenantId = registerForm.tenantId;
      registerVisible.value = false;
      registerForm.registrationCode = "";
      registerForm.password = "";
    }
  } catch (error) {
    // HTTP 错误已由拦截器提示
  } finally {
    registerLoading.value = false;
  }
};

const handleSmsLogin = () => {
  message.info("验证码登录暂未开放");
};

// 从本地存储加载保存的登录信息（仅回填用户名和租户ID，不回填密码）
const loadSavedLoginInfo = () => {
  const savedInfo = localStorage.getItem('savedLoginInfo');
  if (savedInfo) {
    try {
      const info = JSON.parse(savedInfo);
      if (info.username && info.tenantId) {
        passwordForm.username = info.username;
        passwordForm.tenantId = info.tenantId;
        passwordForm.remember = info.remember !== false;
        return;
      }
    } catch (error) {
      console.error('Failed to parse saved login info:', error);
    }
  } else {
      passwordForm.username = '';
      passwordForm.password = '';
      passwordForm.tenantId = passwordForm.tenantId || '1000';
      passwordForm.remember = false;
  } 
};

watch(() => registerForm.tenantId, () => {
  registerForm.departmentId = undefined;
  registerForm.positionId = undefined;
  if (registerVisible.value) loadRegisterOptions();
});
onMounted(loadSavedLoginInfo);
</script>

